"""End-to-end pipeline with the Groq call mocked: each route gives the right reply type, and
every reply has <= 3 sentences, exactly one link and the footer."""

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.pipeline.catalog import Catalog
from app.pipeline.classifier import Classifier, GroqClassifier, QueryClass
from app.pipeline.generator import Generator
from app.pipeline.orchestrator import AnswerPipeline, ThreadContext
from app.pipeline.pii_guard import WITHHELD_PLACEHOLDER
from app.pipeline.retriever import Evidence, RetrievalResult
from app.pipeline.router import Router
from app.pipeline.text import split_sentences
from tests.fake_groq import FakeGroq, answer, client, reply, status_error

KOTAK = "kotak-multi-cap"
WHEN = datetime(2026, 11, 9, 13, 39, tzinfo=UTC)
CONFIRMED = date(2026, 11, 9)


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: CONFIRMED for sid in cat.source_urls})


def fact_block(catalog, scheme_id=KOTAK, fields=None):
    fields = fields or {"expense_ratio": "0.42%"}
    s = catalog.schemes[scheme_id]
    lines = "\n".join(f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in fields.items())
    return Evidence("E1", "fact", f"{s.name} (Direct Plan - Growth), from its scheme page:\n"
                    f"{lines}", s.source_id, s.url, WHEN, scheme_id, data={"fields": fields})


def metric_block(catalog, scheme_id=KOTAK):
    s = catalog.schemes[scheme_id]
    row = {"label": "3-year annualised return", "value": 17.06, "unit": "%", "period": "3y",
           "basis": "annualised", "as_of": date(2026, 11, 6), "rank_of": None,
           "displayed": True}
    return Evidence("E1", "metric", f"{s.name} (Direct Plan - Growth), figures published by "
                    "Groww:\n- 3-year annualised return: +17.06% as of 06 Nov 2026",
                    s.source_id, s.url, WHEN, scheme_id,
                    data={"metric": "return", "rows": [row]})


def chunk_block(catalog, eid="E2", scheme_id=KOTAK):
    s = catalog.schemes[scheme_id]
    return Evidence(eid, "chunk", f"[{s.name}] Identifiers\nISIN: INF174KA1HS9.", s.source_id,
                    s.url, WHEN, scheme_id, 0.8, f"groww-{scheme_id}::identifiers::0")


class FakeRetriever:
    """Returns evidence built per request by `make(request)`; records the requests."""

    def __init__(self, catalog, make=None):
        self.catalog, self.requests = catalog, []
        self.make = make or (lambda req: [fact_block(catalog, req.scheme_id or KOTAK),
                                          chunk_block(catalog, scheme_id=req.scheme_id or KOTAK)])

    async def retrieve(self, request):
        self.requests.append(request)
        evidence = tuple(self.make(request))
        link = (self.catalog.scheme_link(request.scheme_id) if request.scheme_id
                else self.catalog.education_link("default"))
        status = "ok" if evidence else "no_evidence"
        return RetrievalResult(status, evidence, request.scheme_id, link, 0.85,
                               "" if evidence else "below min_relevance")


def pipeline(catalog, fake=None, retriever=None, llm_classifier=False):
    llm = client(fake) if fake is not None else None
    classifier = Classifier(GroqClassifier(llm) if llm_classifier and llm else None)
    generator = Generator(llm) if llm else None
    return AnswerPipeline(catalog, Router(catalog, classifier),
                          retriever or FakeRetriever(catalog), generator)


def assert_format(reply):
    assert 1 <= len(split_sentences(reply.answer)) <= 3, reply.answer
    assert re.findall(r"https?://\S+", reply.rendered) == [reply.citation_url]
    assert "http" not in reply.answer
    assert reply.rendered.rstrip().endswith("Last updated from sources: 09 Nov 2026")


GOOD = "The expense ratio of Kotak Multi Cap Fund is 0.42%."


