"""Phase 8 API tests: FastAPI app with the real router/catalog, a fake retriever and a mocked
Groq client (or none: deterministic templates), and a temporary SQLite thread store."""

import logging
import re
from dataclasses import replace

import httpx
import pytest

from app.api.deps import Services
from app.config import get_settings
from app.main import GENERIC_ERROR, create_app
from app.pipeline.catalog import Catalog
from app.pipeline.pii_guard import WITHHELD_PLACEHOLDER
from app.store.db import make_engine
from app.store.threads import ThreadStore
from tests.fake_groq import FakeGroq, answer
from tests.test_answer_pipeline import CONFIRMED, KOTAK, FakeRetriever, fact_block
from tests.test_answer_pipeline import pipeline as make_pipeline

FOOTER = "Last updated from sources: 09 Nov 2026"
FACTS = {"expense_ratio": "0.70%", "exit_load": "Exit load of 1%, if redeemed within 1 year.",
         "benchmark": "Nifty 500 Multicap 50:25:25 Total Return Index"}


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: CONFIRMED for sid in cat.source_urls})


@pytest.fixture
def store(tmp_path):
    return ThreadStore(make_engine(f"sqlite:///{(tmp_path / 'threads.db').as_posix()}"))


def build(catalog, store, fake=None, retriever=None, pipeline=None):
    retriever = retriever or FakeRetriever(
        catalog, lambda req: [fact_block(catalog, req.scheme_id or KOTAK, FACTS)])
    pipeline = pipeline or make_pipeline(catalog, fake, retriever)
    services = Services(settings=get_settings(), pipeline=pipeline, store=store, catalog=catalog)
    return create_app(services=services), services, retriever


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                             base_url="http://test")


async def new_thread(c) -> str:
    r = await c.post("/api/threads")
    assert r.status_code == 201
    return r.json()["id"]


# ------------------------------------------------------------------------------ lifecycle
async def test_create_message_history_delete(catalog, store):
    app, services, _ = build(catalog, store)
    async with await client_for(app) as c:
        r = await c.post("/api/threads")
        assert r.status_code == 201
        body = r.json()
        tid = body["id"]
        assert re.fullmatch(r"[0-9a-f-]{36}", tid) and body["created_at"].endswith("Z")

        assert (await c.get(f"/api/threads/{tid}")).json()["messages"] == []

        r = await c.post(f"/api/threads/{tid}/messages",
                         json={"content": "  What is the expense ratio of Kotak Multi Cap?  "})
        assert r.status_code == 200
        reply = r.json()
        assert reply["type"] == "answer" and "0.70%" in reply["answer"]
        assert reply["citation_url"] == catalog.schemes[KOTAK].url
        assert reply["last_updated"] == "2026-11-09"
        assert reply["rendered"].endswith(FOOTER)
        assert re.findall(r"https?://\S+", reply["rendered"]) == [reply["citation_url"]]

        thread = (await c.get(f"/api/threads/{tid}")).json()
        assert thread["title"] == "What is the expense ratio of Kotak Multi Cap?"
        user, bot = thread["messages"]
        assert user["role"] == "user" and user["rendered"] is None
        assert user["content"] == "What is the expense ratio of Kotak Multi Cap?"
        assert bot["role"] == "assistant" and bot["id"] == reply["message_id"]
        assert user["id"] == reply["user_message_id"]
        assert bot["content"] == reply["answer"] and bot["rendered"] == reply["rendered"]
        assert bot["type"] == "answer"

        assert (await c.delete(f"/api/threads/{tid}")).status_code == 204
        assert (await c.get(f"/api/threads/{tid}")).status_code == 404
        assert (await c.delete(f"/api/threads/{tid}")).status_code == 404
    assert len(services.locks) == 0  # no lock kept for finished work


async def test_unknown_thread_is_404(catalog, store):
    app, _, retriever = build(catalog, store)
    async with await client_for(app) as c:
        r = await c.post("/api/threads/nope/messages", json={"content": "hi"})
        assert r.status_code == 404 and r.json()["detail"] == "Thread not found."
        assert (await c.get("/api/threads/nope")).status_code == 404
    assert retriever.requests == []


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
async def test_empty_message_is_rejected(catalog, store, content):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        assert (await c.post(f"/api/threads/{tid}/messages",
                             json={"content": content})).status_code == 422
        assert (await c.get(f"/api/threads/{tid}")).json()["messages"] == []


