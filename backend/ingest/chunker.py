"""Section-based chunker (architecture §5.3, docs/chunking-research.md §6.1).

Input: the parsed documents in data/parsed/<source_id>.json (title + sections).

Rules
- One chunk per parsed section; sections are never merged. A section is split only when the
  chunk would exceed `chunking.max_tokens` (bge tokenizer, header included):
  - scheme pages: at sentence and `;` list-item boundaries. Every piece repeats the scheme-name
    lead sentence and the list's intro ("Annualised returns as of 06 Nov 2026, ...:"), so each
    piece is self-describing. No overlap.
  - prose pages: at sentence boundaries, with one sentence of overlap.
- Header on every chunk: "[<scheme name or source title>] <heading>", plus a synonym tail for
  the standard Groww headings so user wording ("TER", "monthly investment") matches.
- Excluded: scheme sections answered from SQL (holdings, similar funds), navigation and
  boilerplate, stubs under `chunking.min_tokens`, and any section containing personal data.
  Every dropped section is reported with its reason.

    python -m ingest.chunker [--out chunks.jsonl]   # chunk data/parsed and print a summary
"""

import argparse
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR, ChunkingSettings, get_settings
from app.pipeline.pii_patterns import find_pii_kinds

log = logging.getLogger(__name__)

TokenCounter = Callable[[str], int]

# Synonym tails for the fixed Groww headings: words users type that the section text lacks.
HEADING_SYNONYMS = {
    "Costs and exit load": "expense ratio, TER, charges, fees, exit load, stamp duty",
    "Investment limits and lock-in": (
        "minimum SIP, monthly investment, minimum investment, lumpsum, lock-in period"
    ),
    "NAV and fund size": "NAV, net asset value, price, AUM, fund size, assets under management",
    "Scheme details": "category, benchmark, riskometer, risk level, fund manager, launch date",
    "Investment objective": "objective, strategy, what the fund invests in",
    "Risk measures": "Sharpe, Sortino, standard deviation, alpha, beta, volatility",
    "Identifiers": "ISIN, AMFI scheme code, plan, option, registrar",
    "Rank within category": "rank, category rank",
    "Fund house": "AMC, asset management company, address, website",
}

# heading (regex, from the start) -> section_kind; first match wins. Non-scheme docs are "prose".
SECTION_KINDS = [
    (r"Investment objective", "objective"),
    (r"(Scheme details|Costs and exit load|Investment limits|NAV and fund size)", "facts"),
    (r"Risk measures", "risk"),
    (r"Fund manager\b", "manager"),
    (r"Fund house", "fund_house"),
    (r"Identifiers", "identifiers"),
    (r".*(returns|Rank within category)", "returns"),
]
AS_OF_KINDS = {"returns", "risk", "facts"}

NAV_HEADINGS = {"dark", "view more", "videos", "quick links", "media", "other links"}
NAVIGATED_TO = re.compile(r"^\s*Navigated to\b.*$", re.M)
AS_OF = re.compile(r"\bas of (\d{1,2} [A-Z][a-z]{2} \d{4})")
LIST_SENTENCE = re.compile(r"^(?P<intro>[^;]{0,250}?:)\s+(?P<items>.+;.+)$", re.S)
# Sentence ends at . ! ? followed by space and an uppercase letter, digit, quote or bracket.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(₹])")
ABBREVIATIONS = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Rs|No|Nos|Sr|Jr|St|Ltd|Pvt|Co|Inc|vs|viz|approx|e\.g|i\.e|etc)\.$",
    re.I,
)


@dataclass
class Chunk:
    chunk_id: str
    text: str  # header + "\n" + body; what gets embedded and BM25-indexed
    source_id: str
    source_url: str
    scheme_id: str | None
    amc: str | None
    doc_type: str
    section: str
    section_kind: str
    fetched_at: str
    as_of: str | None
    chunker_version: str


@dataclass
class Dropped:
    source_id: str
    section: str
    reason: str


@dataclass
class ChunkResult:
    chunks: list[Chunk] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)

    def extend(self, other: "ChunkResult") -> None:
        self.chunks += other.chunks
        self.dropped += other.dropped


# --------------------------------------------------------------------------- tokens


@lru_cache(maxsize=4)
def _tokenizer(model_name: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model_name)


def token_counter(model_name: str | None = None) -> TokenCounter:
    """Token count with the embedding model's own tokenizer (no special tokens)."""
    tok = _tokenizer(model_name or get_settings().retrieval.embedding_model)
    return lambda text: len(tok.encode(text, add_special_tokens=False))


# --------------------------------------------------------------------------- text helpers


