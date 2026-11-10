"""Everything else on a Groww scheme page: NAV, AUM, returns, category averages, ranks, risk ratios,
holdings, similar funds, fund-manager profiles, expense-ratio history and all remaining fields.

Field meanings were checked against the labels on the visible page (09 Nov 2026):
- `return_stats.returnN`        -> "Annualised returns" table (headline "3Y annualised"); used for
                                    periods over 1 year (shorter periods equal the absolute ones).
- `simple_return.returnN`       -> "Absolute returns" tab.
- `return_stats.cat_returnN`    -> "Category average" row; `rankN` / `rank_countN` -> "Rank" row.
- `sip_return.returnN`          -> annualised return of a monthly SIP (matches the page's SIP
                                    calculator); not shown as a number on the page.
- `stats[CATEGORY_AVG_RETURN]`  -> disagrees with the visible page, so it is NOT used.
- `category_info`               -> generic text that is wrong on every page ("Contra" for all
                                    equity funds, "Gilt" for all debt funds); stored, flagged
                                    unreliable.
- `analysis`                    -> Groww's PROS/CONS judgements; stored, flagged, never evidence.

All of this is data, not advice. Text sections built here state values with their basis and
as-of date and contain no evaluative wording.
"""

from dataclasses import dataclass, field
from datetime import date

from ingest import normalise as n

PERIODS = [
    ("1d", "1-day"),
    ("1w", "1-week"),
    ("1m", "1-month"),
    ("3m", "3-month"),
    ("6m", "6-month"),
    ("9m", "9-month"),
    ("1y", "1-year"),
    ("2y", "2-year"),
    ("3y", "3-year"),
    ("4y", "4-year"),
    ("5y", "5-year"),
    ("7y", "7-year"),
    ("10y", "10-year"),
]
LONG = {"2y", "3y", "4y", "5y", "7y", "10y"}
RANK_KEYS = {"3m": "3m", "6m": "6m", "1y": "1yr", "3y": "3yr", "5y": "5yr", "10y": "10yr"}
CAT_PERIODS = ["3m", "6m", "1y", "3y", "5y", "10y"]
# What the visible page shows (header, return table, absolute tab); the rest is page data only.
# Default when the page's returns table can't be read; normally the columns are read from the page
# ("Returns and rankings" shows different periods depending on the fund's age).
DEFAULT_TABLE_PERIODS = {"1y", "3y", "5y", "since_launch"}
RISK_RATIOS = [
    ("sharpe_ratio", "Sharpe ratio"),
    ("sortino_ratio", "Sortino ratio"),
    ("beta", "Beta"),
    ("alpha", "Alpha"),
    ("standard_deviation", "Standard deviation"),
    ("information_ratio", "Information ratio"),
]
# Handled by dedicated tables or known-bad; everything else becomes an attribute.
STRUCTURED_KEYS = {
    "holdings",
    "peerComparison",
    "fund_manager_details",
    "historic_fund_expense",
    "historic_exit_loads",
    "analysis",
    "return_stats",
    "simple_return",
    "sip_return",
    "stats",
}
UNRELIABLE_PREFIXES = ("category_info.", "nfo_risk", "stats.")


@dataclass
class Extended:
    metrics: list[dict] = field(default_factory=list)
    holdings: list[dict] = field(default_factory=list)
    peers: list[dict] = field(default_factory=list)
    managers: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    attributes: list[dict] = field(default_factory=list)
    analysis: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)


def _first(value) -> dict:
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else {}
    return value if isinstance(value, dict) else {}


def _metric(metric, period, basis, value, unit, label, as_of, displayed=True, rank_of=None):
    return {
        "metric": metric,
        "period": period,
        "basis": basis,
        "value": value,
        "unit": unit,
        "label": label,
        "as_of": as_of,
        "displayed": displayed,
        "rank_of": rank_of,
    }


