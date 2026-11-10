"""Extract scheme facts from a Groww scheme page.

Strategy, most reliable first (architecture §5.2):
1. The server-rendered `__NEXT_DATA__` JSON (`props.pageProps.mfServerSideData`).
2. Label-based lookup in the visible page text ("Expense ratio" -> next text node, ...).
3. Otherwise the field is None. Never guess.

Everything else on the page (NAV, AUM, returns, category averages, ranks, holdings, similar funds,
manager profiles, history and all remaining fields) is extracted by `groww_extended`. Those are
stored as facts; only interpretation (judging, ranking, recommending) is restricted, downstream.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ingest import normalise as n
from ingest.parsers.groww_extended import Extended, extract_extended

log = logging.getLogger(__name__)

FACT_FIELDS = (
    "scheme_name",
    "category",
    "expense_ratio",
    "expense_ratio_value",
    "exit_load",
    "min_sip",
    "min_sip_value",
    "min_lumpsum",
    "min_lumpsum_value",
    "lock_in",
    "riskometer",
    "benchmark",
    "fund_managers",
    "launch_date",
)
# Fields that must be present for a scheme; a NULL here needs a review note (Phase 2 exit).
KEY_FIELDS = ("expense_ratio", "exit_load", "min_sip", "riskometer", "benchmark")

_RISK_NODE = re.compile(
    r"^(Low|Low to Moderate|Moderate|Moderately High|High|Very High) Risk$", re.I
)


_PERIOD_LABELS = {
    "1M": "1m",
    "3M": "3m",
    "6M": "6m",
    "1Y": "1y",
    "3Y": "3y",
    "5Y": "5y",
    "10Y": "10y",
    "All": "since_launch",
}


def _num(text: str) -> float | None:
    text = text.replace("%", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None  # "--"


def _returns_table(nodes: list[str]) -> dict | None:
    """Read the visible "Returns and rankings" table: its period columns (which vary with the
    fund's age) and the Fund returns / Category average / Rank rows."""
    try:
        start = nodes.index("Returns and rankings")
        name = nodes.index("Name", start)
        fund = nodes.index("Fund returns", name)
    except ValueError:
        return None
    cols = [_PERIOD_LABELS[t] for t in nodes[name + 1 : fund] if t in _PERIOD_LABELS]
    if not cols:
        return None
    k = len(cols)

    def row_after(label_prefix: str, frm: int) -> tuple[list[float | None], int] | None:
        for i in range(frm, min(frm + 12, len(nodes))):
            if nodes[i].startswith(label_prefix):
                j = i + 1
                if label_prefix != "Fund returns":  # skip "(", category name, ")"
                    while j < len(nodes) and nodes[j] != ")":
                        j += 1
                    j += 1
                return [_num(v) for v in nodes[j : j + k]], j + k
        return None

    table = {"columns": cols}
    pos = fund
    for key, label in (
        ("fund", "Fund returns"),
        ("category", "Category average"),
        ("rank", "Rank"),
    ):
        found = row_after(label, pos)
        if found is None:
            break
        table[key], pos = found
    return table


def _cross_check(metrics: list[dict], table: dict) -> list[str]:
    """Compare stored values with the page's table; mark disagreements as not displayed."""
    warnings = []
    rows = {"return": "fund", "category_average_return": "category", "category_rank": "rank"}
    for m in metrics:
        row = rows.get(m["metric"])
        if row is None or row not in table or m["period"] not in table["columns"]:
            continue
        if m["metric"] == "return" and m["basis"] != (
            "annualised"
            if m["period"] in {"2y", "3y", "4y", "5y", "7y", "10y", "since_launch"}
            else "absolute"
        ):
            continue
        shown = table[row][table["columns"].index(m["period"])]
        tolerance = 0 if row == "rank" else 0.051  # page rounds returns to 1 decimal
        if shown is None or abs(shown - m["value"]) > tolerance:
            m["displayed"] = False
            if shown is not None:
                warnings.append(
                    f"{m['label']}: Groww's data says {m['value']:g} but the page shows "
                    f"{shown:g}; stored value kept as data, marked not displayed"
                )
        else:
            m["displayed"] = True
    return warnings


class GrowwParseError(ValueError):
    """The page is not a usable Groww scheme page (block page, wrong layout)."""


@dataclass
class GrowwParse:
    facts: dict
    sections: list[dict]
    extracted_from: str  # json | dom | mixed
    warnings: list[str] = field(default_factory=list)
    data: dict | None = None  # the complete embedded data object, retained verbatim
    extended: Extended | None = None  # metrics, holdings, peers, managers, history, ...


def _next_data(tree: HTMLParser) -> dict | None:
    node = tree.css_first("script#__NEXT_DATA__")
    if node is None:
        return None
    try:
        data = json.loads(node.text())
        return data["props"]["pageProps"]["mfServerSideData"] or None
    except (ValueError, KeyError, TypeError):
        return None


def _meta_risk(meta_desc: object) -> str | None:
    m = re.search(r"Risk is ([A-Za-z ]+?)\s*\.?\s*$", n.clean_text(meta_desc) or "")
    return n.riskometer(m.group(1)) if m else None


def _from_json(d: dict) -> dict:
    er_text, er_value = n.expense_ratio(d.get("expense_ratio"))
    if d.get("sip_allowed", True):
        sip_text, sip_value = n.amount(d.get("min_sip_investment"))
    else:
        sip_text, sip_value = None, None
    lump_text, lump_value = n.amount(d.get("min_investment_amount"))
    parts = (n.clean_text(d.get("category")), n.clean_text(d.get("sub_category")))
    category = " - ".join(p for p in parts if p) or None
    managers = [
        m["person_name"].strip()
        for m in d.get("fund_manager_details") or []
        if isinstance(m, dict) and n.clean_text(m.get("person_name"))
    ]
    if not managers and n.clean_text(d.get("fund_manager")):
        managers = [n.clean_text(d.get("fund_manager"))]
    return {
        "scheme_name": n.clean_text(d.get("scheme_name")),
        "category": category,
        "expense_ratio": er_text,
        "expense_ratio_value": er_value,
        "exit_load": n.clean_text(d.get("exit_load")),
        "min_sip": sip_text,
        "min_sip_value": sip_value,
        "min_lumpsum": lump_text,
        "min_lumpsum_value": lump_value,
        "lock_in": n.lock_in(d["lock_in"]) if "lock_in" in d else None,
        # `nfo_risk` holds the launch-time rating and is stale on many pages; the meta description
        # ("... Risk is Very High") matches the riskometer the page displays.
        "riskometer": _meta_risk(d.get("meta_desc")),
        "benchmark": n.clean_text(d.get("benchmark_name")) or n.clean_text(d.get("benchmark")),
        "fund_managers": managers or None,
        "launch_date": n.parse_date(d.get("launch_date")) or n.parse_date(d.get("allotment_date")),
    }


def _text_nodes(tree: HTMLParser, keep_placeholders: bool = False) -> list[str]:
    """Visible text nodes in order. With keep_placeholders, "--" cells are kept so table rows
    stay aligned with their columns."""
    body = tree.body
    if body is None:
        return []
    for tag in ("script", "style", "noscript", "svg"):
        for node in body.css(tag):
            node.decompose()
    nodes = []
    for node in body.traverse(include_text=True):
        if node.tag == "-text":
            raw = " ".join(node.text().split())
            text = n.clean_text(raw)
            if text:
                nodes.append(text)
            elif keep_placeholders and raw in ("--", "-", "—"):
                nodes.append("--")
    return nodes


def _after_label(nodes: list[str], label: str, skip_prefix: str | None = None) -> str | None:
    for i, text in enumerate(nodes):
        if text.casefold() == label.casefold():
            for nxt in nodes[i + 1 : i + 4]:
                if skip_prefix and nxt.startswith(skip_prefix):
                    continue
                return nxt
    return None


def _from_dom(tree: HTMLParser) -> dict:
    nodes = _text_nodes(tree)
    er_text, er_value = n.expense_ratio(_after_label(nodes, "Expense ratio"))
    sip_text, sip_value = n.amount(_after_label(nodes, "Min. for SIP"))
    risk = next((n.riskometer(t) for t in nodes if _RISK_NODE.match(t)), None)
    h1 = tree.css_first("h1")
    return {
        "scheme_name": n.clean_text(h1.text()) if h1 else None,
        "expense_ratio": er_text,
        "expense_ratio_value": er_value,
        "exit_load": _after_label(nodes, "Exit load", skip_prefix="A fee payable"),
        "min_sip": sip_text,
        "min_sip_value": sip_value,
        "riskometer": risk,
        "benchmark": _after_label(nodes, "Fund benchmark"),
    }


def _sections(f: dict, d: dict | None) -> list[dict]:
    name = f.get("scheme_name") or "This scheme"
    sections = []
    if d and n.clean_text(d.get("description")):
        sections.append({"heading": "Investment objective", "text": n.clean_text(d["description"])})
    lines = []
    if f.get("category"):
        lines.append(f"Category: {f['category']}.")
    if d and n.clean_text(d.get("fund_house")):
        lines.append(f"Fund house: {n.clean_text(d['fund_house'])}.")
    if f.get("benchmark"):
        lines.append(f"Benchmark: {f['benchmark']}.")
    if f.get("riskometer"):
        lines.append(f"Riskometer: {f['riskometer']} risk.")
    if f.get("launch_date"):
        lines.append(f"Launch date: {f['launch_date'].strftime('%d %b %Y')}.")
    if f.get("fund_managers"):
        lines.append(f"Fund manager(s): {', '.join(f['fund_managers'])}.")
    if d and n.clean_text(d.get("registrar_agent")):
        lines.append(f"Registrar and transfer agent: {n.clean_text(d['registrar_agent'])}.")
    if lines:
        sections.append({"heading": "Scheme details", "text": " ".join(lines)})
    costs = []
    if f.get("expense_ratio"):
        costs.append(f"Expense ratio: {f['expense_ratio']}.")
    if f.get("exit_load"):
        costs.append(f"Exit load: {f['exit_load']}")
    if d and n.clean_text(d.get("stamp_duty")):
        costs.append(f"Stamp duty: {n.clean_text(d['stamp_duty'])}.")
    if costs:
        sections.append({"heading": "Costs and exit load", "text": " ".join(costs)})
    inv = []
    if f.get("min_sip"):
        inv.append(f"Minimum SIP investment: {f['min_sip']}.")
    if f.get("min_lumpsum"):
        inv.append(f"Minimum lumpsum investment: {f['min_lumpsum']}.")
    if f.get("lock_in"):
        inv.append(f"Lock-in: {f['lock_in']}.")
    if inv:
        sections.append({"heading": "Investment limits and lock-in", "text": " ".join(inv)})
    for s in sections:
        s["text"] = f"{name}. {s['text']}" if not s["text"].startswith(name) else s["text"]
    return sections


def parse_groww(html: str) -> GrowwParse:
    tree = HTMLParser(html)
    data = _next_data(tree)
    json_facts = _from_json(data) if data else {}
    dom_facts = _from_dom(HTMLParser(html))
    warnings: list[str] = []

    facts: dict = {}
    used = set()
    for key in FACT_FIELDS:
        jv, dv = json_facts.get(key), dom_facts.get(key)
        if jv is not None:
            facts[key] = jv
            used.add("json")
            if key in ("expense_ratio", "riskometer", "min_sip") and dv is not None and dv != jv:
                warnings.append(f"{key}: page JSON says {jv!r} but visible page says {dv!r}")
        elif dv is not None:
            facts[key] = dv
            used.add("dom")
        else:
            facts[key] = None

    if not used or not facts.get("scheme_name"):
        raise GrowwParseError("no scheme data found (not a Groww scheme page, or layout changed)")

    if facts["expense_ratio"] is None:
        raw = (data or {}).get("expense_ratio") or _after_label(
            _text_nodes(HTMLParser(html)), "Expense ratio"
        )
        if n.parse_percent(raw) is not None:
            warnings.append(
                f"expense ratio shown as {n.parse_percent(raw)}% is outside the plausible "
                f"0-{n.MAX_EXPENSE_RATIO}% range (likely a source data error); stored as NULL"
            )

    if data:
        if (n.clean_text(data.get("plan_type")) or "").casefold() != "direct":
            warnings.append(f"plan_type is {data.get('plan_type')!r}, expected Direct")
        if (n.clean_text(data.get("scheme_type")) or "").casefold() != "growth":
            warnings.append(f"scheme_type is {data.get('scheme_type')!r}, expected Growth")

    extracted_from = "mixed" if len(used) > 1 else used.pop()
    sections = _sections(facts, data)
    extended = None
    if data:
        table = _returns_table(_text_nodes(HTMLParser(html), keep_placeholders=True))
        periods = set(table["columns"]) if table else None
        extended = extract_extended(data, facts["scheme_name"], facts.get("category"), periods)
        if table:
            warnings += _cross_check(extended.metrics, table)
        sections += extended.sections
    return GrowwParse(facts, sections, extracted_from, warnings, data, extended)


def missing_key_fields(facts: dict) -> list[str]:
    return [k for k in KEY_FIELDS if facts.get(k) is None]


def as_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
