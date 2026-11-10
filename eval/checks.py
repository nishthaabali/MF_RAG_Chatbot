"""Golden-set loading and the per-reply checks used by eval/run.py and eval/tune.py.

Kept free of I/O beyond reading config files, so backend/tests/test_eval_checks.py can test the
matching rules without a database, index or API key.
"""

import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = EVAL_DIR.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.pipeline.text import normalise_unicode, split_sentences  # noqa: E402
from app.pipeline.validator import ADVICE, JUDGEMENT  # noqa: E402

GOLDEN_FILE = EVAL_DIR / "golden.yaml"
CONFIG_DIR = ROOT_DIR / "config"

BUCKETS = (
    "scheme_facts", "general_facts", "advisory", "metrics", "performance_judgement", "pii",
    "follow_ups", "other_routes",
)
REPLY_TYPES = {
    "answer", "advisory_refusal", "performance_refusal", "out_of_scope", "not_covered",
    "greeting", "pii_blocked", "no_evidence", "clarify", "unavailable",
}
EXPECT_KEYS = {"type", "scheme", "cite", "values", "forbid", "basis", "as_of", "candidates"}

URL_RE = re.compile(r"https?://\S+", re.I)
FOOTER_RE = re.compile(r"Last updated from sources: (\d{2} [A-Z][a-z]{2} \d{4})\s*$")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
BASIS_RE = {
    "annualised": re.compile(r"\b(annuali[sz]ed|cagr|per\s+annum|p\.\s?a\.?)", re.I),
    "absolute": re.compile(r"\b(absolute|point[\s-]to[\s-]point|cumulative)\b", re.I),
}


# ------------------------------------------------------------------------------ golden file
@dataclass(frozen=True)
class Turn:
    q: str
    expect: dict[str, Any]


@dataclass(frozen=True)
class Case:
    id: str
    bucket: str
    turns: tuple[Turn, ...]
    pii: str | None = None  # the personal detail in a PII case, for the leak checks
    verify_note: str | None = None  # why verify_golden can't find the value verbatim on the page


@dataclass
class Golden:
    cases: list[Case]
    targets: dict[str, float]
    factual_buckets: tuple[str, ...]


def load_golden(path: Path = GOLDEN_FILE) -> Golden:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = []
    for raw in data["cases"]:
        if "turns" in raw:
            turns = tuple(Turn(t["q"], _expect(t["expect"])) for t in raw["turns"])
        else:
            turns = (Turn(raw["q"], _expect(raw["expect"])),)
        cases.append(Case(raw["id"], raw["bucket"], turns, raw.get("pii"), raw.get("verify_note")))
    return Golden(cases, dict(data["targets"]), tuple(data["factual_buckets"]))


def _expect(raw: dict) -> dict:
    out = dict(raw)
    if isinstance(out.get("type"), str):
        out["type"] = [out["type"]]
    if isinstance(out.get("as_of"), str):
        out["as_of"] = date.fromisoformat(out["as_of"])
    return out


def load_sources(config_dir: Path = CONFIG_DIR) -> dict[str, dict]:
    data = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8"))
    items = data["sources"] if isinstance(data, dict) else data
    return {s["id"]: s for s in items}


def schema_problems(golden: Golden, sources: dict[str, dict], scheme_ids: set[str]) -> list[str]:
    """Structural mistakes in golden.yaml: unknown buckets, ids, types, keys or sources."""
    problems = []
    seen: set[str] = set()
    for case in golden.cases:
        where = f"case {case.id}"
        if case.id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(case.id)
        if case.bucket not in BUCKETS:
            problems.append(f"{where}: unknown bucket {case.bucket!r}")
        if case.bucket == "pii" and not case.pii:
            problems.append(f"{where}: PII case without a `pii:` value")
        if case.pii and all(case.pii not in t.q for t in case.turns):
            problems.append(f"{where}: `pii` value is not in the message")
        for turn in case.turns:
            e = turn.expect
            if not turn.q.strip():
                problems.append(f"{where}: empty message")
            for key in set(e) - EXPECT_KEYS:
                problems.append(f"{where}: unknown expect key {key!r}")
            for t in e.get("type") or []:
                if t not in REPLY_TYPES:
                    problems.append(f"{where}: unknown reply type {t!r}")
            if not e.get("type"):
                problems.append(f"{where}: no expected type")
            for sid in [e.get("scheme"), *e.get("candidates", [])]:
                if sid and sid not in scheme_ids:
                    problems.append(f"{where}: unknown scheme_id {sid!r}")
            cite = e.get("cite")
            if cite == "scheme" and not e.get("scheme"):
                problems.append(f"{where}: cite: scheme needs `scheme`")
            elif isinstance(cite, list):
                problems += [f"{where}: unknown source {s!r}" for s in cite if s not in sources]
            elif cite not in (None, "scheme", "education"):
                problems.append(f"{where}: bad cite {cite!r}")
            if "answer" in (e.get("type") or []) and e.get("cite") is None:
                problems.append(f"{where}: answers need a `cite`")
            if e.get("basis") and e["basis"] not in BASIS_RE:
                problems.append(f"{where}: unknown basis {e['basis']!r}")
    for name in golden.targets:
        if name not in (*BUCKETS, "format", "factual"):
            problems.append(f"targets: unknown name {name!r}")
    return problems


