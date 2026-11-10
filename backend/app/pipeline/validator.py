"""Step 6: deterministic checks on the model's reply before anything reaches the user
(architecture §6.6).

| Check            | On failure                                                  |
|------------------|-------------------------------------------------------------|
| schema           | retry once, then fallback                                   |
| answerable       | no-evidence reply                                           |
| evidence_id      | retry once, then fallback                                   |
| length (<= 3)    | truncate to the first 3 sentences                           |
| no URLs          | strip them                                                  |
| no advice        | advisory refusal                                            |
| no judgement     | performance-judgement refusal                               |
| figures          | retry once, then fact/metric template                       |
| basis and date   | retry once, then metric template                            |
| fact consistency | retry once, then fact template                              |

"Fallback" is the deterministic fact/metric template when the evidence has one, else the
no-evidence reply. The validator only decides; the pipeline carries the action out.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.pipeline.generator import GeneratedAnswer
from app.pipeline.retriever import Evidence
from app.pipeline.scheme_resolver import Intent
from app.pipeline.text import normalise_name, split_sentences

Action = Literal["accept", "retry", "no_evidence", "advisory_refusal", "judgement_refusal"]

MAX_SENTENCES = 3

URL = re.compile(r"\(?\s*(?:https?://|www\.)\S+\s*\)?", re.I)
NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d+)*(?:\.\d+)?")

ADVICE = [re.compile(p, re.I) for p in (
    r"\byou\s+should\s+(invest|buy|sell|consider|choose|switch|redeem|hold|stay|avoid|pick|go|"
    r"exit|start|stop|continue)\b",
    r"\b(i|we)\s+(would\s+)?(recommend|suggest|advise)\b",
    r"\brecommended\b",
    r"\bconsider\s+(investing|buying|adding|switching|selling)\b",
    r"\bgood\s+(choice|option|investment|fund|pick|time\s+to)\b",
    r"\bworth\s+(it|investing|buying|considering)\b",
    r"\b(should|could)\s+(invest|buy|sell|switch|redeem)\s+(in|into|now)\b",
    r"\byou\s+(may|might)\s+want\s+to\b",
    r"\b(suitable|ideal|perfect|right)\s+(for\s+you|choice|fit)\b",
    r"\bbest\s+(fund|option|choice|scheme|bet)\b",
)]
JUDGEMENT = [re.compile(p, re.I) for p in (
    r"\b(performed|performing|done|doing)\s+(well|poorly|badly|strongly|nicely|impressively)\b",
    r"\b(strong|good|great|excellent|impressive|solid|poor|weak|disappointing|attractive|"
    r"healthy|decent|stellar|robust)\s+(returns?|performance|performer|track\s+record|growth)\b",
    r"\b(out|under)perform\w*\b",
    r"\bbeat(s|en)?\s+(the|its)\s+(benchmark|index|market|category|peers|average)\b",
    r"\b(better|worse)\s+than\b",
    r"\b(top|best|worst)[\s-]+perform(ing|er|ers)\b",
    # predicate form: "its returns are strong", "performance has been impressive"
    r"\b(returns?|performance|track\s+record)\s+(is|are|was|were|has\s+been|have\s+been|"
    r"remains?|looks?)\s+(\w+\s+)?(strong|good|great|excellent|impressive|solid|poor|weak|"
    r"disappointing|attractive|healthy|decent|stellar|robust)\b",
)]
RETURN_MENTION = re.compile(r"\b(returns?|cagr)\b", re.I)
BASIS = re.compile(r"\b(annuali[sz]ed|absolute|sip|cagr|per\s+annum|p\.a\.?)\b", re.I)
DATE = re.compile(
    r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{4}\b",
    re.I,
)
_NIL = {"nil", "none", "no lock-in", "no lock in", "0", "0%", "no exit load"}

# Which action a failed rule leads to (also the order they are reported in).
RULE_ACTIONS: dict[str, Action] = {
    "schema": "retry",
    "answerable": "no_evidence",
    "evidence_id": "retry",
    "advice": "advisory_refusal",
    "judgement": "judgement_refusal",
    "empty": "retry",
    "figures": "retry",
    "basis_and_date": "retry",
    "fact_consistency": "retry",
}
_PRIORITY = ["advisory_refusal", "judgement_refusal", "no_evidence", "retry", "accept"]


@dataclass(frozen=True)
class Verdict:
    action: Action
    answer: str | None = None  # the cleaned answer (accept only)
    evidence: Evidence | None = None  # the cited block
    failures: tuple[str, ...] = ()  # rules that failed and need the action above
    fixes: tuple[str, ...] = field(default_factory=tuple)  # rules repaired in place

    @property
    def ok(self) -> bool:
        return self.action == "accept"


def numbers(text: str) -> set[float]:
    return {float(m.group().replace(",", "")) for m in NUMBER.finditer(text)}


def _value_words(value: str) -> list[str]:
    return [w for w in normalise_name(value).split() if w not in ("the", "of", "and", "a")]


def value_present(value, answer: str) -> bool:
    """Does the answer state this fact value (as published)?"""
    if value is None:
        return True
    low = answer.lower()
    if isinstance(value, list | tuple):
        # Every listed name (fund managers): a partial list reads as the complete one.
        return all(normalise_name(str(v)) in normalise_name(answer) for v in value)
    if isinstance(value, date):
        month = value.strftime("%b").lower()
        return str(value.year) in answer and month in low and str(value.day) in answer
    text = str(value).strip()
    if text.lower().rstrip(".") in _NIL:
        return bool(re.search(r"\b(no|nil|none|not|zero)\b", low))
    wanted = numbers(text)
    if wanted:
        return wanted <= numbers(answer)
    words = _value_words(text)
    if not words:
        return True
    present = set(normalise_name(answer).split())
    return sum(w in present for w in words) / len(words) >= 0.6


def _strip_urls(answer: str) -> str:
    cleaned = URL.sub(" ", answer)
    return re.sub(r"\s+([.,;:])", r"\1", " ".join(cleaned.split())).strip()


def validate(
    generated: GeneratedAnswer | None,
    evidence: Sequence[Evidence],
    intent: Intent | None = None,
) -> Verdict:
    """Check one model reply against the evidence that was sent with it."""
    if generated is None:
        return Verdict("retry", failures=("schema",))
    if not generated.answerable:
        return Verdict("no_evidence", failures=("answerable",))
    by_id = {e.id: e for e in evidence}
    cited = by_id.get(generated.evidence_id.strip())
    if cited is None:
        return Verdict("retry", failures=("evidence_id",))

    answer = generated.answer.strip()
    fixes: list[str] = []
    if URL.search(answer):
        answer = _strip_urls(answer)
        fixes.append("urls")
    sentences = split_sentences(answer)
    if len(sentences) > MAX_SENTENCES:
        answer = " ".join(sentences[:MAX_SENTENCES])
        fixes.append("length")

    failures: list[str] = []
    if not answer:
        failures.append("empty")
    if any(p.search(answer) for p in ADVICE):
        failures.append("advice")
    if any(p.search(answer) for p in JUDGEMENT):
        failures.append("judgement")
    if not numbers(answer) <= numbers(cited.text):
        failures.append("figures")
    if RETURN_MENTION.search(answer) and "%" in answer and not (
        BASIS.search(answer) and DATE.search(answer)
    ):
        failures.append("basis_and_date")
    elif cited.kind in ("metric", "holdings") and numbers(answer) and not DATE.search(answer):
        # NAV, AUM, ranks, holdings: every published figure carries its as-of date
        failures.append("basis_and_date")
    asked = set(intent.fields) if intent else set()
    for block in evidence:
        if block.kind != "fact":
            continue
        for name, value in block.data.get("fields", {}).items():
            if name in asked and not value_present(value, answer):
                failures.append("fact_consistency")
                break

    if not failures:
        return Verdict("accept", answer, cited, (), tuple(fixes))
    action = min((RULE_ACTIONS[f] for f in failures), key=_PRIORITY.index)
    return Verdict(action, None, cited, tuple(dict.fromkeys(failures)), tuple(fixes))


def correction_message(failures: Sequence[str]) -> str:
    """Told to the model on the one retry: what went wrong, without new instructions."""
    reasons = {
        "schema": "the reply was not valid JSON in the required shape",
        "evidence_id": "evidence_id must be the id of one of the evidence blocks, e.g. E1",
        "empty": "the answer was empty",
        "figures": "every number in the answer must appear exactly in the cited evidence block",
        "basis_and_date": "state each return with its basis (annualised, absolute or SIP) and "
                          "its as-of date from the evidence",
        "fact_consistency": "the answer must state the value given in the fact evidence block",
    }
    listed = "; ".join(reasons[f] for f in failures if f in reasons)
    return f"Your previous reply was rejected: {listed}. Reply again with the JSON object only."