# ------------------------------------------------------------------------------ answer route
async def test_factual_answer(catalog):
    fake = FakeGroq(answer(GOOD))
    result = await pipeline(catalog, fake).answer(
        ThreadContext(), "What is the expense ratio of Kotak Multi Cap?")
    assert result.route == "answer" and result.reply.type == "answer"
    assert result.reply.answer == GOOD
    assert result.reply.citation_url == catalog.schemes[KOTAK].url
    assert result.active_scheme_id == KOTAK
    assert result.trace["llm_calls"] == 1 and result.trace["prompt_tokens"] == 100
    assert_format(result.reply)
    messages = fake.requests[0]["messages"]
    assert messages[0]["role"] == "system" and "[E1]" in messages[-1]["content"]


async def test_history_is_sent_and_follow_up_uses_the_active_scheme(catalog):
    fake = FakeGroq(answer(GOOD),
                    answer("The exit load of Kotak Multi Cap Fund is 1% within 1 year."))
    retr = FakeRetriever(catalog, lambda req: [fact_block(
        catalog, req.scheme_id, {"expense_ratio": "0.42%",
                                 "exit_load": "Exit load of 1% if redeemed within 1 year."})])
    p = pipeline(catalog, fake, retr)
    ctx = ThreadContext()
    first = await p.answer(ctx, "What is the expense ratio of Kotak Multi Cap?")
    ctx = first.next_context(ctx)
    second = await p.answer(ctx, "and its exit load?")
    assert retr.requests[1].scheme_id == KOTAK
    assert second.route == "answer"
    sent = fake.requests[1]["messages"]
    assert {"role": "assistant", "content": GOOD} in sent  # earlier turn, for context only
    assert {"role": "user", "content": "What is the expense ratio of Kotak Multi Cap?"} in sent


@pytest.mark.parametrize("follow_up", ["And for Bandhan Small Cap?", "Bandhan Small Cap?",
                                       "what about Bandhan Small Cap Fund"])
async def test_elliptical_follow_up_inherits_the_question(catalog, follow_up):
    # Phase 7 eval: "And for Bandhan Small Cap?" named a scheme but no field
    retr = FakeRetriever(catalog, lambda req: [fact_block(catalog, req.scheme_id,
                                                          {"expense_ratio": "0.65%"})])
    p = pipeline(catalog, None, retr)
    ctx = ThreadContext()
    first = await p.answer(ctx, "What is the expense ratio of Kotak Multi Cap?")
    ctx = first.next_context(ctx)
    assert ctx.last_intent.fields == ("expense_ratio",)
    second = await p.answer(ctx, follow_up)
    assert retr.requests[1].scheme_id == "bandhan-small-cap"
    assert retr.requests[1].intent.fields == ("expense_ratio",)
    assert second.trace["intent_inherited"] and second.reply.type == "answer"


async def test_reply_to_a_clarifying_question_inherits_the_question(catalog):
    retr = FakeRetriever(catalog, lambda req: [fact_block(
        catalog, req.scheme_id, {"exit_load": "Exit load of 1%, if redeemed within 1 month."})])
    p = pipeline(catalog, None, retr)
    ctx = ThreadContext()
    first = await p.answer(ctx, "What is the exit load of the large cap fund?")
    assert first.reply.type == "clarify"
    ctx = first.next_context(ctx)
    await p.answer(ctx, "the ICICI one")
    assert retr.requests[0].scheme_id == "icici-large-cap"
    assert retr.requests[0].intent.fields == ("exit_load",)


async def test_a_full_new_question_does_not_inherit(catalog):
    retr = FakeRetriever(catalog, lambda req: [chunk_block(catalog, "E1", req.scheme_id)])
    p = pipeline(catalog, None, retr)
    ctx = (await p.answer(ThreadContext(), "Expense ratio of Kotak Multi Cap?")).next_context(
        ThreadContext())
    await p.answer(ctx, "Tell me about the investment approach of Bandhan Small Cap Fund")
    assert retr.requests[-1].intent.empty


def test_thread_intent_round_trips_through_json():
    from app.pipeline.orchestrator import intent_from_dict, intent_to_dict
    from app.pipeline.scheme_resolver import Intent, MetricIntent

    intent = Intent(("exit_load",), (MetricIntent("return", "3y", "annualised"),))
    assert intent_from_dict(json.loads(json.dumps(intent_to_dict(intent)))) == intent
    assert intent_from_dict(None) is None


