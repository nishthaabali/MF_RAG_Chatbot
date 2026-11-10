"""Validator: one passing and one failing case per rule (architecture §6.6)."""

from datetime import UTC, date, datetime

import pytest

from app.pipeline.generator import GeneratedAnswer
from app.pipeline.retriever import Evidence
from app.pipeline.scheme_resolver import Intent
from app.pipeline.text import split_sentences
from app.pipeline.validator import correction_message, validate, value_present

URL = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"
WHEN = datetime(2026, 11, 9, 13, 39, tzinfo=UTC)
FACT = Evidence("E1", "fact", "Kotak Multi Cap Fund (Direct Plan - Growth), from its scheme "
                "page:\n- Expense ratio: 0.42%\n- Exit load: Exit load of 1% if redeemed within "
                "1 year.", "groww-kotak-multi-cap", URL, WHEN, "kotak-multi-cap",
                data={"fields": {"expense_ratio": "0.42%",
                                 "exit_load": "Exit load of 1% if redeemed within 1 year."}})
METRIC = Evidence("E2", "metric", "Kotak Multi Cap Fund (Direct Plan - Growth), figures "
                  "published by Groww:\n- 3-year annualised return: +17.06% as of 06 Nov 2026",
                  "groww-kotak-multi-cap", URL, WHEN, "kotak-multi-cap",
                  data={"metric": "return", "rows": []})
CHUNK = Evidence("E3", "chunk", "[Kotak Multi Cap Fund] Identifiers\nISIN: INF174KA1HS9.",
                 "groww-kotak-multi-cap", URL, WHEN, "kotak-multi-cap", 0.8, "c::1")
EVIDENCE = [FACT, METRIC, CHUNK]
ER = Intent(("expense_ratio",))


def gen(answer, evidence_id="E1", answerable=True):
    return GeneratedAnswer(answer=answer, evidence_id=evidence_id, answerable=answerable)


GOOD = "The expense ratio of Kotak Multi Cap Fund is 0.42%."


def test_accepts_a_compliant_answer():
    v = validate(gen(GOOD), EVIDENCE, ER)
    assert v.ok and v.answer == GOOD and v.evidence is FACT and v.failures == ()


# --- schema
def test_schema_failure_retries():
    v = validate(None, EVIDENCE, ER)
    assert v.action == "retry" and v.failures == ("schema",)


# --- answerable
def test_unanswerable_means_no_evidence():
    v = validate(gen("", answerable=False), EVIDENCE)
    assert v.action == "no_evidence"


# --- evidence id
def test_unknown_evidence_id_retries():
    assert validate(gen(GOOD, "E9"), EVIDENCE, ER).failures == ("evidence_id",)
    assert validate(gen(GOOD, " E1 "), EVIDENCE, ER).ok


# --- length
def test_more_than_three_sentences_is_truncated():
    long = GOOD + " It is a multi cap fund. It is managed by Kotak. It has no lock-in."
    v = validate(gen(long), EVIDENCE, ER)
    assert v.ok and len(split_sentences(v.answer)) == 3 and "length" in v.fixes
    assert validate(gen(GOOD + " There is no lock-in."), EVIDENCE, ER).fixes == ()


# --- URLs
def test_urls_are_stripped():
    v = validate(gen(GOOD + " See https://groww.in/x for details."), EVIDENCE, ER)
    assert v.ok and "http" not in v.answer and "urls" in v.fixes
    assert validate(gen(GOOD), EVIDENCE, ER).fixes == ()


# --- advice
@pytest.mark.parametrize("text", [
    GOOD + " You should invest in it.", GOOD + " I recommend this fund.",
    "At 0.42%, Kotak Multi Cap Fund is a good choice.", GOOD + " It is worth investing.",
])
def test_advice_becomes_an_advisory_refusal(text):
    assert validate(gen(text), EVIDENCE, ER).action == "advisory_refusal"


def test_procedural_should_is_not_advice():
    text = "You should log in to the CAMS website to download the statement."
    assert validate(gen(text, "E3"), EVIDENCE).failures == ()