async def test_message_length_limit(catalog, store):
    app, _, _ = build(catalog, store)
    limit = get_settings().api.max_input_chars
    async with await client_for(app) as c:
        tid = await new_thread(c)
        ok = "Expense ratio of Kotak Multi Cap? " + "x" * (limit - 34)
        assert len(ok) == limit
        assert (await c.post(f"/api/threads/{tid}/messages", json={"content": ok})).status_code \
            == 200
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": ok + "y"})
        assert r.status_code == 422 and str(limit) in r.json()["detail"]
        assert (await c.post(f"/api/threads/{tid}/messages", json={})).status_code == 422


# ------------------------------------------------------------------------------ privacy
async def test_pii_message_is_blocked_and_only_the_placeholder_is_stored(catalog, store, caplog):
    fake = FakeGroq()  # any LLM call fails the test
    app, _, retriever = build(catalog, store, fake=fake)
    caplog.set_level(logging.DEBUG)
    text = "My PAN is ABCDE1234F, what is the exit load of Kotak Multi Cap?"
    async with await client_for(app) as c:
        tid = await new_thread(c)
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": text})
        assert r.status_code == 200 and r.json()["type"] == "pii_blocked"
        thread = (await c.get(f"/api/threads/{tid}")).text
    assert "ABCDE1234F" not in thread
    user = store.get(tid).messages[0]
    assert user.content == WITHHELD_PLACEHOLDER
    assert store.get(tid).title == WITHHELD_PLACEHOLDER
    assert fake.requests == [] and retriever.requests == []
    assert "ABCDE1234F" not in caplog.text and text not in caplog.text


async def test_unexpected_error_is_generic_and_logs_no_text(catalog, store, caplog):
    class Broken:
        async def answer(self, ctx, text):
            raise RuntimeError(f"boom while handling {text}")

    app, _, _ = build(catalog, store, pipeline=Broken())
    caplog.set_level(logging.DEBUG)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": "secret question"})
    assert r.status_code == 500 and r.json() == {"detail": GENERIC_ERROR}
    assert "RuntimeError" in caplog.text and "secret question" not in caplog.text
    assert store.get(tid).messages == ()  # nothing half-written


# ------------------------------------------------------------------------------ conversation state
async def test_follow_up_state_is_kept_in_the_database(catalog, store):
    app, _, retriever = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        await c.post(f"/api/threads/{tid}/messages",
                     json={"content": "Exit load of Kotak Multi Cap?"})
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": "and its benchmark?"})
        assert "Multicap 50:25:25" in r.json()["answer"]
        r = await c.post(f"/api/threads/{tid}/messages",
                         json={"content": "And for Bandhan Small Cap?"})  # inherits benchmark
    assert [q.scheme_id for q in retriever.requests] == [KOTAK, KOTAK, "bandhan-small-cap"]
    assert retriever.requests[2].intent.fields == ("benchmark",)
    assert store.get(tid).active_scheme_id == "bandhan-small-cap"


async def test_clarifying_question_round_trip(catalog, store):
    app, _, retriever = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        r = await c.post(f"/api/threads/{tid}/messages",
                         json={"content": "What is the exit load of the large cap fund?"})
        assert r.json()["type"] == "clarify"
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": "the ICICI one"})
        assert r.json()["type"] == "answer"
    assert retriever.requests[0].scheme_id == "icici-large-cap"
    assert retriever.requests[0].intent.fields == ("exit_load",)


async def test_history_is_sent_to_the_model(catalog, store):
    fake = FakeGroq(answer("The expense ratio of Kotak Multi Cap Fund is 0.70%."),
                    answer("The exit load of Kotak Multi Cap Fund is 1% within 1 year."))
    app, _, _ = build(catalog, store, fake=fake)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        await c.post(f"/api/threads/{tid}/messages", json={"content": "Kotak Multi Cap TER?"})
        await c.post(f"/api/threads/{tid}/messages", json={"content": "and its exit load?"})
    sent = fake.requests[1]["messages"]
    assert {"role": "user", "content": "Kotak Multi Cap TER?"} in sent
    assert {"role": "assistant",
            "content": "The expense ratio of Kotak Multi Cap Fund is 0.70%."} in sent