async def test_holdings_fall_back_to_their_template(catalog):
    from app.pipeline.orchestrator import deterministic_reply
    from app.pipeline.retriever import Evidence
    from app.pipeline.router import RetrievalRequest
    from app.pipeline.scheme_resolver import Intent, MetricIntent

    s = catalog.schemes["ppfas-flexi-cap"]
    block = Evidence("E1", "holdings", "...", s.source_id, s.url, None, s.id, data={
        "holdings": [{"company_name": "HDFC Bank Ltd", "percent_of_assets": 7.75}],
        "portfolio_date": date(2026, 9, 30), "total": 50})
    request = RetrievalRequest(QueryClass.FACTUAL_METRIC, s.id, (),
                               Intent((), (MetricIntent("holdings"),)), "top holdings?")
    reply = deterministic_reply(catalog, request, [block])
    assert "HDFC Bank Ltd (7.75%)" in reply.answer and "30 Sep 2026" in reply.answer


async def test_url_in_the_answer_is_stripped(catalog):
    fake = FakeGroq(answer(GOOD + " See https://groww.in/x."))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.route == "answer" and "http" not in result.reply.answer
    assert_format(result.reply)


async def test_several_schemes_invite_a_follow_up(catalog):  # RES-30
    fake = FakeGroq(answer("The expense ratio of HDFC Flexi Cap Fund is 0.42%."))
    result = await pipeline(catalog, fake).answer(
        ThreadContext(), "Expense ratio of HDFC Flexi Cap and PPFAS Flexi Cap?")
    assert result.reply.answer.endswith("Ask me about Parag Parikh Flexi Cap Fund in a "
                                        "separate question.")
    assert_format(result.reply)


# ------------------------------------------------------------------------------ retry / fallback
async def test_retry_once_then_accept(catalog):
    fake = FakeGroq(reply("not json"), answer(GOOD))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.route == "answer" and len(fake.requests) == 2
    assert "rejected" in fake.requests[1]["messages"][-1]["content"]
    assert result.trace["validator_failures"] == ["schema#1"]


async def test_two_bad_replies_fall_back_to_the_fact_template(catalog):
    wrong = answer("The expense ratio of Kotak Multi Cap Fund is 0.5%.")  # not in evidence
    fake = FakeGroq(wrong, answer("The expense ratio of Kotak Multi Cap Fund is 0.4%."))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.route == "template_fallback"
    assert result.reply.answer == "The expense ratio of Kotak Multi Cap Fund is 0.42%."
    assert result.trace["fallback_used"] == "validator"
    assert_format(result.reply)


async def test_llm_unavailable_uses_the_fact_template(catalog):
    fake = FakeGroq(status_error(500), status_error(500))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.route == "template_fallback" and result.trace["fallback_used"] == "llm_server"
    assert_format(result.reply)


async def test_llm_unavailable_uses_the_metric_template(catalog):
    fake = FakeGroq(status_error(429, retry_after=120))
    retr = FakeRetriever(catalog, lambda req: [metric_block(catalog)])
    result = await pipeline(catalog, fake, retr).answer(
        ThreadContext(), "3-year return of Kotak Multi Cap?")
    assert result.route == "template_fallback"
    assert result.reply.answer == ("The 3-year annualised return of Kotak Multi Cap Fund was "
                                   "+17.06% as of 06 Nov 2026, as published by Groww.")
    assert_format(result.reply)


async def test_llm_down_without_a_template_says_so(catalog):
    fake = FakeGroq(status_error(400))
    retr = FakeRetriever(catalog, lambda req: [chunk_block(catalog, "E1")])
    result = await pipeline(catalog, fake, retr).answer(ThreadContext(), "ISIN of Kotak Multi Cap?")
    assert result.route == "unavailable" and result.reply.type == "unavailable"
    assert "try again" in result.reply.answer  # not "I couldn't find that"
    assert result.reply.citation_url == catalog.schemes[KOTAK].url  # most relevant source
    assert_format(result.reply)


async def test_validator_giving_up_without_a_template_is_no_evidence(catalog):
    bad = answer("The ISIN is INF174KA1HS9 and it was 2.5%.", "E1")  # 2.5 not in evidence
    fake = FakeGroq(bad, bad)
    retr = FakeRetriever(catalog, lambda req: [chunk_block(catalog, "E1")])
    result = await pipeline(catalog, fake, retr).answer(ThreadContext(), "ISIN of Kotak Multi Cap?")
    assert result.route == "no_evidence_fallback" and result.reply.type == "no_evidence"