def _metrics(d: dict, nav_date: date | None, category: str, shown: set[str]) -> list[dict]:
    out: list[dict] = []
    nav = n.to_float(d.get("nav"))
    if nav is not None:
        out.append(_metric("nav", None, "point", nav, "₹", "NAV", nav_date))
    rs = _first(d.get("return_stats"))
    aum = n.to_float(d.get("aum")) or n.to_float(rs.get("aum"))
    if aum is not None:
        out.append(_metric("aum", None, "point", aum, "₹ Cr", "Fund size (AUM)", nav_date))
    for key, label, unit in (
        ("groww_rating", "Groww rating (out of 5)", "rating"),
        ("base_expense_ratio", "Base expense ratio", "%"),
        ("portfolio_turnover", "Portfolio turnover", "%"),
        ("min_withdrawal", "Minimum withdrawal", "₹"),
        ("max_sip_investment", "Maximum SIP amount", "₹"),
        ("mini_additional_investment", "Minimum additional investment", "₹"),
        ("face_value", "Face value", "₹"),
    ):
        v = n.to_float(d.get(key))
        if v is not None:
            on_page = key == "groww_rating"
            out.append(_metric(key, None, "point", v, unit, label, nav_date, displayed=on_page))

    # Periods up to 1 year are identical in return_stats and simple_return, so annualised
    # values are taken only for longer periods; absolute values come from simple_return.
    for period, words in PERIODS:
        v = n.to_float(rs.get(f"return{period}"))
        if v is not None and period in LONG:
            out.append(
                _metric(
                    "return",
                    period,
                    "annualised",
                    v,
                    "%",
                    f"{words} annualised return",
                    nav_date,
                    displayed=period in shown,
                )
            )
    v = n.to_float(rs.get("return_since_created"))
    if v is not None:
        out.append(
            _metric(
                "return",
                "since_launch",
                "annualised",
                v,
                "%",
                "annualised return since launch",
                nav_date,
            )
        )

    simple = _first(d.get("simple_return"))
    for period, words in PERIODS:
        v = n.to_float(simple.get(f"return{period}"))
        if v is not None:
            out.append(
                _metric(
                    "return",
                    period,
                    "absolute",
                    v,
                    "%",
                    f"{words} absolute return",
                    nav_date,
                    displayed=period in shown and period not in LONG and period != "since_launch",
                )
            )
    v = n.to_float(simple.get("return_since_created"))
    if v is not None:
        out.append(
            _metric(
                "return",
                "since_launch",
                "absolute",
                v,
                "%",
                "absolute return since launch",
                nav_date,
                displayed=False,  # only in the click-to-show "Absolute returns" tab
            )
        )

    sip = _first(d.get("sip_return"))
    for period, words in PERIODS:
        v = n.to_float(sip.get(f"return{period}"))
        if v is not None:
            out.append(
                _metric(
                    "sip_return",
                    period,
                    "sip_annualised",
                    v,
                    "%",
                    f"{words} SIP return (annualised)",
                    nav_date,
                    displayed=False,
                )
            )

    for period in CAT_PERIODS:
        words = dict(PERIODS)[period]
        v = n.to_float(rs.get(f"cat_return{period}"))
        if v is not None:
            basis = "annualised" if period in LONG else "absolute"
            out.append(
                _metric(
                    "category_average_return",
                    period,
                    basis,
                    v,
                    "%",
                    f"{category} category average {words} return",
                    nav_date,
                    displayed=period in shown,
                )
            )
        rank = n.to_float(rs.get(f"rank{RANK_KEYS[period]}"))
        if rank is not None:
            of = rs.get(f"rank_count{RANK_KEYS[period]}") or None
            out.append(
                _metric(
                    "category_rank",
                    period,
                    "point",
                    rank,
                    "rank",
                    f"Groww rank within {category} for {words} return",
                    nav_date,
                    displayed=period in shown,
                    rank_of=of,
                )
            )
    v = n.to_float(rs.get("cat_return_since_launch"))
    if v is not None:
        out.append(
            _metric(
                "category_average_return",
                "since_launch",
                "annualised",
                v,
                "%",
                f"{category} category average annualised return since launch",
                nav_date,
            )
        )
    for period in ("1y", "3y", "5y", "since_launch"):
        v = n.to_float(rs.get(f"index_return{period}"))
        if v is not None:
            out.append(
                _metric(
                    "benchmark_return",
                    period,
                    "annualised",
                    v,
                    "%",
                    f"benchmark {period} return",
                    nav_date,
                    displayed=False,
                )
            )
    for key, label in RISK_RATIOS:
        v = n.to_float(rs.get(key))
        if v is not None:
            out.append(
                _metric(
                    key,
                    None,
                    "point",
                    v,
                    "%" if key == "standard_deviation" else "ratio",
                    label,
                    nav_date,
                    displayed=False,
                )
            )
    v = n.to_float(rs.get("mean_return"))
    if v is not None:
        out.append(
            _metric(
                "mean_return",
                None,
                "point",
                v,
                "%",
                "mean return (Groww data)",
                nav_date,
                displayed=False,
            )
        )
    return out


def _holdings(d: dict) -> list[dict]:
    rows = []
    for i, h in enumerate(d.get("holdings") or [], start=1):
        if not isinstance(h, dict) or not n.clean_text(h.get("company_name")):
            continue
        rows.append(
            {
                "position": i,
                "company_name": n.clean_text(h["company_name"]),
                "sector": n.clean_text(h.get("sector_name")),
                "instrument": n.clean_text(h.get("instrument_name")),
                "asset_type": n.clean_text(h.get("nature_name")),
                "credit_rating": n.clean_text(h.get("rating")),
                "market_value_cr": n.to_float(h.get("market_value")),
                "percent_of_assets": n.to_float(h.get("corpus_per")),
                "portfolio_date": n.parse_date(h.get("portfolio_date")),
            }
        )
    return rows


def _peers(d: dict) -> list[dict]:
    rows = []
    for i, p in enumerate(d.get("peerComparison") or [], start=1):
        if not isinstance(p, dict) or not n.clean_text(p.get("scheme_name")):
            continue
        cat = (
            " - ".join(
                x
                for x in (n.clean_text(p.get("category")), n.clean_text(p.get("sub_category")))
                if x
            )
            or None
        )
        rows.append(
            {
                "position": i,
                "peer_name": n.clean_text(p["scheme_name"]),
                "peer_groww_id": n.clean_text(p.get("search_id")),
                "fund_house": n.clean_text(p.get("fund_house")),
                "category": cat,
                "aum_cr": n.to_float(p.get("aum")),
                "expense_ratio": n.to_float(p.get("expense_ratio")),
                "return_1m": n.to_float(p.get("return1m")),
                "return_3m": n.to_float(p.get("return3m")),
                "return_6m": n.to_float(p.get("return6m")),
                "return_1y": n.to_float(p.get("return1y")),
                "return_3y": n.to_float(p.get("return3y")),
                "return_5y": n.to_float(p.get("return5y")),
                "return_since_launch": n.to_float(p.get("return_since_created")),
                "min_investment": n.to_float(p.get("min_investment")),
                "min_sip": n.to_float(p.get("min_sip_investment")),
                "riskometer": n.riskometer(p.get("risk")),
                "groww_rating": n.to_float(p.get("groww_rating")),
                "launch_date": n.parse_date(p.get("launch_date")),
                "data": p,
            }
        )
    return rows


def _managers(d: dict) -> list[dict]:
    rows = []
    for m in d.get("fund_manager_details") or []:
        if not isinstance(m, dict) or not n.clean_text(m.get("person_name")):
            continue
        rows.append(
            {
                "name": n.clean_text(m["person_name"]),
                "managing_since": n.parse_date(m.get("date_from")),
                "education": n.clean_text(m.get("education")),
                "experience": n.clean_text(m.get("experience")),
                "other_funds": [
                    f.get("scheme_name")
                    for f in m.get("funds_managed") or []
                    if isinstance(f, dict) and f.get("scheme_name")
                ]
                or None,
            }
        )
    return rows


def _history(d: dict) -> list[dict]:
    rows = []
    for h in d.get("historic_fund_expense") or []:
        if isinstance(h, dict):
            rows.append(
                {
                    "kind": "expense_ratio",
                    "as_on_date": n.parse_date(h.get("as_on_date")),
                    "value": n.to_float(h.get("expense_ratio")),
                    "text": None,
                    "extra": {"base_expense_ratio": h.get("base_expense_ratio")},
                }
            )
    for h in d.get("historic_exit_loads") or []:
        if isinstance(h, dict):
            rows.append(
                {
                    "kind": "exit_load",
                    "as_on_date": n.parse_date(h.get("as_on_date")),
                    "value": None,
                    "text": n.clean_text(h.get("note")),
                    "extra": {k: v for k, v in h.items() if k not in ("note", "as_on_date")},
                }
            )
    return rows


def _flatten(prefix: str, value, out: list[dict]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(value, list):
        if value and all(not isinstance(x, (dict, list)) for x in value):
            out.append({"key": prefix, "value": ", ".join(str(x) for x in value)})
        else:
            for i, v in enumerate(value):
                _flatten(f"{prefix}[{i}]", v, out)
    elif value is not None and value != "":
        text = n.strip_html(value) if isinstance(value, str) else str(value)
        if text:
            out.append({"key": prefix, "value": text})


def _attributes(d: dict) -> list[dict]:
    out: list[dict] = []
    for key, value in d.items():
        if key not in STRUCTURED_KEYS:
            _flatten(key, value, out)
    for row in out:
        row["reliable"] = not row["key"].startswith(UNRELIABLE_PREFIXES)
    return out


def _analysis(d: dict) -> list[dict]:
    return [
        {
            "analysis_type": a.get("analysis_type"),
            "subject": a.get("analysis_subject"),
            "description": n.clean_text(a.get("analysis_desc")),
            "data": n.clean_text(a.get("analysis_data")),
        }
        for a in d.get("analysis") or []
        if isinstance(a, dict)
    ]


def _fmt_date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "an unstated date"


def _sections(
    name: str,
    category: str,
    metrics: list[dict],
    holdings: list[dict],
    peers: list[dict],
    managers: list[dict],
    d: dict,
    nav_date: date | None,
) -> list[dict]:
    """Labelled, dated statements of the data, for retrieval. No evaluative wording."""
    by = {}
    for m in metrics:
        by.setdefault((m["metric"], m["basis"]), {})[m["period"]] = m
    when = _fmt_date(nav_date)
    sections = []

    point = []
    if m := by.get(("nav", "point"), {}).get(None):
        point.append(f"NAV: ₹{m['value']:,.2f} as of {when}.")
    if m := by.get(("aum", "point"), {}).get(None):
        point.append(f"Fund size (AUM): ₹{n.format_inr_decimal(m['value'])} Cr.")
    if m := by.get(("groww_rating", "point"), {}).get(None):
        point.append(f"Groww rating: {m['value']:g} out of 5 (Groww's own rating).")
    if point:
        sections.append({"heading": "NAV and fund size", "text": " ".join(point)})

    def series(metric, basis, title):
        rows = by.get((metric, basis), {})
        parts = [
            f"{dict(PERIODS).get(p, 'since launch' if p == 'since_launch' else p)} "
            f"{n.signed_pct(m['value'])}"
            for p, m in rows.items()
        ]
        if parts:
            sections.append(
                {
                    "heading": title,
                    "text": f"{title} as of {when}, as published by Groww: "
                    + "; ".join(parts)
                    + ".",
                }
            )

    series("return", "annualised", "Annualised returns")
    series("return", "absolute", "Absolute returns")
    series("sip_return", "sip_annualised", "SIP returns (annualised)")
    series(
        "category_average_return", "annualised", f"{category} category average annualised returns"
    )
    series(
        "category_average_return",
        "absolute",
        f"{category} category average returns for periods up to 1 year",
    )

    ranks = by.get(("category_rank", "point"), {})
    if ranks:
        parts = [
            f"{dict(PERIODS)[p]}: rank {m['value']:g}"
            + (f" of {m['rank_of']}" if m["rank_of"] else "")
            for p, m in ranks.items()
        ]
        sections.append(
            {
                "heading": "Rank within category",
                "text": f"Groww's rank of this scheme within {category}, by return, "
                f"as of {when}: " + "; ".join(parts) + ".",
            }
        )
    ratios = [
        f"{label}: {by[(key, 'point')][None]['value']:g}"
        for key, label in RISK_RATIOS
        if (key, "point") in by
    ]
    if ratios:
        sections.append(
            {
                "heading": "Risk measures",
                "text": f"Risk measures from Groww's data as of {when}: " + "; ".join(ratios) + ".",
            }
        )

    if holdings:
        pdate = _fmt_date(holdings[0]["portfolio_date"])
        for start in range(0, len(holdings), 25):
            chunk = holdings[start : start + 25]
            lines = [
                f"{h['company_name']} ({', '.join(x for x in (h['sector'], h['instrument']) if x)})"
                f" {h['percent_of_assets']:.2f}%"
                if h["percent_of_assets"] is not None
                else h["company_name"]
                for h in chunk
            ]
            sections.append(
                {
                    "heading": (
                        f"Portfolio holdings {start + 1}-{start + len(chunk)} of {len(holdings)}"
                    ),
                    "text": f"Portfolio as of {pdate}, {len(holdings)} holdings, share of assets: "
                    + "; ".join(lines)
                    + ".",
                }
            )

    for m in managers:
        bits = [f"Fund manager: {m['name']}"]
        if m["managing_since"]:
            bits.append(f"managing since {_fmt_date(m['managing_since'])}")
        text = ", ".join(bits) + "."
        if m["education"]:
            text += f" Education: {m['education']}"
        if m["experience"]:
            text += f" Experience: {m['experience']}"
        sections.append({"heading": f"Fund manager {m['name']}", "text": text})

    if peers:
        lines = []
        for p in peers:
            figs = [
                f"{lab} {n.signed_pct(p[key])}"
                for key, lab in (("return_1y", "1Y"), ("return_3y", "3Y"), ("return_5y", "5Y"))
                if p[key] is not None
            ]
            if p["aum_cr"] is not None:
                figs.append(f"AUM ₹{n.format_inr_decimal(p['aum_cr'])} Cr")
            lines.append(
                f"{p['peer_name']} ({p['fund_house']})" + (f": {', '.join(figs)}" if figs else "")
            )
        sections.append(
            {
                "heading": "Similar funds listed on Groww",
                "text": "Similar funds listed on this scheme's Groww page, with the figures "
                "Groww shows for them (3Y and 5Y annualised): " + "; ".join(lines) + ".",
            }
        )

    amc = d.get("amc_info") if isinstance(d.get("amc_info"), dict) else {}
    amc_bits = []
    if n.clean_text(amc.get("name")):
        amc_bits.append(f"Fund house: {n.clean_text(amc['name'])}.")
    if n.to_float(amc.get("aum")) is not None:
        amc_bits.append(f"Fund house AUM: ₹{n.format_inr_decimal(n.to_float(amc['aum']))} Cr.")
    if n.strip_html(amc.get("description")):
        amc_bits.append(n.strip_html(amc["description"]))
    if n.clean_text(amc.get("address")):
        amc_bits.append(f"Address: {n.clean_text(amc['address'])}.")
    if n.clean_text(amc.get("vro_website")):
        amc_bits.append(f"Website: {n.clean_text(amc['vro_website'])}.")
    if amc_bits:
        sections.append({"heading": "Fund house", "text": " ".join(amc_bits)})

    ids = [
        f"{label}: {n.clean_text(d.get(k))}"
        for k, label in (
            ("isin", "ISIN"),
            ("scheme_code", "AMFI scheme code"),
            ("plan_type", "Plan"),
            ("scheme_type", "Option"),
            ("registrar_agent", "Registrar"),
        )
        if n.clean_text(d.get(k))
    ]
    if ids:
        sections.append({"heading": "Identifiers", "text": "; ".join(ids) + "."})

    for s in sections:
        s["text"] = f"{name}. {s['text']}"
    return sections


def extract_extended(
    d: dict, scheme_name: str, category: str | None, table_periods: set[str] | None = None
) -> Extended:
    """`table_periods`: the period columns of the page's "Returns and rankings" table."""
    nav_date = n.parse_date(d.get("nav_date"))
    cat = n.clean_text(d.get("sub_category")) or category or "its category"
    metrics = _metrics(d, nav_date, cat, table_periods or DEFAULT_TABLE_PERIODS)
    holdings = _holdings(d)
    peers = _peers(d)
    managers = _managers(d)
    return Extended(
        metrics=metrics,
        holdings=holdings,
        peers=peers,
        managers=managers,
        history=_history(d),
        attributes=_attributes(d),
        analysis=_analysis(d),
        sections=_sections(scheme_name, cat, metrics, holdings, peers, managers, d, nav_date),
    )