async def test_threads_survive_a_restart(catalog, tmp_path):
    url = f"sqlite:///{(tmp_path / 'restart.db').as_posix()}"
    app, _, _ = build(catalog, ThreadStore(make_engine(url)))
    async with await client_for(app) as c:
        tid = await new_thread(c)
        await c.post(f"/api/threads/{tid}/messages", json={"content": "Kotak Multi Cap TER?"})
    app2, _, retriever = build(catalog, ThreadStore(make_engine(url)))
    async with await client_for(app2) as c:
        assert len((await c.get(f"/api/threads/{tid}")).json()["messages"]) == 2
        await c.post(f"/api/threads/{tid}/messages", json={"content": "and its exit load?"})
    assert retriever.requests[0].scheme_id == KOTAK  # active scheme came from the database


# ------------------------------------------------------------------------------ meta, health, docs
async def test_meta(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        meta = (await c.get("/api/meta")).json()
    assert meta["disclaimer"] == "Facts-only. No investment advice."
    assert meta["examples"] == [
        "What is the expense ratio of Parag Parikh Flexi Cap Fund?",
        "What is the exit load of Kotak Multi Cap Fund?",
        "How do I download my capital gains statement?",
    ]
    assert "19 mutual fund schemes" in meta["welcome"]
    assert len(meta["schemes"]) == 19 and meta["max_input_chars"] == 500
    kotak = next(s for s in meta["schemes"] if s["id"] == KOTAK)
    assert kotak["url"] == catalog.schemes[KOTAK].url and kotak["amc"] == "Kotak Mutual Fund"
    assert "PAN" in meta["pii_notice"]


async def test_health(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        health = (await c.get("/api/health")).json()
    assert health["index_loaded"] is False  # the fake retriever has no indexes
    assert health["status"] == "degraded" and health["llm_configured"] is False
    assert health["last_ingestion"] == "2026-11-09"
    assert health["sources"]["groww-kotak-multi-cap"] == "2026-11-09"


async def test_openapi_lists_every_endpoint(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        paths = (await c.get("/openapi.json")).json()["paths"]
    assert set(paths) == {"/api/threads", "/api/threads/{thread_id}",
                          "/api/threads/{thread_id}/messages", "/api/meta", "/api/health"}
    assert set(paths["/api/threads/{thread_id}"]) == {"get", "patch", "delete"}


# ------------------------------------------------------------------------------ rename
async def test_rename_thread(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        r = await c.patch(f"/api/threads/{tid}", json={"title": "  Kotak   costs  "})
        assert r.status_code == 200 and r.json() == {"id": tid, "title": "Kotak costs"}
        # The first message's automatic title does not overwrite a rename.
        await c.post(f"/api/threads/{tid}/messages",
                     json={"content": "What is the expense ratio of Kotak Multi Cap?"})
        assert (await c.get(f"/api/threads/{tid}")).json()["title"] == "Kotak costs"
        r = await c.patch(f"/api/threads/{tid}", json={"title": "x" * 150})
        assert r.status_code == 200 and len(r.json()["title"]) == 60


async def test_rename_validation_and_missing_thread(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        assert (await c.patch(f"/api/threads/{tid}", json={"title": "   "})).status_code == 422
        assert (await c.patch(f"/api/threads/{tid}", json={})).status_code == 422
        r = await c.patch("/api/threads/nope", json={"title": "A"})
        assert r.status_code == 404


async def test_rename_refuses_pii_and_never_echoes_it(catalog, store, caplog):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        tid = await new_thread(c)
        with caplog.at_level(logging.DEBUG):
            r = await c.patch(f"/api/threads/{tid}", json={"title": "PAN ABCDE1234F"})
        assert r.status_code == 422 and "ABCDE1234F" not in r.text
        assert "ABCDE1234F" not in caplog.text
        assert (await c.get(f"/api/threads/{tid}")).json()["title"] is None


async def test_cors_allows_only_the_frontend_origin(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        ok = await c.options("/api/threads", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
        bad = await c.options("/api/threads", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers
