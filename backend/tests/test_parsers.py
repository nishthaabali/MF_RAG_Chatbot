"""Snapshot tests against real (trimmed) pages saved on 2026-11-09.

If Groww or AMFI changes its layout, these fail loudly instead of the parser silently
producing wrong facts.
"""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from ingest.parsers import GrowwParseError, parse_generic, parse_groww
from ingest.parsers.groww import missing_key_fields

FIXTURES = Path(__file__).parent / "fixtures"
GROWW = (FIXTURES / "groww_kotak_multi_cap.html").read_text(encoding="utf-8")
AMFI = (FIXTURES / "amfi_exit_load.html").read_text(encoding="utf-8")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>.*?</script>', re.S)

KOTAK = {
    "scheme_name": "Kotak Multi Cap Fund Direct Growth",
    "category": "Equity - Multi Cap",
    "expense_ratio": "0.70%",
    "expense_ratio_value": 0.7,
    "exit_load": "Exit load for units in excess of 10% of the investment,1% will be charged "
    "for redemption within 1 year.",
    "min_sip": "₹100",
    "min_sip_value": 100.0,
    "min_lumpsum": "₹100",
    "min_lumpsum_value": 100.0,
    "lock_in": "No lock-in",
    "riskometer": "Very High",
    "benchmark": "Nifty 500 Multicap 50:25:25 Total Return Index",
    "fund_managers": ["Abhishek Bisen", "Devender Singhal"],
    "launch_date": date(2021, 9, 29),
}


def _with_json(html: str, **changes) -> str:
    """Return the fixture with fields in mfServerSideData changed (None removes the key)."""
    m = NEXT_DATA.search(html)
    blob = m.group(0)
    start = blob.index(">") + 1
    data = json.loads(blob[start : -len("</script>")])
    mf = data["props"]["pageProps"]["mfServerSideData"]
    for key, value in changes.items():
        if value is None:
            mf.pop(key, None)
        else:
            mf[key] = value
    new = blob[:start] + json.dumps(data, ensure_ascii=False) + "</script>"
    return html[: m.start()] + new + html[m.end() :]


# ------------------------------------------------------------------ Groww: embedded JSON path


def test_groww_snapshot_every_field():
    parsed = parse_groww(GROWW)
    assert parsed.extracted_from == "json"
    assert parsed.warnings == []
    for key, expected in KOTAK.items():
        assert parsed.facts[key] == expected, key
    assert missing_key_fields(parsed.facts) == []


def test_groww_riskometer_comes_from_meta_not_stale_nfo_risk():
    # nfo_risk is the launch-time rating and was stale on 10 of 19 live pages.
    html = _with_json(GROWW, nfo_risk="Moderately High")
    assert parse_groww(html).facts["riskometer"] == "Very High"


def _metric(parsed, metric, period=None, basis=None):
    hits = [
        m
        for m in parsed.extended.metrics
        if m["metric"] == metric
        and m["period"] == period
        and (basis is None or m["basis"] == basis)
    ]
    assert len(hits) == 1, (metric, period, basis, hits)
    return hits[0]


def test_groww_full_page_data_is_retained():
    parsed = parse_groww(GROWW)
    assert parsed.data["isin"] == "INF174KA1HV3"
    x = parsed.extended
    assert len(x.holdings) == 77
    assert len(x.peers) == 5
    assert [m["name"] for m in x.managers] == ["Abhishek Bisen", "Devender Singhal"]
    assert {h["kind"] for h in x.history} == {"expense_ratio", "exit_load"}
    assert any(a["key"] == "isin" and a["value"] == "INF174KA1HV3" for a in x.attributes)


def test_groww_metrics_match_the_visible_page_labels():
    """Values and bases checked against the live page on 09 Nov 2026."""
    p = parse_groww(GROWW)
    as_of = date(2026, 11, 6)
    assert _metric(p, "nav")["value"] == 21.558
    assert _metric(p, "nav")["as_of"] == as_of
    assert _metric(p, "aum")["value"] == 30335.28  # "Fund size (AUM) ₹30,335.28 Cr"
    # "Annualised returns" table and "3Y annualised" headline
    assert _metric(p, "return", "3y", "annualised")["value"] == 17.06
    assert _metric(p, "return", "5y", "annualised")["value"] == 16.43
    assert _metric(p, "return", "since_launch", "annualised")["value"] == 16.23
    # "Absolute returns" tab
    assert _metric(p, "return", "1y", "absolute")["value"] == 2.7
    assert _metric(p, "return", "3y", "absolute")["value"] == 60.46
    # Category average and rank rows
    assert _metric(p, "category_average_return", "1y")["value"] == 5.12
    assert _metric(p, "category_average_return", "3y")["value"] == 15.34
    rank = _metric(p, "category_rank", "3y")
    assert (rank["value"], rank["rank_of"]) == (8, 23)
    assert _metric(p, "sip_return", "3y", "sip_annualised")["displayed"] is False
    assert _metric(p, "sharpe_ratio")["value"] == 0.6


def test_groww_no_duplicate_returns_for_short_periods():
    p = parse_groww(GROWW)
    one_year = [m for m in p.extended.metrics if m["metric"] == "return" and m["period"] == "1y"]
    assert len(one_year) == 1 and one_year[0]["basis"] == "absolute"


def test_groww_category_average_ignores_the_stale_stats_block():
    # stats[CATEGORY_AVG_RETURN] says 0.93 for 1Y; the page shows 5.1 (return_stats).
    assert _metric(parse_groww(GROWW), "category_average_return", "1y")["value"] != 0.93


