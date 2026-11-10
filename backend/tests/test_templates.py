"""Reply templates: every reply has <= 3 sentences, exactly one URL and a footer (FMT-01)."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.pipeline import templates as t
from app.pipeline.catalog import Catalog
from app.pipeline.text import split_sentences

URL = re.compile(r"https?://\S+|www\.\S+")
FETCHED = date(2026, 11, 9)


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: FETCHED for sid in cat.source_urls})


def all_replies(catalog):
    ids = list(catalog.schemes)
    return {
        "advisory": t.advisory_refusal(catalog, "choosing_a_fund"),
        "advisory_default": t.advisory_refusal(catalog),
        "performance": t.performance_refusal(catalog, "kotak-multi-cap"),
        "performance_long_name": t.performance_refusal(catalog, "absl-bse-total-market"),
        "performance_no_scheme": t.performance_refusal(catalog),
        "out_of_scope": t.out_of_scope(catalog),
        "not_covered": t.not_covered(catalog),
        "plan_not_covered": t.plan_not_covered(catalog, "kotak-multi-cap"),
        "greeting": t.greeting(catalog),
        "pii": t.pii_blocked(catalog),
        "unavailable": t.unavailable(catalog.scheme_link("kotak-multi-cap")),
        "no_evidence_scheme": t.no_evidence(catalog, "kotak-multi-cap"),
        "no_evidence_general": t.no_evidence(catalog),
        "clarify_none": t.clarify(catalog),
        "clarify_three": t.clarify(catalog, ids[:3]),
        "clarify_amc_seven": t.clarify(catalog, [s.id for s in catalog.schemes_of("absl")],
                                       amc="absl"),
        "fact_expense": t.fact(catalog, "kotak-multi-cap", "expense_ratio", "0.38%"),
        "fact_exit_nil": t.fact(catalog, "hdfc-nifty-next-50", "exit_load", "Nil"),
        "fact_exit": t.fact(catalog, "hdfc-pharma-healthcare", "exit_load",
                            "Exit load of 1%, if redeemed within 30 days."),
        "fact_managers": t.fact(catalog, "kotak-multi-cap", "fund_managers",
                                ["Abhishek Bisen", "Devender Singhal"]),
        "fact_launch": t.fact(catalog, "kotak-multi-cap", "launch_date", date(2021, 9, 29)),
        "metric_nav": t.metric(catalog, "hdfc-nifty-next-50", label="NAV", value=16.97,
                               unit="₹", as_of=date(2026, 11, 6), metric_name="nav"),
        "metric_return": t.metric(catalog, "kotak-multi-cap", label="3-year annualised return",
                                  value=17.06, unit="%", as_of=date(2026, 11, 6),
                                  metric_name="return"),
        "metric_rank": t.metric(catalog, "kotak-multi-cap",
                                label="Groww rank within Multi Cap for 3-year return",
                                value=8, unit="rank", rank_of=23, as_of=date(2026, 11, 6),
                                metric_name="category_rank"),
        "holdings": t.holdings(catalog, "ppfas-flexi-cap", [
            {"company_name": "HDFC Bank Ltd", "percent_of_assets": 7.754},
            {"company_name": "Power Grid Corporation of India Ltd", "percent_of_assets": 5.6},
            {"company_name": "ITC Ltd", "percent_of_assets": 5.49},
            {"company_name": "Coal India Ltd", "percent_of_assets": None},
            {"company_name": "ICICI Bank Ltd", "percent_of_assets": 5.24},
            {"company_name": "Not listed", "percent_of_assets": 1.0},
        ], date(2026, 9, 30)),
    }


def test_holdings_wording(catalog):
    r = all_replies(catalog)["holdings"]
    assert r.answer.startswith("The top 5 holdings of ")
    assert "as of 30 Sep 2026" in r.answer and "HDFC Bank Ltd (7.75%)" in r.answer
    assert "Coal India Ltd and ICICI" in r.answer and "Not listed" not in r.answer
    assert r.answer.endswith("ICICI Bank Ltd (5.24%), as published by Groww.")


def test_every_template_obeys_the_format(catalog):
    for name, reply in all_replies(catalog).items():
        assert 1 <= len(split_sentences(reply.answer)) <= 3, name
        assert not URL.search(reply.answer), name  # the URL is only on the Source line
        assert URL.findall(reply.rendered) == [reply.citation_url], name
        assert reply.citation_url in catalog.source_urls.values(), name  # on the allowlist
        assert "Last updated from sources: 09 Nov 2026" in reply.rendered, name
        assert reply.to_dict()["last_updated"] == "2026-11-09", name


def test_rendered_and_structured_fields_agree(catalog):  # FMT-07
    reply = t.fact(catalog, "kotak-multi-cap", "expense_ratio", "0.38%")
    data = reply.to_dict()
    assert data["rendered"] == (
        f"{data['answer']}\n\nSource: {data['citation_url']}\n\n"
        "Last updated from sources: 09 Nov 2026"
    )
    assert data["type"] == "answer"


def test_missing_date_never_prints_none():  # FMT-03
    reply = t.pii_blocked(Catalog.load())  # no fetch dates loaded
    assert "None" not in reply.rendered and "Last updated" not in reply.rendered
    assert reply.to_dict()["last_updated"] is None


def test_scheme_replies_cite_the_groww_page(catalog):
    groww = catalog.schemes["kotak-multi-cap"].url
    assert t.performance_refusal(catalog, "kotak-multi-cap").citation_url == groww
    assert t.no_evidence(catalog, "kotak-multi-cap").citation_url == groww
    assert t.fact(catalog, "kotak-multi-cap", "expense_ratio", "0.38%").citation_url == groww


def test_refusals_cite_education_pages(catalog):
    for reply in (t.advisory_refusal(catalog), t.out_of_scope(catalog), t.greeting(catalog),
                  t.pii_blocked(catalog), t.clarify(catalog)):
        assert catalog.source_types[_source_of(catalog, reply.citation_url)] != "scheme_page"


def _source_of(catalog, url):
    return next(sid for sid, u in catalog.source_urls.items() if u == url)


def test_fact_wording(catalog):
    assert t.fact(catalog, "kotak-multi-cap", "expense_ratio", "0.38%").answer == (
        "The expense ratio of Kotak Multi Cap Fund is 0.38%."
    )
    assert t.fact(catalog, "hdfc-nifty-next-50", "exit_load", "Nil").answer == (
        "There is no exit load for HDFC NIFTY Next 50 Index Fund."
    )
    assert t.fact(catalog, "hdfc-pharma-healthcare", "exit_load",
                  "Exit load of 1%, if redeemed within 30 days.").answer == (
        "The exit load of HDFC Pharma And Healthcare Fund is 1%, if redeemed within 30 days."
    )
    assert t.fact(catalog, "kotak-multi-cap", "lock_in", "No lock-in").answer == (
        "Kotak Multi Cap Fund has no lock-in period."
    )
    assert t.fact(catalog, "kotak-multi-cap", "launch_date", date(2021, 9, 29)).answer == (
        "The launch date of Kotak Multi Cap Fund is 29 Sep 2021."
    )


def test_fact_uses_the_fact_rows_fetch_date(catalog):
    fetched = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)  # 01:30 on 10 Nov in IST
    reply = t.fact(catalog, "kotak-multi-cap", "expense_ratio", "0.38%", fetched_at=fetched)
    assert reply.last_updated == date(2026, 11, 10)


def test_metric_wording(catalog):
    replies = all_replies(catalog)
    assert replies["metric_nav"].answer == (
        "The NAV of HDFC NIFTY Next 50 Index Fund was ₹16.97 as of 06 Nov 2026, "
        "as published by Groww."
    )
    assert replies["metric_return"].answer == (
        "The 3-year annualised return of Kotak Multi Cap Fund was +17.06% as of 06 Nov 2026, "
        "as published by Groww."
    )
    assert replies["metric_rank"].answer == (
        "Groww ranks Kotak Multi Cap Fund 8 of 23 within Multi Cap for 3-year return as of "
        "06 Nov 2026."
    )
    aum = t.metric(catalog, "hdfc-nifty-next-50", label="Fund size (AUM)", value=2620.5,
                   unit="₹ Cr", as_of=date(2026, 11, 6), metric_name="aum")
    assert "fund size (AUM) of HDFC NIFTY Next 50 Index Fund was ₹2,620.50 Cr" in aum.answer


def test_clarify_lists_candidates(catalog):
    reply = t.clarify(catalog, ["absl-large-cap", "nippon-large-cap", "icici-large-cap"])
    assert reply.answer == (
        "Which scheme do you mean: Aditya Birla Sun Life Large Cap Fund, Nippon India Large "
        "Cap Fund or ICICI Prudential Large Cap Fund?"
    )
    many = t.clarify(catalog, [s.id for s in catalog.schemes_of("absl")], amc="absl")
    assert many.answer.startswith("Which Aditya Birla Sun Life scheme do you mean? I cover 7")


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("Should I invest in ELSS for tax saving?", "elss"),
        ("Which is better for a monthly SIP?", "sip_basics"),
        ("Is it safe?", "risk"),
        ("HDFC vs PPFAS", "comparing_funds"),
        ("Should I invest now?", "choosing_a_fund"),
        ("hmm", "default"),
    ],
)
def test_education_topic(text, topic):
    assert t.pick_education_topic(text) == topic


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("NAV is ₹16.97. Expense ratio is 0.38%.", 2),
        ("Exit load is 1% (approx.) for 1 yr. There is no lock-in.", 2),
        ("Managed by Sharmila D. Silva. Launched in 2013.", 2),
        ("One sentence without a full stop", 1),
    ],
)
def test_sentence_splitter(text, count):
    assert len(split_sentences(text)) == count