def split_sentences(text: str) -> list[str]:
    """Sentences and lines; does not split after abbreviations such as "Mr." or "Rs."."""
    out: list[str] = []
    for line in text.split("\n"):
        parts = SENTENCE_END.split(line.strip())
        buf = ""
        for part in parts:
            buf = f"{buf} {part}" if buf else part
            if not ABBREVIATIONS.search(buf):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return [s for s in (o.strip() for o in out) if s]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "section"


def _clean_title(title: str) -> str:
    return " ".join(title.strip(" :").split())


def _header(title: str, heading: str | None) -> str:
    head = f"[{title}] {heading or ''}".strip()
    synonyms = HEADING_SYNONYMS.get(heading or "")
    return f"{head} ({synonyms})" if synonyms else head


def _section_kind(doc_type: str, heading: str) -> str:
    if doc_type != "scheme_page":
        return "prose"
    for pattern, kind in SECTION_KINDS:
        if re.match(pattern, heading):
            return kind
    return "facts"


def _as_of(kind: str, text: str) -> str | None:
    if kind not in AS_OF_KINDS or not (m := AS_OF.search(text)):
        return None
    return datetime.strptime(m.group(1), "%d %b %Y").date().isoformat()


def _is_menu_like(text: str) -> bool:
    """Mostly short lines (links, labels) with little prose.

    The prose check keeps real content that contains a flattened table, e.g. the glossary's
    "Expense Ratio" entry: many short table cells, but most of its words are in sentences.
    """
    lines = [ln.split() for ln in text.split("\n") if ln.strip()]
    if len(lines) < 6 or sum(len(ln) <= 4 for ln in lines) / len(lines) <= 0.6:
        return False
    words = sum(len(ln) for ln in lines)
    prose_words = sum(len(ln) for ln in lines if len(ln) >= 12)
    return prose_words / words < 0.3


# --------------------------------------------------------------------------- splitting


def _fit_words(text: str, budget: int, count: TokenCounter) -> list[str]:
    """Last resort for a single unit longer than the budget: split on word boundaries."""
    pieces, cur = [], []
    for word in text.split():
        if cur and count(" ".join([*cur, word])) > budget:
            pieces.append(" ".join(cur))
            cur = []
        cur.append(word)
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def _scheme_units(body: str) -> tuple[str, list[tuple[str | None, str]]]:
    """(lead sentence, units). A unit is (list intro or None, sentence or list item)."""
    sentences = split_sentences(body)
    lead, units = sentences[0], []
    for sentence in sentences[1:]:
        m = LIST_SENTENCE.match(sentence)
        if m:
            items = [i.strip() for i in m.group("items").rstrip(".").split(";") if i.strip()]
            units += [(m.group("intro"), item) for item in items]
        else:
            units.append((None, sentence))
    return lead, units


def _render_scheme(lead: str, units: list[tuple[str | None, str]]) -> str:
    parts, i = [lead], 0
    while i < len(units):
        intro, text = units[i]
        if intro is None:
            parts.append(text)
            i += 1
            continue
        items = []
        while i < len(units) and units[i][0] == intro:
            items.append(units[i][1])
            i += 1
        parts.append(f"{intro} {'; '.join(items)}.")
    return " ".join(parts)


def _split_scheme(header: str, body: str, max_tokens: int, count: TokenCounter) -> list[str]:
    lead, units = _scheme_units(body)
    budget = max_tokens - count(header) - 1
    pieces: list[str] = []
    group: list[tuple[str | None, str]] = []
    for unit in units:
        if group and count(_render_scheme(lead, [*group, unit])) > budget:
            pieces.append(_render_scheme(lead, group))
            group = []
        if not group and count(_render_scheme(lead, [unit])) > budget:
            intro, text = unit
            room = budget - count(_render_scheme(lead, [(intro, "")]))
            pieces += [_render_scheme(lead, [(intro, w)]) for w in _fit_words(text, room, count)]
            continue
        group.append(unit)
    if group:
        pieces.append(_render_scheme(lead, group))
    return pieces


def _split_prose(header: str, body: str, max_tokens: int, count: TokenCounter) -> list[str]:
    budget = max_tokens - count(header) - 1
    units: list[str] = []
    for sentence in split_sentences(body):
        units += [sentence] if count(sentence) <= budget else _fit_words(sentence, budget, count)
    pieces: list[str] = []
    group: list[str] = []
    for unit in units:
        if group and count(" ".join([*group, unit])) > budget:
            pieces.append(" ".join(group))
            overlap = group[-1]
            group = [overlap] if count(f"{overlap} {unit}") <= budget else []
        group.append(unit)
    if group:
        pieces.append(" ".join(group))
    return pieces