def test_groww_holdings_and_peers_are_structured():
    x = parse_groww(GROWW).extended
    top = x.holdings[0]
    assert top["company_name"] == "HDFC Bank Ltd"
    assert top["sector"] == "Financial"
    assert top["percent_of_assets"] == 5.66
    assert top["portfolio_date"] == date(2026, 9, 30)  # 2026-09-29T18:30Z is 30 Sep in IST
    peer = x.peers[0]
    assert peer["peer_name"] == "Axis Multicap Fund Direct Growth"
    assert peer["return_3y"] == 19.47


def test_groww_judgements_and_bad_fields_are_flagged():
    x = parse_groww(GROWW).extended
    assert x.analysis and all(a["description"] for a in x.analysis)
    bad = [a for a in x.attributes if a["key"].startswith("category_info.")]
    assert bad and not any(a["reliable"] for a in bad)


def test_groww_text_sections_state_labelled_dated_figures_without_judgement():
    parsed = parse_groww(GROWW)
    by_heading = {s["heading"]: s["text"] for s in parsed.sections}
    assert "NAV: ₹21.56 as of 06 Nov 2026" in by_heading["NAV and fund size"]
    assert "3-year +17.06%" in by_heading["Annualised returns"]
    assert "3-year +60.46%" in by_heading["Absolute returns"]
    assert "rank 8 of 23" in by_heading["Rank within category"]
    assert any(h.startswith("Portfolio holdings 1-25 of 77") for h in by_heading)
    text = " ".join(by_heading.values()).lower()
    for judgement in ("pros", "cons", "better", "higher than category", "good", "recommend"):
        assert not re.search(rf"\b{judgement}\b", text), judgement
    assert all(s["text"].startswith(KOTAK["scheme_name"]) for s in parsed.sections)


def test_groww_implausible_expense_ratio_is_null_with_note():
    html = _with_json(GROWW, expense_ratio="6.17")
    # Remove the visible value too, as on the live ABSL BSE Total Market page.
    html = html.replace(">0.70%<", ">6.17%<")
    parsed = parse_groww(html)
    assert parsed.facts["expense_ratio"] is None
    assert any("6.17" in w and "NULL" in w for w in parsed.warnings)


def test_groww_disagreement_between_json_and_page_is_reported():
    html = _with_json(GROWW, min_sip_investment=500)
    parsed = parse_groww(html)
    assert parsed.facts["min_sip"] == "₹500"  # JSON wins
    assert any(w.startswith("min_sip") for w in parsed.warnings)


def test_groww_non_direct_plan_is_flagged():
    html = _with_json(GROWW, plan_type="Regular")
    assert any("plan_type" in w for w in parse_groww(html).warnings)


# ---------------------------------------------------------------- Groww: visible-page fallback


def test_groww_falls_back_to_visible_labels_without_json():
    parsed = parse_groww(NEXT_DATA.sub("", GROWW))
    assert parsed.extracted_from == "dom"
    assert parsed.facts["expense_ratio"] == "0.70%"
    assert parsed.facts["min_sip"] == "₹100"
    assert parsed.facts["riskometer"] == "Very High"
    assert parsed.facts["benchmark"] == KOTAK["benchmark"]
    assert parsed.facts["scheme_name"]


def test_groww_missing_label_gives_null_not_a_wrong_value():
    html = NEXT_DATA.sub("", GROWW).replace(">Expense ratio<", ">Something else<")
    parsed = parse_groww(html)
    assert parsed.facts["expense_ratio"] is None
    assert parsed.facts["expense_ratio_value"] is None
    assert "expense_ratio" in missing_key_fields(parsed.facts)


def test_groww_missing_json_field_gives_null_when_page_lacks_it_too():
    html = _with_json(GROWW, benchmark=None, benchmark_name=None).replace(
        ">Fund benchmark<", ">Removed<"
    )
    assert parse_groww(html).facts["benchmark"] is None


def test_non_scheme_page_raises():
    with pytest.raises(GrowwParseError):
        parse_groww("<html><body><h2>Access denied</h2></body></html>")


# ----------------------------------------------------------------------------- generic pages


def test_generic_snapshot_amfi_exit_load():
    doc = parse_generic(AMFI)
    assert doc["title"] == "Know the cost incurred during redemption of Mutual Funds"
    headings = [s["heading"] for s in doc["sections"]]
    assert "What costs does one incur while redeeming Mutual Fund units?" in headings
    text = " ".join(s["text"] for s in doc["sections"])
    assert "an exit load is levied" in text
    assert text.lower().count("exit load") >= 5


def test_generic_drops_scripts_navigation_and_duplicates():
    html = """
    <html><head><title> T </title></head><body>
      <nav><p>Menu item</p></nav>
      <h2>Exit load</h2>
      <div><p>An exit load is charged on <b>early</b> redemption.</p></div>
      <p>An exit load is charged on <b>early</b> redemption.</p>
      <script>var x = "secret";</script>
      <ul><li><p>Nested paragraph in list</p></li></ul>
      <footer><p>Copyright</p></footer>
    </body></html>"""
    doc = parse_generic(html)
    assert doc["title"] == "T"
    assert doc["sections"] == [
        {
            "heading": "Exit load",
            "text": "An exit load is charged on early redemption.\nNested paragraph in list",
        }
    ]