async def test_without_an_api_key_templates_answer(catalog):
    result = await pipeline(catalog, None).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.route == "template_fallback" and result.trace["fallback_used"] == "no_llm"


# ------------------------------------------------------------------------------ validator routes
async def test_unanswerable_reply_gives_no_evidence(catalog):
    fake = FakeGroq(answer("", answerable=False))
    retr = FakeRetriever(catalog, lambda req: [chunk_block(catalog, "E1")])  # no fact block
    result = await pipeline(catalog, fake, retr).answer(
        ThreadContext(), "Who audits Kotak Multi Cap Fund?")
    assert result.reply.type == "no_evidence" and len(fake.requests) == 1
    assert_format(result.reply)


async def test_unanswerable_reply_with_the_fact_in_evidence_uses_the_template(catalog):
    # Phase 7 eval: the model refused "HDFC Equity Fund" questions because the evidence names
    # the scheme HDFC Flexi Cap; the stored fact for the asked field is authoritative.
    fake = FakeGroq(answer("", answerable=False))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.reply.type == "answer" and result.route == "template_fallback"
    assert result.trace["fallback_used"] == "unanswerable_with_fact"
    assert_format(result.reply)


async def test_generator_is_told_the_resolved_scheme_and_field(catalog):
    fake = FakeGroq(answer(GOOD))
    await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multicap?")
    last_user = [m for m in fake.requests[-1]["messages"] if m["role"] == "user"][-1]
    name = catalog.schemes[KOTAK].name
    assert f"Resolved: {name} (Direct Plan - Growth); asked about: expense ratio" in \
        last_user["content"]


async def test_advice_in_the_answer_becomes_a_refusal(catalog):
    fake = FakeGroq(answer(GOOD + " You should invest in it."))
    result = await pipeline(catalog, fake).answer(ThreadContext(), "TER of Kotak Multi Cap?")
    assert result.reply.type == "advisory_refusal" and result.route == "validator_advisory"
    assert_format(result.reply)


async def test_judgement_in_the_answer_becomes_a_refusal(catalog):
    fake = FakeGroq(answer("Kotak Multi Cap Fund has strong returns of +17.06% (3-year "
                           "annualised as of 06 Nov 2026)."))
    retr = FakeRetriever(catalog, lambda req: [metric_block(catalog)])
    result = await pipeline(catalog, fake, retr).answer(ThreadContext(),
                                                        "3-year return of Kotak Multi Cap?")
    assert result.reply.type == "performance_refusal"
    assert result.reply.citation_url == catalog.schemes[KOTAK].url
    assert_format(result.reply)


# ------------------------------------------------------------------------------ template routes
@pytest.mark.parametrize(
    ("text", "reply_type"),
    [
        ("My PAN is ABCDE1234F", "pii_blocked"),
        ("Should I invest in Kotak Multi Cap?", "advisory_refusal"),
        ("Is HDFC Flexi Cap performing well?", "performance_refusal"),
        ("Hi", "greeting"),
        ("What is the share price of Reliance?", "out_of_scope"),
        ("Expense ratio of SBI Bluechip?", "not_covered"),
        ("Regular plan expense ratio of Kotak Multi Cap", "not_covered"),
        ("What is the exit load of the large cap fund?", "clarify"),
    ],
)
async def test_template_routes_never_call_the_llm(catalog, text, reply_type):
    fake = FakeGroq()  # any call fails the test
    result = await pipeline(catalog, fake).answer(ThreadContext(active_scheme_id=KOTAK), text)
    assert result.reply.type == reply_type
    assert fake.requests == []
    assert_format(result.reply)


async def test_pii_is_never_stored_or_sent(catalog):
    fake = FakeGroq()
    ctx = ThreadContext(active_scheme_id=KOTAK)
    result = await pipeline(catalog, fake).answer(ctx, "my phone is 9876543210")
    nxt = result.next_context(ctx)
    assert nxt.history[0] == {"role": "user", "content": WITHHELD_PLACEHOLDER}
    assert nxt.active_scheme_id == KOTAK and "9876543210" not in str(result.trace)


