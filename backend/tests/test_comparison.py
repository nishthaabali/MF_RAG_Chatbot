"""Side-by-side comparison of published figures (comparison.py): classification, routing, the
table itself, the pipeline route (no LLM) and persistence of the table with the thread."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app.pipeline import comparison
from app.pipeline.catalog import Catalog
from app.pipeline.classifier import Classifier, QueryClass, classify_rules
from app.pipeline.orchestrator import AnswerPipeline, ThreadContext
from app.pipeline.retriever import Evidence
from app.pipeline.router import Router
from app.pipeline.scheme_resolver import Intent, MetricIntent
from app.pipeline.templates import NOT_PUBLISHED, Comparison, Reply
from app.pipeline.text import split_sentences
from app.store.db import init_db
from app.store.models import THREAD_TABLES
from app.store.threads import ThreadStore
from tests.fake_groq import FakeGroq, client

KOTAK, ICICI = "kotak-multi-cap", "icici-large-cap"
USER_QUESTION = ("compare the expense ration, NAV and 3Y CAGR of kotak Multi cap fund and "
                 "ICICI Prudential Large cap fund")
WHEN = datetime(2026, 11, 9, 13, 39, tzinfo=UTC)
JUDGEMENT = re.compile(r"\b(better|best|worse|worst|lower|higher|cheaper|outperform\w*|"
                       r"recommends?|recommended|should)\b", re.I)


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: date(2026, 11, 9) for sid in cat.source_urls})


@pytest.fixture
def router(catalog):
    return Router(catalog, Classifier())


# ------------------------------------------------------------------------------ classifier
@pytest.mark.parametrize("text", [
    USER_QUESTION,
    "Kotak Multi Cap vs HDFC Flexi Cap",
    "HDFC Flexi Cap versus Parag Parikh Flexi Cap expense ratio",
    "Compare Nippon India Large Cap and ICICI Pru Large Cap",
    "Show the NAV of Kotak Multi Cap and HDFC Flexi Cap side by side",
    "comparison of exit load: Bandhan Small Cap, ABSL Small Cap",
])
def test_neutral_comparisons_are_their_own_class(text):
    rule = classify_rules(text)
    assert rule.cls == QueryClass.COMPARISON and rule.decisive


@pytest.mark.parametrize(("text", "cls"), [
    ("Compare Kotak Multi Cap and HDFC Flexi Cap, which is better?", QueryClass.ADVISORY),
    ("Kotak vs HDFC: which has the lower expense ratio?", QueryClass.ADVISORY),
    ("Compare and recommend between Kotak Multi Cap and ICICI Large Cap", QueryClass.ADVISORY),
    ("Should I compare Kotak Multi Cap with HDFC Flexi Cap?", QueryClass.ADVISORY),
    ("How does Kotak Multi Cap compare with its benchmark?", QueryClass.PERFORMANCE_JUDGEMENT),
    ("Compare Kotak Multi Cap vs HDFC Flexi Cap, which performed well?",
     QueryClass.PERFORMANCE_JUDGEMENT),
])
def test_a_verdict_inside_a_comparison_is_still_refused(text, cls):
    assert classify_rules(text).cls == cls


# ------------------------------------------------------------------------------ router
async def test_the_user_question_routes_to_a_table(router):
    route = await router.route_message(USER_QUESTION)
    assert route.route == "comparison" and route.reply is None and route.retrieval is None
    request = route.comparison
    assert request.scheme_ids == (KOTAK, ICICI)  # in order of mention
    assert request.intent.fields == ("expense_ratio",)  # "expense ration": typo tolerated
    assert request.intent.metrics == (MetricIntent("nav", None, "point"),
                                      MetricIntent("return", "3y", "annualised"))
    assert route.active_scheme_id == KOTAK


async def test_a_bare_comparison_shows_the_scheme_basics(router):
    route = await router.route_message("Kotak Multi Cap vs HDFC Flexi Cap")
    assert route.comparison.intent == comparison.DEFAULT_INTENT


async def test_compare_it_with_uses_the_thread_scheme(router):
    route = await router.route_message("compare it with ICICI Pru Large Cap",
                                       active_scheme_id=KOTAK)
    assert route.comparison.scheme_ids == (KOTAK, ICICI)


async def test_a_misspelt_second_scheme_is_found(router):
    route = await router.route_message("compare kotak mutli cap and ICICI Pru Large Cap NAV")
    assert set(route.comparison.scheme_ids) == {KOTAK, ICICI}


async def test_only_one_covered_scheme_asks_for_the_other(router):
    route = await router.route_message("Compare the expense ratio of Kotak Multi Cap")
    assert route.route == "clarify" and "Kotak Multi Cap Fund" in route.reply.answer


async def test_an_uncovered_second_scheme_is_not_covered(router):
    route = await router.route_message("Compare Kotak Multi Cap with SBI Bluechip")
    assert route.route == "not_covered"


async def test_no_scheme_at_all_keeps_the_education_refusal(router):
    route = await router.route_message("Direct vs regular plan")
    assert route.route == "advisory" and route.reply.type == "advisory_refusal"


async def test_regular_plan_comparison_is_not_covered(router):
    route = await router.route_message("Compare regular plan of Kotak Multi Cap vs HDFC Flexi Cap")
    assert route.route == "plan_not_covered"


async def test_at_most_four_schemes(router):
    route = await router.route_message(
        "Compare NAV of Kotak Multi Cap, HDFC Flexi Cap, PPFAS Flexi Cap, ICICI Pru Large Cap "
        "and Bandhan Small Cap")
    assert len(route.comparison.scheme_ids) == comparison.MAX_SCHEMES
    assert route.comparison.dropped == ("bandhan-small-cap",)


async def test_items_a_table_cannot_show_are_named(router):
    route = await router.route_message(
        "Compare expense ratio and holdings of Kotak Multi Cap and HDFC Flexi Cap")
    assert route.comparison.intent.fields == ("expense_ratio",)
    assert route.comparison.left_out == ("top holdings",)


# ------------------------------------------------------------------------------ the table
def fact(catalog, sid, fields, when=WHEN):
    s = catalog.schemes[sid]
    return Evidence("", "fact", "", s.source_id, s.url, when, sid, data={"fields": fields})


def metric(catalog, sid, name, rows, when=WHEN):
    s = catalog.schemes[sid]
    full = [{"label": label, "value": value, "unit": unit, "period": period,
             "basis": "point" if period is None else "annualised", "as_of": as_of,
             "rank_of": None, "displayed": True} for label, value, unit, period, as_of in rows]
    return Evidence("", "metric", "", s.source_id, s.url, when, sid,
                    data={"metric": name, "rows": full})


INTENT = Intent(("expense_ratio",), (MetricIntent("nav", None, "point"),
                                     MetricIntent("return", "3y", "annualised")))
NOV6, NOV7 = date(2026, 11, 6), date(2026, 11, 7)
ICICI_FETCHED = datetime(2026, 11, 8, 4, tzinfo=UTC)  # an older page than Kotak's


def evidence(catalog):
    return {
        KOTAK: [fact(catalog, KOTAK, {"expense_ratio": "0.70%"}),
                metric(catalog, KOTAK, "nav", [("NAV", 21.74, "₹", None, NOV7)]),
                metric(catalog, KOTAK, "return",
                       [("3-year annualised return", 17.11, "%", "3y", NOV7)])],
        ICICI: [fact(catalog, ICICI, {"expense_ratio": "1.00%"}, when=ICICI_FETCHED),
                metric(catalog, ICICI, "nav", [("NAV", 122.96, "₹", None, NOV6)],
                       when=ICICI_FETCHED)],
    }


def test_table_cells_are_the_published_values(catalog):
    reply = comparison.build(catalog, [KOTAK, ICICI], INTENT, evidence(catalog))
    table = reply.comparison
    assert reply.type == "comparison"
    assert [c.scheme_id for c in table.columns] == [KOTAK, ICICI]  # never re-ordered
    rows = {r.label: r.cells for r in table.rows}
    assert rows["Expense ratio"] == ("0.70%", "1.00%")
    # Different as-of dates are shown per cell; a shared one goes in the label.
    assert rows["NAV"] == ("₹21.74 (as of 07 Nov 2026)", "₹122.96 (as of 06 Nov 2026)")
    assert rows["3-year annualised return"] == ("+17.11% (as of 07 Nov 2026)", NOT_PUBLISHED)
    assert any("doesn't publish" in n for n in table.notes)


def test_shared_as_of_date_goes_in_the_row_label(catalog):
    ev = evidence(catalog)
    ev[ICICI][1] = metric(catalog, ICICI, "nav", [("NAV", 122.96, "₹", None, NOV7)])
    rows = {r.label: r.cells for r in comparison.build(catalog, [KOTAK, ICICI], INTENT,
                                                       ev).comparison.rows}
    assert rows["NAV (as of 07 Nov 2026)"] == ("₹21.74", "₹122.96")


def test_a_figure_no_scheme_publishes_is_a_row_of_dashes(catalog):
    intent = Intent((), (MetricIntent("return", "10y", "annualised"),))
    reply = comparison.build(catalog, [KOTAK, ICICI], intent, {KOTAK: [], ICICI: []})
    assert reply.comparison.rows[0].label == "10-year annualised return"
    assert reply.comparison.rows[0].cells == (NOT_PUBLISHED, NOT_PUBLISHED)


def test_reply_states_no_verdict_and_cites_every_scheme(catalog):
    reply = comparison.build(catalog, [KOTAK, ICICI], INTENT, evidence(catalog))
    assert 1 <= len(split_sentences(reply.answer)) <= 3
    assert not JUDGEMENT.search(reply.answer + reply.comparison.markdown())
    assert "http" not in reply.answer
    urls = re.findall(r"https?://\S+", reply.rendered)
    assert urls == [catalog.schemes[KOTAK].url, catalog.schemes[ICICI].url]
    assert reply.citation_url == catalog.schemes[KOTAK].url
    # The footer is the oldest of the sources, so no figure looks fresher than it is.
    assert reply.last_updated == date(2026, 11, 8)
    assert reply.rendered.rstrip().endswith("Last updated from sources: 08 Nov 2026")


def test_markdown_escapes_pipes():
    table = Comparison.from_dict({
        "columns": [{"scheme_id": "a", "name": "A", "url": "https://x/a", "last_updated": None}],
        "rows": [{"label": "Exit load", "cells": ["1% | 2%"]}], "notes": []})
    assert "1% \\| 2%" in table.markdown()


def test_comparison_round_trips_through_a_dict(catalog):
    reply = comparison.build(catalog, [KOTAK, ICICI], INTENT, evidence(catalog))
    assert Comparison.from_dict(reply.comparison.to_dict()) == reply.comparison
    assert reply.to_dict()["comparison"] == reply.comparison.to_dict()


# ------------------------------------------------------------------------------ pipeline
class StructuredRetriever:
    def __init__(self, catalog):
        self.catalog, self.asked = catalog, []

    async def structured_for(self, scheme_ids, intent):
        self.asked.append((tuple(scheme_ids), intent))
        ev = evidence(self.catalog)
        return {sid: ev.get(sid, []) for sid in scheme_ids}

    async def retrieve(self, request):  # pragma: no cover - must not be reached
        raise AssertionError("a comparison never runs chunk retrieval")


async def test_pipeline_answers_without_the_llm(catalog):
    fake = FakeGroq()  # any LLM call raises "unexpected LLM call"
    from app.pipeline.generator import Generator

    retriever = StructuredRetriever(catalog)
    pipe = AnswerPipeline(catalog, Router(catalog, Classifier()), retriever,
                          Generator(client(fake)))
    result = await pipe.answer(ThreadContext(), USER_QUESTION)
    assert result.route == "comparison" and result.reply.type == "comparison"
    assert fake.requests == []
    assert retriever.asked[0][0] == (KOTAK, ICICI)
    assert result.trace["compared"] == [KOTAK, ICICI]
    ctx = result.next_context(ThreadContext())
    assert ctx.active_scheme_id == KOTAK and ctx.last_intent == result.intent


async def test_degraded_pipeline_says_the_sources_are_loading(catalog):
    pipe = AnswerPipeline(catalog, Router(catalog, Classifier()), None, None)
    result = await pipe.answer(ThreadContext(), USER_QUESTION)
    assert result.route == "corpus_unavailable"


# ------------------------------------------------------------------------------ persistence
def test_thread_store_keeps_the_table(catalog, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    store = ThreadStore(engine)
    thread = store.create()
    reply = comparison.build(catalog, [KOTAK, ICICI], INTENT, evidence(catalog))
    store.append_turn(thread.id, USER_QUESTION, reply, "comparison", ThreadContext())
    bot = store.get(thread.id).messages[-1]
    assert bot.type == "comparison"
    assert Comparison.from_dict(bot.comparison) == reply.comparison
    assert bot.rendered == reply.rendered


def test_an_existing_messages_table_gets_the_new_column(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:  # the messages table as created before comparisons
        conn.execute(text(
            "CREATE TABLE messages (id VARCHAR PRIMARY KEY, thread_id VARCHAR, seq INTEGER, "
            "role VARCHAR, content TEXT, response_type VARCHAR, route VARCHAR, "
            "citation_url TEXT, last_updated DATE, created_at DATETIME)"))
    init_db(engine, THREAD_TABLES)
    assert "comparison" in {c["name"] for c in inspect(engine).get_columns("messages")}
    init_db(engine, THREAD_TABLES)  # idempotent


def test_plain_replies_have_no_table():
    reply = Reply("answer", "The NAV is ₹10.", "https://x", None)
    assert reply.comparison is None and reply.to_dict()["comparison"] is None
    assert reply.rendered == "The NAV is ₹10.\n\nSource: https://x"
