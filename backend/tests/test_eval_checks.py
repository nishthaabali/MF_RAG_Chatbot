"""The Phase 7 evaluation harness (eval/checks.py): matching rules and golden-file validity.

No database, index or API: these guard the measuring stick, not the pipeline.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
sys.path.insert(0, str(EVAL_DIR))

from checks import (  # noqa: E402
    CitationContext,
    TurnCheck,
    check_compliance,
    check_format,
    date_present,
    load_golden,
    load_sources,
    schema_problems,
    value_present,
)

from app.pipeline.templates import Reply  # noqa: E402

KOTAK_URL = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"
FETCHED = date(2026, 11, 9)


# ------------------------------------------------------------------------------ values
@pytest.mark.parametrize(("expected", "answer"), [
    ("0.70%", "The expense ratio is 0.7%."),
    ("0.70% | 0.7%", "It is 0.70 %."),
    ("₹1000", "The minimum SIP is ₹1,000."),
    ("₹1000", "The minimum SIP is Rs. 1000."),
    ("21.558", "The NAV was ₹21.56 as of 06 Nov 2026."),  # template rounds to 2 decimals
    ("21.558", "The NAV was ₹21.558."),
    ("145070.5", "AUM was ₹1,45,070.50 Cr."),
    ("19.73%", "The 3-year annualised return was +19.73%."),
    ("1 year | one year", "1% if redeemed within one year."),
    ("Very High", "Its riskometer level is very high."),
    ("50:25:25", "Nifty 500 Multicap 50:25:25 Total Return Index"),
])
def test_value_present(expected, answer):
    assert value_present(expected, answer)


@pytest.mark.parametrize(("expected", "answer"), [
    ("0.70%", "The expense ratio is 0.71%."),
    ("0.70%", "Exit load 0.70 within a year."),  # percent required
    ("19.73%", "The return was 19.7%."),  # a different (less precise) figure
    ("21.558", "The NAV was ₹21.55."),
    ("₹1000", "The minimum SIP is ₹100."),
    ("moderate", "Its riskometer level is high."),
])
def test_value_absent(expected, answer):
    assert not value_present(expected, answer)


def test_exact_mode_for_pages():
    assert value_present("21.558", '"nav":21.558', units=False, exact=True)
    assert not value_present("21.558", '"nav":21.56', units=False, exact=True)
    assert value_present("0.70%", '"expense_ratio":"0.70"', units=False, exact=True)


@pytest.mark.parametrize("text", [
    "as of 06 Nov 2026", "as of 6 November 2026", "on November 6, 2026", "as of 2026-11-06",
    "as of 6th Nov 2026", "as of 06/11/2026",
])
def test_date_present(text):
    assert date_present(date(2026, 11, 6), text)


def test_date_absent():
    assert not date_present(date(2026, 11, 6), "as of 09 Nov 2026")
    assert not date_present(date(2026, 11, 6), "in November 2026")


# ------------------------------------------------------------------------------ reply checks
@pytest.fixture
def ctx():
    return CitationContext(
        url_to_source={KOTAK_URL: "groww-kotak-multi-cap"},
        source_dates={"groww-kotak-multi-cap": FETCHED},
        scheme_urls={"kotak-multi-cap": KOTAK_URL},
        education_urls=frozenset(),
    )


def reply(answer, url=KOTAK_URL, when=FETCHED, kind="answer"):
    return Reply(kind, answer, url, when)


def test_good_reply_passes_format_and_compliance(ctx):
    tc = TurnCheck()
    check_format(reply("The expense ratio of Kotak Multi Cap Fund is 0.70%."), ctx, tc)
    check_compliance(reply("The expense ratio of Kotak Multi Cap Fund is 0.70%."), tc)
    assert tc.passed, tc.failures()


@pytest.mark.parametrize(("r", "failed"), [
    (reply("One. Two. Three. Four."), "format.sentences"),
    (reply("See https://example.com for it."), "format.no_url_in_answer"),
    (reply("Fine.", url="https://example.com/x"), "format.allowlisted"),
    (reply("Fine.", when=date(2026, 11, 1)), "format.footer"),
    (reply("Fine.", when=None), "format.footer"),
])
def test_format_failures(ctx, r, failed):
    tc = TurnCheck()
    check_format(r, ctx, tc)
    assert not tc.checks[failed]


def test_compliance_failures():
    tc = TurnCheck()
    check_compliance(reply("Its returns are strong, so you should invest now."), tc)
    assert not tc.checks["compliance.no_advice"]
    assert not tc.checks["compliance.no_judgement"]


def test_refusal_templates_are_not_flagged_for_naming_what_they_refuse():
    from app.pipeline.catalog import Catalog
    from app.pipeline.templates import advisory_refusal, performance_refusal

    catalog = Catalog.load()
    for r in (advisory_refusal(catalog), performance_refusal(catalog, "kotak-multi-cap")):
        tc = TurnCheck()
        check_compliance(r, tc)
        assert tc.passed, tc.failures()


# ------------------------------------------------------------------------------ golden file
def test_golden_file_is_valid():
    golden = load_golden()
    sources = load_sources()
    scheme_ids = {s["scheme_id"] for s in sources.values() if s.get("type") == "scheme_page"}
    assert schema_problems(golden, sources, scheme_ids) == []


def test_golden_bucket_sizes_match_the_plan():
    golden = load_golden()
    counts = {}
    for c in golden.cases:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    assert counts["scheme_facts"] >= 57  # ≈3 per scheme
    assert counts["general_facts"] >= 15
    assert counts["advisory"] >= 15
    assert counts["metrics"] >= 15
    assert counts["performance_judgement"] >= 10
    assert counts["pii"] >= 10
    assert counts["follow_ups"] >= 10
    covered = {c.turns[0].expect.get("scheme") for c in golden.cases
               if c.bucket == "scheme_facts"}
    sources = load_sources()
    assert covered >= {s["scheme_id"] for s in sources.values() if s.get("type") == "scheme_page"}


def test_schema_problems_are_reported():
    golden = load_golden()
    golden.cases.append(golden.cases[0])  # duplicate id
    assert any("duplicate id" in p for p in schema_problems(golden, load_sources(), set()))
