"""Check golden.yaml's expected values against the saved source pages (data/raw).

    python eval/verify_golden.py

For every case with `values`, each value must appear in the newest raw snapshot of the cited
page (for `cite: scheme`, the scheme's Groww page; for a list, at least one of the listed
sources). It reads the page itself, visible text and embedded JSON, not the parsed tables, so a
parser bug cannot make the golden set agree with itself. Free-text alternatives such as
"no exit load | nil" pass when any one of them is on the page.

A scheme page holds hundreds of figures (holdings weights, peers' returns), so a number counts
only when it matches exactly and appears within a short window of a label for the field the
question asks about (e.g. "expense" near 0.70 for an expense-ratio case). The field comes from
the pipeline's own intent detector; a question with no detectable field searches the whole page.

Cases with a `verify_note` (a value the page implies but doesn't spell out) are listed, not
checked. Also validates the file's structure. Exit code 1 on any problem.
"""

import re
import sys

from checks import (
    _NUMERIC,
    ROOT_DIR,
    load_golden,
    load_sources,
    normalise,
    schema_problems,
    value_present,
)
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.pipeline.scheme_resolver import detect_intent

RAW_DIR = ROOT_DIR / "data" / "raw"
# Phrasings that describe a value rather than quote it; the page shows the quoted form.
_PARAPHRASES = {"no exit load", "not charge", "no load", "0%", "does not have", "not have a lock",
                "one year", "12 months", "365 days", "ninety days", "fifteen days", "thirty days",
                "one month", "seven days", "three years", "3-year", "three-year", "1 month",
                "30 days", "1 year", "every month", "each month", "once a month", "six",
                "regular interval", "periodic", "directly", "evaluate", "compare", "index",
                "standard", "dp", "stockbroker", "broker", "exit", "sell", "withdraw",
                "redemption", "1.5 lakh", "1,50,000", "150000", "1.5 lac"}

# field / metric -> words that label it on a Groww page (visible text or JSON keys)
LABELS = {
    "expense_ratio": ("expense",), "exit_load": ("exit",), "min_sip": ("sip",),
    "min_lumpsum": ("lumpsum", "min_investment", "minimum", "min_inv"),
    "riskometer": ("risk",), "benchmark": ("benchmark",), "nav": ("nav",),
    "aum": ("aum", "fund size"), "return": ("return",), "category_rank": ("rank",),
    "category_average_return": ("cat_avg", "category"), "holdings": ("holding", "company"),
}
WINDOW = 200


def page_text(source_id: str) -> str | None:
    folder = RAW_DIR / source_id
    snaps = sorted(folder.glob("*.htm*")) if folder.exists() else []
    if not snaps:
        return None
    tree = HTMLParser(snaps[-1].read_text(encoding="utf-8", errors="replace"))
    # Embedded JSON holds the unrounded figures; SVG paths and styles are full of stray numbers.
    scripts = [n.text() for n in tree.css("script")]
    for node in tree.css("style, svg, script, noscript"):
        node.decompose()
    visible = tree.body.text(separator=" ") if tree.body else ""
    return normalise("\n".join([visible, *scripts]))


def labelled_text(page: str, question: str) -> str:
    """The parts of the page within WINDOW characters of a label for the asked field."""
    intent = detect_intent(question)
    keys = [*intent.fields, *(m.metric for m in intent.metrics)]
    words = [w for k in keys for w in LABELS.get(k, ())]
    if not words:
        return page
    spans = sorted((max(0, m.start() - WINDOW), m.end() + WINDOW)
                   for w in words for m in re.finditer(re.escape(w), page))
    return "\n".join(page[a:b] for a, b in spans)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    golden = load_golden()
    sources = load_sources()
    scheme_source = {s["scheme_id"]: sid for sid, s in sources.items()
                     if s.get("type") == "scheme_page"}
    problems = schema_problems(golden, sources, set(scheme_source))

    texts: dict[str, str | None] = {}
    checked = 0
    notes = []
    for case in golden.cases:
        for n, turn in enumerate(case.turns, start=1):
            e = turn.expect
            if not e.get("values"):
                continue
            cite = e.get("cite")
            if cite == "scheme":
                cited = [scheme_source[e["scheme"]]]
            elif isinstance(cite, list):
                cited = cite
            else:
                continue
            where = f"{case.id}" + (f" turn {n}" if len(case.turns) > 1 else "")
            if case.verify_note:
                notes.append(f"{where}: {case.verify_note}")
                continue
            for sid in cited:
                if sid not in texts:
                    texts[sid] = page_text(sid)
            pages = [texts[s] for s in cited if texts.get(s)]
            if not pages:
                problems.append(f"{where}: no raw snapshot for {cited}")
                continue
            for value in e["values"]:
                checked += 1
                alts = [a.strip() for a in value.split("|")]
                quoted = [a for a in alts if a.lower() not in _PARAPHRASES] or alts
                numeric = any(_NUMERIC.match(a) for a in quoted)
                found = any(
                    value_present(" | ".join(quoted),
                                  labelled_text(page, turn.q) if numeric else page,
                                  units=False, exact=True)
                    for page in pages
                )
                if not found:
                    problems.append(f"{where}: {value!r} not found on {', '.join(cited)}")

    print(f"Checked {checked} expected values in {len(golden.cases)} cases.")
    for note in notes:
        print(f"  not checked: {note}")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("All expected values appear on their cited source pages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