# ------------------------------------------------------------------------------ value matching
_NUMERIC = re.compile(r"^(?P<cur>₹)?\s*(?P<num>[+-]?\d[\d,]*(?:\.\d+)?)\s*(?P<pct>%)?$")
_ANSWER_NUMBER = re.compile(
    r"(?P<cur>₹|\brs\.?|\binr)?\s*(?P<num>[+-]?\d[\d,]*(?:\.\d+)?)\s*(?P<pct>%|\s?percent\b)?",
    re.I,
)


def normalise(text: str) -> str:
    text = normalise_unicode(text).lower()
    text = re.sub(r"[‐-―−]", "-", text)  # dashes
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def _numbers(answer: str) -> list[tuple[float, bool, bool]]:
    """(value, has_currency, is_percent) for every number in the answer."""
    found = []
    for m in _ANSWER_NUMBER.finditer(normalise_unicode(answer)):
        try:
            value = float(m["num"].replace(",", ""))
        except ValueError:
            continue
        found.append((value, bool(m["cur"]), bool(m["pct"])))
    return found


def _close(expected: float, got: float) -> bool:
    """Equal, or the same figure rounded to 2 decimals (the metric template's format)."""
    return abs(expected - got) < 1e-9 or abs(round(expected, 2) - got) < 1e-9 or (
        abs(expected - got) <= 0.005 + 1e-9 and abs(got * 100 - round(got * 100)) < 1e-6
    )


def value_present(expected: str, answer: str, *, units: bool = True, exact: bool = False) -> bool:
    """One expected item; `a | b` means either. Numbers compare numerically with their unit.

    `units=False` ignores % and currency (for page JSON, where figures are bare numbers);
    `exact=True` drops the 2-decimal rounding allowance (pages carry the unrounded figure).
    """
    for alt in (a.strip() for a in expected.split("|")):
        if not alt:
            continue
        m = _NUMERIC.match(alt)
        if m:
            want = float(m["num"].replace(",", ""))
            for got, cur, pct in _numbers(answer):
                if abs(got) != abs(want) and (exact or not _close(abs(want), abs(got))):
                    continue
                if units and m["pct"] and not pct:
                    continue
                if units and m["cur"] and not cur and pct:
                    continue
                return True
        elif normalise(alt) in normalise(answer):
            return True
    return False


def date_present(d: date, text: str) -> bool:
    month = MONTHS[d.month - 1]
    day = rf"0?{d.day}(st|nd|rd|th)?"
    patterns = (
        rf"\b{day}\s+{month}[a-z]*\.?,?\s+{d.year}\b",
        rf"\b{month}[a-z]*\.?\s+{day},?\s+{d.year}\b",
        rf"\b{d.isoformat()}\b",
        rf"\b0?{d.day}[/-]0?{d.month}[/-]{d.year}\b",
    )
    return any(re.search(p, text, re.I) for p in patterns)


# ------------------------------------------------------------------------------ reply checks
@dataclass
class TurnCheck:
    """Outcome of one reply: named checks, each True/False, with details for failures."""

    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    skipped: str | None = None  # why the content checks could not run (e.g. no LLM)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = bool(ok)
        if not ok and detail:
            self.details[name] = detail

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def format_ok(self) -> bool:
        return all(v for k, v in self.checks.items() if k.startswith("format."))

    def failures(self) -> list[str]:
        return [f"{k}: {self.details.get(k, 'failed')}" for k, v in self.checks.items() if not v]


