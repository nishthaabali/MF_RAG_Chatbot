"""Scheme resolver and field intent (architecture §6.3, docs/edge-case.md §6)."""

import pytest

from app.pipeline.catalog import Catalog
from app.pipeline.scheme_resolver import (
    MetricIntent,
    SchemeResolver,
    detect_intent,
    is_concept_question,
    mentions_other_plan,
)

CATALOG = Catalog.load()


@pytest.fixture(scope="module")
def resolver():
    return SchemeResolver(CATALOG)


ALL_ALIASES = [
    (scheme.id, alias)
    for scheme in CATALOG.schemes.values()
    for alias in (*scheme.aliases, scheme.name)
]


@pytest.mark.parametrize(("scheme_id", "alias"), ALL_ALIASES)
def test_every_alias_resolves(resolver, scheme_id, alias):  # RES-11, RES-12, RES-13
    for text in (f"What is the expense ratio of {alias}?", alias.lower(), alias.upper()):
        res = resolver.resolve(text)
        assert res.status == "resolved", text
        assert res.scheme_ids == (scheme_id,), text


@pytest.mark.parametrize(
    ("text", "scheme_id"),
    [
        ("What's the TER of HDFC's Flexi-Cap Fund?", "hdfc-flexi-cap"),
        ("what is Fund size of hdfc-nifty-next-50-index-fund-direct-growth", "hdfc-nifty-next-50"),
        ("HDFC NIFTY Next 50 Index Fund Direct Growth expense ratio", "hdfc-nifty-next-50"),
        ("HDFC Pharma & Healthcare exit load", "hdfc-pharma-healthcare"),
        ("kotak multicap", "kotak-multi-cap"),
        ("kotak mutli cap exit load", "kotak-multi-cap"),  # CLS-40 typo
        ("parag parikh flexy cap", "ppfas-flexi-cap"),
        ("Edelweiss Mid & Small Cap", "edelweiss-mid-cap"),
    ],
)
def test_spelling_variants(resolver, text, scheme_id):
    res = resolver.resolve(text)
    assert res.status == "resolved" and res.scheme_id == scheme_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the large cap fund", {"absl-large-cap", "nippon-large-cap", "icici-large-cap"}),
        ("flexi cap fund", {"hdfc-flexi-cap", "absl-flexi-cap", "ppfas-flexi-cap"}),
        ("corporate bond fund",
         {"hdfc-corporate-bond", "absl-corporate-bond", "icici-corporate-bond"}),
        ("multi cap", {"absl-multi-cap", "kotak-multi-cap"}),
        ("small cap exit load", {"absl-small-cap", "bandhan-small-cap"}),
        ("HDFC fund", {"hdfc-flexi-cap", "hdfc-silver-etf-fof", "hdfc-pharma-healthcare",
                       "hdfc-nifty-next-50", "hdfc-corporate-bond"}),
        ("Birla expense ratio", {s.id for s in CATALOG.schemes_of("absl")}),
        ("the index fund", {"hdfc-nifty-next-50", "absl-bse-total-market"}),
    ],
)
def test_ambiguous_phrases_return_all_candidates(resolver, text, expected):  # RES-01..06
    res = resolver.resolve(text)
    assert res.status == "ambiguous"
    assert set(res.candidates) == expected


@pytest.mark.parametrize(
    ("text", "scheme_id"),
    [
        ("Parag Parikh", "ppfas-flexi-cap"),  # RES-07: AMCs with one covered scheme
        ("Nippon exit load", "nippon-large-cap"),
        ("Kotak", "kotak-multi-cap"),
        ("Bandhan NAV", "bandhan-small-cap"),
        ("Edelweiss", "edelweiss-mid-cap"),
        ("HDFC index fund", "hdfc-nifty-next-50"),  # RES-08: AMC + unique qualifier
        ("HDFC silver", "hdfc-silver-etf-fof"),
        ("ABSL short term", "absl-corporate-bond"),
        ("the mid cap fund", "edelweiss-mid-cap"),  # one scheme in that category
    ],
)
def test_unique_amc_or_qualifier_resolves(resolver, text, scheme_id):
    res = resolver.resolve(text)
    assert res.status == "resolved" and res.scheme_id == scheme_id


