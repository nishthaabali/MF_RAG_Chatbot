"""Side-by-side comparison of published figures for 2-4 covered schemes.

"Compare the expense ratio, NAV and 3Y CAGR of Kotak Multi Cap and ICICI Prudential Large Cap"
gets a table: one column per scheme (in the order named), one row per asked field or figure,
each cell exactly as the scheme's page publishes it. A dash marks a value the page doesn't
publish; nothing is inferred.

What keeps this facts-only:
- The table is built straight from the facts and metrics tables, with no LLM, so a value can't
  be invented or reworded into a judgement.
- Columns are never sorted and cells never highlighted; no row says which value is lower,
  higher, better or cheaper.
- Only neutral wording ("compare", "vs", "side by side") is answered this way. A question that
  also asks for a verdict ("which is better", "which has the lower expense ratio", "should I")
  is still refused by the classifier as advisory.
- Each column cites its own scheme page, and the footer date is the oldest of those pages.
"""

import re
from collections.abc import Sequence
from datetime import date

from app.pipeline import templates
from app.pipeline.catalog import Catalog, to_ist_date
from app.pipeline.scheme_resolver import Intent, MetricIntent
from app.pipeline.templates import (
    FIELD_LABELS,
    NOT_PUBLISHED,
    Comparison,
    ComparisonColumn,
    ComparisonRow,
    Reply,
    format_date,
    format_metric_value,
)

MAX_SCHEMES = 4
_PERIOD_ORDER = ("1d", "1w", "1m", "3m", "6m", "9m", "1y", "2y", "3y", "4y", "5y", "7y", "10y",
                 "since_launch")

# Neutral comparison wording. Judgement wording ("better", "which ... lower") is matched by the
# classifier's advisory patterns, which still run on the rest of the message.
COMPARE_PHRASE = re.compile(r"\b(compar(e|ed|es|ing|ison)|vs|versus|side\s+by\s+side)\b")

# Fields a table can show. Manager education / experience are long published profiles that
# don't fit a cell; they are answered per scheme.
TABLE_FIELDS = tuple(f for f in FIELD_LABELS if f not in ("manager_education",
                                                         "manager_experience"))
NOT_TABULAR_METRICS = {"holdings": "top holdings", "peers": "similar funds"}

# "Compare Kotak Multi Cap and ICICI Pru Large Cap" with no field named: the scheme basics.
DEFAULT_INTENT = Intent(
    fields=("category", "expense_ratio", "exit_load", "min_sip", "riskometer", "benchmark"),
    metrics=(MetricIntent("nav", None, "point"), MetricIntent("aum", None, "point"),
             MetricIntent("return", "3y", "annualised")),
)


def is_comparison_phrase(text: str) -> bool:
    return bool(COMPARE_PHRASE.search(text.lower()))


def strip_comparison_phrase(text: str) -> str:
    """The message without its neutral comparison words, for the advisory check."""
    return COMPARE_PHRASE.sub(" ", text)


def table_intent(intent: Intent) -> tuple[Intent, list[str]]:
    """The part of `intent` a table can show, and labels for the parts it can't."""
    if intent.empty:
        return DEFAULT_INTENT, []
    fields = tuple(f for f in intent.fields if f in TABLE_FIELDS)
    metrics = tuple(m for m in intent.metrics if m.metric not in NOT_TABULAR_METRICS)
    left_out = [NOT_TABULAR_METRICS[m.metric] for m in intent.metrics
                if m.metric in NOT_TABULAR_METRICS]
    left_out += [FIELD_LABELS[f] for f in intent.fields
                 if f not in TABLE_FIELDS and f in FIELD_LABELS]
    return Intent(fields, metrics), list(dict.fromkeys(left_out))


def can_tabulate(intent: Intent) -> bool:
    """False when every asked item is one a table can't show (e.g. only holdings)."""
    if intent.empty:
        return True
    shown, _ = table_intent(intent)
    return not shown.empty


# ------------------------------------------------------------------------------ cell values
def _sentence_case(label: str) -> str:
    """'minimum SIP amount' -> 'Minimum SIP amount' (str.capitalize would lower 'SIP')."""
    return label[:1].upper() + label[1:]


def _period_key(row: dict) -> int:
    period = row.get("period")
    return _PERIOD_ORDER.index(period) if period in _PERIOD_ORDER else -1


def _fact_cell(field: str, value) -> str:
    if value in (None, "", []):
        return NOT_PUBLISHED
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, date):
        return format_date(value)
    return " ".join(str(value).split())


def _metric_cell(row: dict) -> str:
    if row["unit"] == "rank" and row.get("rank_of"):
        return f"{row['value']:g} of {row['rank_of']}"
    return format_metric_value(row["value"], row["unit"], row.get("metric", ""))


def build(catalog: Catalog, scheme_ids: Sequence[str], intent: Intent,
          evidence_by_scheme: dict[str, list], *, left_out: Sequence[str] = (),
          dropped: Sequence[str] = ()) -> Reply:
    """The comparison reply from each scheme's structured evidence (fact and metric blocks).

    `evidence_by_scheme`: scheme id -> the retriever's structured Evidence for `intent`.
    `left_out`: asked items a table can't show; `dropped`: schemes past MAX_SCHEMES.
    """
    ids = list(scheme_ids)
    columns, dates = [], []
    for sid in ids:
        link = catalog.scheme_link(sid)
        blocks = evidence_by_scheme.get(sid, [])
        fetched = [to_ist_date(b.fetched_at) for b in blocks if b.fetched_at is not None]
        when = max(fetched) if fetched else link.last_updated
        columns.append(ComparisonColumn(sid, catalog.schemes[sid].name, link.url, when))
        if when is not None:
            dates.append(when)

    rows: list[ComparisonRow] = []
    # Fact fields: one row each, in the order asked.
    facts = {sid: next((b.data.get("fields", {}) for b in evidence_by_scheme.get(sid, [])
                        if b.kind == "fact"), {}) for sid in ids}
    for f in intent.fields:
        cells = tuple(_fact_cell(f, facts[sid].get(f)) for sid in ids)
        rows.append(ComparisonRow(_sentence_case(FIELD_LABELS.get(f, f.replace("_", " "))),
                                  cells))

    # Metrics: one row per published label ("NAV", "3-year annualised return"), in the order
    # asked; a scheme without that figure gets a dash.
    for m in intent.metrics:
        per_scheme: dict[str, dict[str, dict]] = {}
        labels: dict[str, int] = {}  # label -> period sort key
        for sid in ids:
            block = next((b for b in evidence_by_scheme.get(sid, [])
                          if b.kind == "metric" and b.data.get("metric") == m.metric), None)
            for row in (block.data.get("rows", []) if block else []):
                row = {**row, "metric": m.metric}
                per_scheme.setdefault(sid, {})[row["label"]] = row
                labels.setdefault(row["label"], _period_key(row))
        if not labels:
            label = _metric_label(m)
            rows.append(ComparisonRow(label, tuple(NOT_PUBLISHED for _ in ids)))
            continue
        for label in sorted(labels, key=labels.get):  # stable: same period keeps page order
            found = [per_scheme.get(sid, {}).get(label) for sid in ids]
            dates_of = {r["as_of"] for r in found if r and r.get("as_of")}
            shared = len(dates_of) == 1 and all(found)
            cells = []
            for r in found:
                if r is None:
                    cells.append(NOT_PUBLISHED)
                    continue
                value = _metric_cell(r)
                if not shared and r.get("as_of"):
                    value += f" (as of {format_date(r['as_of'])})"
                cells.append(value)
            shown = f"{label} (as of {format_date(next(iter(dates_of)))})" if shared else label
            rows.append(ComparisonRow(_sentence_case(shown), tuple(cells)))

    notes = []
    if any(NOT_PUBLISHED in r.cells for r in rows):
        notes.append(f"{NOT_PUBLISHED} means the scheme's page doesn't publish that value.")
    if left_out:
        what = " and ".join(left_out)
        notes.append(f"{what[0].upper()}{what[1:]} can't be shown side by side; "
                     "ask about each scheme separately.")
    if dropped:
        names = ", ".join(catalog.schemes[s].name for s in dropped)
        notes.append(f"I can compare up to {MAX_SCHEMES} schemes at a time, so {names} "
                     "isn't included.")

    names = [c.name for c in columns]
    joined = ", ".join(names[:-1]) + " and " + names[-1]
    answer = (f"Here are the published figures for {joined}, side by side. They are restated "
              "from each scheme's Groww page, not ranked or judged, and are not a "
              "recommendation.")
    first = columns[0]
    return Reply("comparison", answer, first.url, min(dates) if dates else None,
                 Comparison(tuple(columns), tuple(rows), tuple(notes)))


_METRIC_NAMES = {"nav": "NAV", "aum": "Fund size (AUM)", "return": "return",
                 "sip_return": "SIP return", "category_average_return": "category average return"}
_UNIT_NAMES = {"y": "year", "m": "month", "w": "week", "d": "day"}


def _metric_label(m: MetricIntent) -> str:
    """Row label for a figure no compared scheme publishes ("3-year annualised return")."""
    base = _METRIC_NAMES.get(m.metric, m.metric.replace("_", " "))
    if not m.period:
        return base[0].upper() + base[1:]
    if m.period == "since_launch":
        period = "Since-launch"
    else:
        period = f"{m.period[:-1]}-{_UNIT_NAMES.get(m.period[-1], m.period[-1])}"
    basis = f" {m.basis}" if m.basis and m.basis not in ("point", "sip_annualised") else ""
    return f"{period}{basis} {base}"


# ------------------------------------------------------------------------------ fixed replies
def needs_second_scheme(catalog: Catalog, scheme_id: str | None) -> Reply:
    """A comparison that names only one covered scheme (the other is unknown or uncovered)."""
    if scheme_id is not None:
        name = catalog.schemes[scheme_id].name
        answer = (f"I can put published figures side by side for two to {MAX_SCHEMES} of the "
                  f"schemes I cover, but I only recognised {name} in your question. Name the "
                  "other scheme, for example \"compare the expense ratio of Kotak Multi Cap "
                  "and ICICI Prudential Large Cap\".")
    else:
        answer = (f"I can put published figures side by side for two to {MAX_SCHEMES} of the "
                  "schemes I cover. Name them, for example \"compare the expense ratio of "
                  "Kotak Multi Cap and ICICI Prudential Large Cap\".")
    return templates.Reply("clarify", answer, catalog.education_link("default").url,
                           catalog.education_link("default").last_updated)