@dataclass(frozen=True)
class CitationContext:
    """What citation checks need: allowlisted URLs, per-source dates and the link groups."""

    url_to_source: dict[str, str]
    source_dates: dict[str, date | None]
    scheme_urls: dict[str, str]
    education_urls: frozenset[str]

    @classmethod
    def from_catalog(cls, catalog, sources: dict[str, dict]) -> "CitationContext":
        url_to_source = {s["url"]: sid for sid, s in sources.items()}
        dates = {sid: catalog.link(sid).last_updated for sid in sources}
        schemes = {sid: s.url for sid, s in catalog.schemes.items()}
        education = frozenset(catalog.education_link(t).url for t in catalog.education_topics)
        return cls(url_to_source, dates, schemes, education)

    def expected_urls(self, expect: dict, sources: dict[str, dict]) -> set[str] | None:
        cite = expect.get("cite")
        if cite is None:
            return None
        if cite == "scheme":
            return {self.scheme_urls[expect["scheme"]]}
        if cite == "education":
            return set(self.education_urls)
        return {sources[s]["url"] for s in cite}


def check_format(reply, ctx: CitationContext, tc: TurnCheck) -> None:
    """The rules every reply must meet, whatever its type."""
    from app.pipeline.templates import format_date

    sentences = split_sentences(reply.answer)
    tc.add("format.sentences", 1 <= len(sentences) <= 3, f"{len(sentences)} sentences")
    tc.add("format.no_url_in_answer", not URL_RE.search(reply.answer), "URL inside the answer")
    urls = URL_RE.findall(reply.rendered)
    tc.add("format.one_link", len(urls) == 1 and urls[0] == reply.citation_url,
           f"{len(urls)} links")
    source_id = ctx.url_to_source.get(reply.citation_url)
    tc.add("format.allowlisted", source_id is not None, f"{reply.citation_url} not in sources")
    footer = FOOTER_RE.search(reply.rendered)
    want = ctx.source_dates.get(source_id) if source_id else None
    ok = footer is not None and want is not None and footer.group(1) == format_date(want)
    tc.add("format.footer", ok, f"footer {footer.group(1) if footer else None!r}, "
                                f"source fetched {want}")


def check_compliance(reply, tc: TurnCheck) -> None:
    advice = [p.pattern for p in ADVICE if p.search(reply.answer)]
    tc.add("compliance.no_advice", not advice, f"advice wording {advice}")
    if reply.type == "answer":  # refusal templates may name what they won't do
        judged = [p.pattern for p in JUDGEMENT if p.search(reply.answer)]
        tc.add("compliance.no_judgement", not judged, f"judgement wording {judged}")


def check_expectations(result, expect: dict, ctx: CitationContext, sources: dict[str, dict],
                       tc: TurnCheck, *, no_llm: bool = False) -> None:
    """Route, scheme, citation and content checks for one turn."""
    reply = result.reply
    types = expect.get("type") or []
    if no_llm and reply.type == "unavailable":
        tc.skipped = "needs the LLM (text answer, no deterministic template)"
        return
    tc.add("route", reply.type in types, f"got {reply.type} ({result.route}), want {types}")

    scheme = expect.get("scheme")
    if scheme:
        tc.add("scheme", result.active_scheme_id == scheme,
               f"active scheme {result.active_scheme_id}, want {scheme}")

    urls = ctx.expected_urls(expect, sources)
    if urls is not None:
        tc.add("citation", reply.citation_url in urls,
               f"cited {ctx.url_to_source.get(reply.citation_url, reply.citation_url)}")

    if reply.type not in types:
        return  # content checks only make sense on the expected reply type
    answer = reply.answer
    missing = [v for v in expect.get("values", []) if not value_present(v, answer)]
    if expect.get("values"):
        tc.add("values", not missing, f"missing {missing}")
    present = [v for v in expect.get("forbid", []) if value_present(v, answer)]
    if expect.get("forbid"):
        tc.add("forbid", not present, f"contains {present}")
    if expect.get("basis") and reply.type == "answer":
        tc.add("basis", bool(BASIS_RE[expect["basis"]].search(answer)),
               f"no '{expect['basis']}'")
    if expect.get("as_of") and reply.type == "answer":
        tc.add("as_of", date_present(expect["as_of"], answer), f"no date {expect['as_of']}")
    if expect.get("candidates"):
        offered = set(result.pending_candidates)
        lacking = [c for c in expect["candidates"] if c not in offered]
        tc.add("candidates", not lacking, f"not offered {lacking}")