# --------------------------------------------------------------------------- chunking


def drop_reason(
    doc: dict, heading: str, body: str, cfg: ChunkingSettings, count: TokenCounter
) -> str | None:
    """Why a section is left out of the text index, or None to keep it."""
    if doc["type"] == "scheme_page":
        for pattern in cfg.exclude_sections:
            if re.match(pattern, heading):
                return "answered from SQL"
    if (kinds := find_pii_kinds(body)) or (kinds := find_pii_kinds(heading)):
        return f"personal data ({', '.join(kinds)})"
    if heading.strip().lower() in NAV_HEADINGS:
        return "navigation heading"
    if _is_menu_like(body):
        return "menu-like block"
    if count(body) < cfg.min_tokens:
        return f"under {cfg.min_tokens} tokens"
    return None


def chunk_document(
    doc: dict, cfg: ChunkingSettings | None = None, count: TokenCounter | None = None
) -> ChunkResult:
    cfg = cfg or get_settings().chunking
    count = count or token_counter()
    result = ChunkResult()
    sid = doc["source_id"]
    title = _clean_title(doc.get("title") or sid)
    used_slugs: Counter[str] = Counter()

    for section in doc.get("sections") or []:
        heading = (section.get("heading") or "").strip()
        body = NAVIGATED_TO.sub("", section.get("text") or "").strip()
        reason = drop_reason(doc, heading, body, cfg, count)
        if reason:
            result.dropped.append(Dropped(sid, heading, reason))
            continue

        header = _header(title, heading or None)
        if count(f"{header}\n{body}") <= cfg.max_tokens:
            pieces = [body]
        elif doc["type"] == "scheme_page":
            pieces = _split_scheme(header, body, cfg.max_tokens, count)
        else:
            pieces = _split_prose(header, body, cfg.max_tokens, count)

        slug = _slug(heading or "intro")
        used_slugs[slug] += 1
        if used_slugs[slug] > 1:  # repeated heading within one document
            slug = f"{slug}-{used_slugs[slug]}"
        kind = _section_kind(doc["type"], heading)
        as_of = _as_of(kind, body)
        for n, piece in enumerate(pieces):
            result.chunks.append(
                Chunk(
                    chunk_id=f"{sid}::{slug}::{n}",
                    text=f"{header}\n{piece}",
                    source_id=sid,
                    source_url=doc["url"],
                    scheme_id=doc.get("scheme_id"),
                    amc=doc.get("amc"),
                    doc_type=doc["type"],
                    section=heading,
                    section_kind=kind,
                    fetched_at=doc["fetched_at"],
                    as_of=as_of,
                    chunker_version=cfg.version,
                )
            )
    return result


def load_parsed(parsed_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(parsed_dir.glob("*.json"))]


def chunk_corpus(
    docs: Iterable[dict], cfg: ChunkingSettings | None = None, count: TokenCounter | None = None
) -> ChunkResult:
    cfg = cfg or get_settings().chunking
    count = count or token_counter()
    result = ChunkResult()
    for doc in docs:
        result.extend(chunk_document(doc, cfg, count))
    return result


# --------------------------------------------------------------------------- CLI


def summarise(result: ChunkResult, count: TokenCounter) -> str:
    sizes = sorted(count(c.text) for c in result.chunks)
    by_type = Counter(c.doc_type for c in result.chunks)
    reasons = Counter(re.sub(r" \(.*\)$", "", d.reason) for d in result.dropped)
    lines = [
        f"chunks            : {len(sizes)} ({', '.join(f'{k} {v}' for k, v in by_type.items())})",
        f"tokens per chunk  : median {sizes[len(sizes) // 2] if sizes else 0}, "
        f"max {sizes[-1] if sizes else 0}, total {sum(sizes)}",
        f"sections dropped  : {len(result.dropped)}",
    ]
    lines += [f"    - {reason}: {n}" for reason, n in reasons.most_common()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.chunker", description=__doc__)
    parser.add_argument("--parsed-dir", type=Path, default=DATA_DIR / "parsed")
    parser.add_argument("--out", type=Path, help="write chunks as JSON lines")
    parser.add_argument("--show-dropped", action="store_true", help="list every dropped section")
    args = parser.parse_args(argv)

    count = token_counter()
    result = chunk_corpus(load_parsed(args.parsed_dir), count=count)
    print(summarise(result, count))
    if args.show_dropped:
        for d in result.dropped:
            print(f"  {d.source_id} | {d.section or '(no heading)'} | {d.reason}")
    if args.out:
        with args.out.open("w", encoding="utf-8") as f:
            for chunk in result.chunks:
                f.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