async def test_clarify_then_answer(catalog):
    fake = FakeGroq(answer("The exit load of ICICI Prudential Large Cap Fund is 1% within 1 "
                           "month."))
    retr = FakeRetriever(catalog, lambda req: [fact_block(
        catalog, req.scheme_id, {"exit_load": "Exit load of 1%, if redeemed within 1 month."})])
    p = pipeline(catalog, fake, retr)
    ctx = ThreadContext()
    first = await p.answer(ctx, "What is the exit load of the large cap fund?")
    assert first.reply.type == "clarify" and len(first.pending_candidates) == 3
    second = await p.answer(first.next_context(ctx), "the ICICI one")
    assert retr.requests[0].scheme_id == "icici-large-cap"
    assert second.route == "answer" and second.active_scheme_id == "icici-large-cap"
    assert second.pending_candidates == ()


async def test_retrieval_no_evidence_skips_the_llm(catalog):
    fake = FakeGroq()
    retr = FakeRetriever(catalog, lambda req: [])
    result = await pipeline(catalog, fake, retr).answer(
        ThreadContext(), "What is the colour of HDFC Flexi Cap?")
    assert result.reply.type == "no_evidence" and fake.requests == []
    assert result.reply.citation_url == catalog.schemes["hdfc-flexi-cap"].url
    assert_format(result.reply)


async def test_llm_classifier_and_generator_share_the_client(catalog):
    fake = FakeGroq(by_purpose={"query_class": [reply('{"class": "factual_scheme"}')],
                                "answer": [answer(GOOD)]})
    result = await pipeline(catalog, fake, llm_classifier=True).answer(
        ThreadContext(), "Tell me about Kotak Multi Cap Fund")
    assert result.route == "answer" and len(fake.requests) == 2


async def test_confident_rules_skip_the_classifier_call(catalog):
    fake = FakeGroq(by_purpose={"answer": [answer(GOOD)]})  # a classifier call would fail
    result = await pipeline(catalog, fake, llm_classifier=True).answer(
        ThreadContext(), "Tell me the TER of Kotak Multi Cap Fund")
    assert result.route == "answer" and len(fake.requests) == 1


async def test_llm_classifier_can_refuse(catalog):
    fake = FakeGroq(by_purpose={"query_class": [reply('{"class": "advisory"}')]})
    result = await pipeline(catalog, fake, llm_classifier=True).answer(
        ThreadContext(), "Tell me about Kotak Multi Cap Fund for retirement planning")
    assert result.reply.type == "advisory_refusal" and len(fake.requests) == 1


# ------------------------------------------------------------------------------ history (Phase 8)
def _history_sent(fake):
    """Earlier turns in the last request: everything between the system prompt and the final
    evidence + question turn."""
    return fake.requests[-1]["messages"][1:-1]


async def test_a_self_contained_question_gets_no_history(catalog):
    # Phase 8 smoke test: after "ELSS lock-in?", the model read the capital-gains question as
    # an ELSS question and refused it. History is only for turns that lean on the thread.
    fake = FakeGroq(answer(GOOD), answer(GOOD))
    p = pipeline(catalog, fake)
    ctx = (await p.answer(ThreadContext(), "TER of Kotak Multi Cap?")).next_context(
        ThreadContext())
    second = await p.answer(ctx, "What is the expense ratio of Kotak Multi Cap Fund?")
    assert _history_sent(fake) == [] and second.trace["history_sent"] is False


@pytest.mark.parametrize("follow_up", ["and its exit load?", "What is this fund's TER?",
                                       "And for Bandhan Small Cap?"])
async def test_a_follow_up_gets_the_history(catalog, follow_up):
    fake = FakeGroq(answer(GOOD), answer(GOOD))
    p = pipeline(catalog, fake)
    ctx = (await p.answer(ThreadContext(), "TER of Kotak Multi Cap?")).next_context(
        ThreadContext())
    result = await p.answer(ctx, follow_up)
    assert result.trace["history_sent"] is True
    assert {"role": "user", "content": "TER of Kotak Multi Cap?"} in _history_sent(fake)