# --- judgement
@pytest.mark.parametrize("text", [
    "Kotak Multi Cap Fund has delivered strong returns of +17.06% (3-year annualised, as of "
    "06 Nov 2026).",
    "Kotak Multi Cap Fund outperformed its category with +17.06% annualised as of 06 Nov 2026.",
    "Kotak Multi Cap Fund has performed well: +17.06% annualised as of 06 Nov 2026.",
    # predicate form (found by the Phase 7 eval harness tests)
    "Kotak Multi Cap Fund's returns are strong at +17.06% annualised as of 06 Nov 2026.",
    "Its performance has been impressive: +17.06% annualised as of 06 Nov 2026.",
])
def test_judgement_becomes_a_performance_refusal(text):
    assert validate(gen(text, "E2"), EVIDENCE).action == "judgement_refusal"


def test_any_published_figure_needs_its_date():  # Phase 7 eval: a rank stated without a date
    rank = Evidence("E2", "metric", "Kotak Multi Cap Fund, figures published by Groww:\n"
                    "- Groww rank within Multi Cap for 3-year return: 8 of 23 as of 06 Nov 2026",
                    "groww-kotak-multi-cap", URL, WHEN, "kotak-multi-cap",
                    data={"metric": "category_rank", "rows": []})
    v = validate(gen("Kotak Multi Cap Fund is ranked 8 of 23 by Groww.", "E2"), [rank])
    assert v.action == "retry" and "basis_and_date" in v.failures
    v = validate(gen("Groww ranks Kotak Multi Cap Fund 8 of 23 for 3-year return as of "
                     "06 Nov 2026.", "E2"), [rank])
    assert v.action == "accept"


# --- figures
def test_numbers_must_appear_in_the_cited_block():
    v = validate(gen("The expense ratio of Kotak Multi Cap Fund is 0.4%."), EVIDENCE, ER)
    assert v.action == "retry" and "figures" in v.failures
    # a figure from another block than the one cited also fails
    v = validate(gen("The ISIN of Kotak Multi Cap Fund is INF174KA1HS9 and its TER is 0.42%.",
                     "E3"), EVIDENCE)
    assert "figures" in v.failures


def test_reformatted_numbers_still_match():
    ev = Evidence("E1", "chunk", "Fund size (AUM): ₹2,620.50 Cr", "s", URL, WHEN)
    assert validate(gen("Its fund size is ₹2620.50 Cr."), [ev]).ok


# --- basis and date
def test_returns_need_basis_and_date():
    bad = validate(gen("The 3-year return of Kotak Multi Cap Fund is +17.06%.", "E2"), EVIDENCE)
    assert bad.action == "retry" and bad.failures == ("basis_and_date",)
    good = ("The 3-year annualised return of Kotak Multi Cap Fund was +17.06% as of "
            "06 Nov 2026, as published by Groww.")
    assert validate(gen(good, "E2"), EVIDENCE).ok


# --- fact consistency
def test_answer_must_state_the_fact_value():
    v = validate(gen("Kotak Multi Cap Fund charges an expense ratio as per its page.", "E3"),
                 EVIDENCE, ER)
    assert v.action == "retry" and "fact_consistency" in v.failures
    # fields that weren't asked for are not checked
    assert validate(gen(GOOD), EVIDENCE, Intent(("expense_ratio",))).ok


@pytest.mark.parametrize(
    ("value", "answer", "expected"),
    [
        ("0.42%", "It is 0.42%.", True),
        ("Nil", "There is no exit load.", True),
        ("Nil", "The exit load is 1%.", False),
        ("Very High", "Its riskometer level is Very High.", True),
        ("Very High", "Its riskometer level is High.", False),
        ("NIFTY Next 50 Total Return Index", "It tracks the NIFTY Next 50 Total Return Index.",
         True),
        (["Abhishek Bisen", "Devender Singhal"], "Managed by Abhishek Bisen and Devender "
         "Singhal.", True),
        (["Abhishek Bisen", "Devender Singhal"], "Managed by Abhishek Bisen.", False),
        (date(2021, 9, 29), "It was launched on 29 Sep 2021.", True),
        ("Exit load of 1%, if redeemed within 30 days.", "1% if redeemed within 30 days.", True),
        ("Exit load of 1%, if redeemed within 30 days.", "1% if redeemed within 15 days.", False),
    ],
)
def test_value_present(value, answer, expected):
    assert value_present(value, answer) is expected


def test_refusal_wins_over_retry():
    v = validate(gen("You should invest because the TER is 0.4%."), EVIDENCE, ER)
    assert v.action == "advisory_refusal"


def test_correction_message_names_the_problem():
    msg = correction_message(["figures", "advice"])
    assert "every number" in msg and "JSON" in msg and "http" not in msg