@pytest.mark.parametrize(
    "text",
    [
        "HDFC Mid-Cap Opportunities expense ratio",  # RES-10
        "HDFC Gold ETF FoF",
        "HDFC Nifty 50 Index Fund",
        "Expense ratio of SBI Bluechip?",  # CLS-30
        "SBI Small Cap exit load",
        "Axis Flexi Cap NAV",
        "HDFC credit risk fund",
        "ABSL Liquid Fund",
        # Phase 7 eval: AMCs with one covered scheme must not absorb their other schemes
        "Kotak Emerging Equity Fund exit load",
        "Bandhan Sterling Value Fund expense ratio",
        "Parag Parikh Conservative Hybrid Fund benchmark",
        "Edelweiss Arbitrage Fund exit load",
        "Kotak Infinity Fund expense ratio",  # unknown name word + "fund"
    ],
)
def test_uncovered_schemes_are_never_guessed(resolver, text):
    res = resolver.resolve(text)
    assert res.status == "not_covered", res
    assert res.scheme_ids == () and res.candidates == ()


@pytest.mark.parametrize(
    "text", ["What is BSE?", "Nifty returns today", "What is Nifty Next 50 TRI?"]
)
def test_index_names_alone_do_not_resolve_a_scheme(resolver, text):  # RES-14, RES-15
    assert resolver.resolve(text).status == "none"


def test_several_schemes_are_returned_in_order_of_mention(resolver):  # RES-30
    res = resolver.resolve("Expense ratio of PPFAS Flexi Cap and HDFC Flexi Cap?")
    assert res.scheme_ids == ("ppfas-flexi-cap", "hdfc-flexi-cap")
    assert res.scheme_id == "ppfas-flexi-cap"


# ------------------------------------------------------------------------------ thread context
@pytest.mark.parametrize("text", ["and its benchmark?", "what about the exit load?",
                                  "exit load?", "What is its NAV?", "this fund's riskometer"])
def test_follow_up_uses_the_active_scheme(resolver, text):  # RES-20
    res = resolver.resolve(text, active_scheme_id="kotak-multi-cap")
    assert res.status == "context" and res.scheme_id == "kotak-multi-cap"


def test_follow_up_without_context_needs_clarification(resolver):  # RES-21, CLS-38
    res = resolver.resolve("what about its exit load?")
    assert res.status == "none" and res.needs_clarification


def test_explicit_scheme_wins_over_context(resolver):  # RES-22, RES-27
    res = resolver.resolve("What about Bandhan Small Cap?", active_scheme_id="kotak-multi-cap")
    assert res.status == "resolved" and res.scheme_id == "bandhan-small-cap"


def test_concept_question_does_not_use_context(resolver):
    res = resolver.resolve("What is exit load?", active_scheme_id="kotak-multi-cap")
    assert res.status == "none" and not res.needs_clarification


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("the ICICI one", "icici-large-cap"),  # RES-09
        ("Nippon", "nippon-large-cap"),
        ("2", "nippon-large-cap"),
        ("the second", "nippon-large-cap"),
        ("last", "icici-large-cap"),
        ("bluechip", "icici-large-cap"),
        ("ABSL Large Cap", "absl-large-cap"),
    ],
)
def test_reply_to_a_clarifying_question(resolver, reply, expected):
    pending = ("absl-large-cap", "nippon-large-cap", "icici-large-cap")
    res = resolver.resolve(reply, pending_candidates=pending)
    assert res.status == "resolved" and res.scheme_id == expected


# ------------------------------------------------------------------------------ field intent
@pytest.mark.parametrize(
    ("text", "fields"),
    [
        ("TER of Kotak", ("expense_ratio",)),  # FLD-01
        ("what are the fees", ("expense_ratio",)),
        ("annual charges", ("expense_ratio",)),
        ("expnse ratio", ("expense_ratio",)),  # CLS-40 typo
        ("exti load", ("exit_load",)),
        ("redemption fee if I redeem early", ("exit_load",)),
        ("What are the charges?", ("expense_ratio", "exit_load")),  # FLD-02
        ("minimum investment", ("min_sip", "min_lumpsum")),  # FLD-03
        ("min SIP", ("min_sip",)),
        ("lump sum amount", ("min_lumpsum",)),
        ("lock-in period", ("lock_in",)),
        ("riskometer", ("riskometer",)),
        ("how risky is it", ("riskometer",)),
        ("benchmark index", ("benchmark",)),
        ("who manages it", ("fund_managers",)),
        ("launch date", ("launch_date",)),
        ("expense ratio and exit load", ("expense_ratio", "exit_load")),  # FLD-04
    ],
)
def test_field_intent(text, fields):
    assert detect_intent(text).fields == fields


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3-year return", MetricIntent("return", "3y", "annualised")),
        ("3 yr returns", MetricIntent("return", "3y", "annualised")),
        ("1 year return", MetricIntent("return", "1y", "absolute")),
        ("6 month return", MetricIntent("return", "6m", "absolute")),
        ("absolute return for 5 years", MetricIntent("return", "5y", "absolute")),
        ("annualised return since launch", MetricIntent("return", "since_launch", "annualised")),
        ("CAGR over five years", MetricIntent("return", "5y", "annualised")),
        ("SIP return for 3 years", MetricIntent("sip_return", "3y", "sip_annualised")),
        ("NAV", MetricIntent("nav", None, "point")),
        ("fund size", MetricIntent("aum", None, "point")),
        ("AUM", MetricIntent("aum", None, "point")),
        ("rank in category for 3 years", MetricIntent("category_rank", "3y", "point")),
        ("sharpe ratio", MetricIntent("sharpe_ratio", None, "point")),
        ("top holdings", MetricIntent("holdings", None, "point")),
        ("similar funds", MetricIntent("peers", None, "point")),
        ("category average for 1 year", MetricIntent("category_average_return", "1y",
                                                     "absolute")),
    ],
)
def test_metric_intent(text, expected):
    assert expected in detect_intent(text).metrics


def test_tax_return_is_not_a_metric():  # CLS-06
    assert detect_intent("How do I file tax returns on MF gains?").metrics == ()


def test_scheme_names_are_not_read_as_fields(resolver):
    _, intent = resolver.analyse("expense ratio of ABSL Credit Risk Fund")
    assert intent.fields == ("expense_ratio",)  # not riskometer


def test_concept_questions():
    assert is_concept_question("What is exit load?")
    assert is_concept_question("What does riskometer mean?")
    assert not is_concept_question("What is the exit load?")
    assert not is_concept_question("What is its NAV?")
    assert is_concept_question("How is the riskometer of a scheme derived?")
    assert is_concept_question("How is NAV calculated?")
    assert is_concept_question("What is the exit load on mutual funds?")
    assert not is_concept_question("What is the exit load of the fund?")
    # Phase 7 eval
    assert is_concept_question("What is the lock-in period of an ELSS fund?")
    assert is_concept_question("How much deduction can I claim under 80C?")
    assert is_concept_question("How many risk levels does the riskometer have?")
    assert is_concept_question("How often is a scheme's riskometer evaluated?")
    assert not is_concept_question("How often is the NAV of this fund published?")


@pytest.mark.parametrize("text", ["Kotak Mutual Fund exit load", "the Kotak fund's exit load",
                                  "Parag Parikh fund expense ratio"])
def test_amc_with_filler_words_still_resolves(resolver, text):
    assert resolver.resolve(text).status == "resolved"


def test_other_plans_are_detected():  # CLS-31
    assert mentions_other_plan("Regular plan expense ratio of Kotak Multi Cap")
    assert mentions_other_plan("IDCW option NAV")
    assert not mentions_other_plan("Direct growth expense ratio")
