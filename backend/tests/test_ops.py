"""Phase 10: rate limiting, structured logs without message text, daily summary, retention,
stale-source health, secrets, and corpus hot-reload."""

import asyncio
import io
import json
import logging
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import update

from app.api.deps import Services
from app.config import ROOT_DIR, get_settings
from app.jobs import CorpusReloader, corpus_signature, log_daily_summary, run_retention, start_jobs
from app.main import create_app
from app.middleware.rate_limit import SlidingWindowLimiter
from app.observability import DailyStats, JsonFormatter, configure_logging, turn_fields
from app.pipeline.catalog import Catalog
from app.pipeline.orchestrator import AnswerPipeline, PipelineResult
from app.pipeline.router import Router
from app.pipeline.templates import Reply
from app.store.db import init_db, make_engine
from app.store.models import Thread
from app.store.threads import ThreadStore
from tests.fake_groq import FakeGroq
from tests.test_answer_pipeline import CONFIRMED, KOTAK, FakeRetriever, fact_block
from tests.test_answer_pipeline import pipeline as make_pipeline

FACTS = {"expense_ratio": "0.70%", "exit_load": "Exit load of 1%, if redeemed within 1 year."}
URL = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"
BACKEND = Path(__file__).resolve().parents[1]
WRITER = """
import sys
from pathlib import Path
from app.config import get_settings
from ingest.indexer import ChromaVectorIndex, build_indexes
from tests.test_indexer import CORPUS, FakeEmbedder, chunk
data = Path(sys.argv[1])
extra = chunk("amfi-sip", 0, "A systematic investment plan invests a fixed amount monthly")
e = FakeEmbedder()
build_indexes([*CORPUS, extra], get_settings(), ChromaVectorIndex(data / "chroma", e), e, data)
"""


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: CONFIRMED for sid in cat.source_urls})


@pytest.fixture
def store(tmp_path):
    return ThreadStore(make_engine(f"sqlite:///{(tmp_path / 'threads.db').as_posix()}"))


def settings_with(api=None, **top):
    base = get_settings()
    update_ = dict(top)
    if api:
        update_["api"] = base.api.model_copy(update=api)
    return base.model_copy(update=update_)


def build(catalog, store, *, settings=None, pipeline=None, fake=None, today=None):
    retriever = FakeRetriever(catalog, lambda req: [fact_block(catalog, req.scheme_id or KOTAK,
                                                               FACTS)])
    pipeline = pipeline or make_pipeline(catalog, fake, retriever)
    services = Services(settings=settings or get_settings(), pipeline=pipeline, store=store,
                        catalog=catalog, today=today)
    return create_app(services=services, frontend_dir=None), services


def client(app, ip="10.0.0.1"):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 5000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ------------------------------------------------------------------------------ rate limit
def test_sliding_window_limiter():
    now = [0.0]
    lim = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    assert [lim.hit("a") for _ in range(3)] == [None, None, None]
    assert lim.hit("a") == pytest.approx(60)  # refused; not counted
    assert lim.hit("b") is None  # other clients are independent
    now[0] = 30.0
    assert lim.hit("a") == pytest.approx(30)
    now[0] = 60.0  # the first three fall out of the window
    assert lim.hit("a") is None


def test_limiter_forgets_idle_clients():
    now = [0.0]
    lim = SlidingWindowLimiter(5, 60, clock=lambda: now[0])
    for i in range(500):
        lim.hit(f"ip{i}")
    now[0] = 120.0
    for _ in range(1000):
        lim.hit("busy")
    assert len(lim) == 1


async def test_message_posts_are_rate_limited_per_ip(catalog, store):
    app, _ = build(catalog, store, settings=settings_with(api={"rate_limit_per_minute": 3}))
    async with client(app) as c:
        tid = (await c.post("/api/threads")).json()["id"]
        codes = [(await c.post(f"/api/threads/{tid}/messages",
                               json={"content": "exit load of Kotak Multi Cap?"})).status_code
                 for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": "again"})
        assert r.status_code == 429
        assert 1 <= int(r.headers["retry-after"]) <= 60
        assert "please wait" in r.json()["detail"].lower()
        # Other endpoints are not limited.
        assert (await c.get(f"/api/threads/{tid}")).status_code == 200
        assert (await c.post("/api/threads")).status_code == 201
        assert len(store.get(tid).messages) == 6  # refused posts wrote nothing
    async with client(app, ip="10.0.0.2") as other:
        r = await other.post(f"/api/threads/{tid}/messages", json={"content": "Kotak exit load?"})
        assert r.status_code == 200


async def test_forwarded_for_is_used_only_when_trusted(catalog, store):
    for trusted, expected in ((False, 429), (True, 200)):
        settings = settings_with(api={"rate_limit_per_minute": 1, "trust_forwarded_for": trusted})
        app, _ = build(catalog, store, settings=settings)
        async with client(app) as c:
            tid = (await c.post("/api/threads")).json()["id"]
            url = f"/api/threads/{tid}/messages"
            body = {"content": "Kotak exit load?"}
            assert (await c.post(url, json=body,
                                 headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
            r = await c.post(url, json=body, headers={"X-Forwarded-For": "2.2.2.2, 10.0.0.9"})
            assert r.status_code == expected


async def test_rate_limit_zero_disables_it(catalog, store):
    app, _ = build(catalog, store, settings=settings_with(api={"rate_limit_per_minute": 0}))
    async with client(app) as c:
        tid = (await c.post("/api/threads")).json()["id"]
        for _ in range(5):
            r = await c.post(f"/api/threads/{tid}/messages", json={"content": "Kotak exit load?"})
            assert r.status_code == 200


# ------------------------------------------------------------------------------ logging
MESSAGES = [
    "What is the exit load of Kotak Multi Cap Fund zqxj?",  # factual (template, no LLM)
    "Should I invest in Kotak Multi Cap Fund zqxj?",  # advisory refusal
    "My PAN is ABCDE1234F, what is the exit load of Kotak Multi Cap?",  # PII
    "Who will win the cricket world cup zqxj?",  # out of scope
    "hi zqxj",  # greeting
]


@pytest.fixture
def json_logs():
    """Everything logged during the test, formatted exactly as the server writes it."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old_level)


async def test_logs_never_contain_message_text(catalog, store, json_logs):
    app, _ = build(catalog, store)
    async with client(app) as c:
        tid = (await c.post("/api/threads")).json()["id"]
        for text in MESSAGES:
            r = await c.post(f"/api/threads/{tid}/messages", json={"content": text})
            assert r.status_code == 200
    out = json_logs.getvalue()
    records = [json.loads(line) for line in out.splitlines()]
    turns = [r for r in records if r["msg"] == "turn"]
    assert len(turns) == len(MESSAGES)
    for t in turns:
        assert t["thread_id"] == tid and isinstance(t["latency_ms"], int)
        assert {"route", "reply_route", "reply_type"} <= set(t)
    assert {t["reply_type"] for t in turns} >= {"answer", "advisory_refusal", "pii_blocked"}
    pii = next(t for t in turns if t["reply_type"] == "pii_blocked")
    assert pii["pii_kinds"] == ["pan"]
    for text in MESSAGES:
        assert text not in out
    assert "zqxj" not in out and "ABCDE1234F" not in out


async def test_exception_logs_carry_type_and_place_but_not_the_message(catalog, store, json_logs):
    class Broken:
        async def answer(self, ctx, text):
            raise RuntimeError(f"failed on {text}")

    app, _ = build(catalog, store, pipeline=Broken())
    async with client(app) as c:
        tid = (await c.post("/api/threads")).json()["id"]
        r = await c.post(f"/api/threads/{tid}/messages", json={"content": "secret zqxj"})
    assert r.status_code == 500
    logging.getLogger("t").exception("x", exc_info=RuntimeError("leak zqxj"))
    try:
        raise ValueError("leak zqxj")
    except ValueError:
        logging.getLogger("t").exception("caught")
    out = json_logs.getvalue()
    assert "zqxj" not in out
    caught = [json.loads(line) for line in out.splitlines() if '"caught"' in line][0]
    assert caught["exc_type"] == "ValueError"
    assert caught["exc_at"].rsplit(":", 1)[0].endswith("test_ops.py")


def test_turn_fields_is_a_whitelist():
    trace = {"route": "factual", "scheme_id": KOTAK, "retrieval_top_score": 0.8,
             "question": "leak", "evidence_text": "leak", "fallback_used": "validator"}
    assert turn_fields(trace) == {"route": "factual", "scheme_id": KOTAK,
                                  "retrieval_top_score": 0.8, "fallback_used": "validator"}


def test_configure_logging_is_idempotent_and_json():
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        configure_logging("json", "INFO")
        configure_logging("json", "INFO")
        ours = [h for h in root.handlers if h not in before]
        assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)
        assert logging.getLogger("uvicorn.access").propagate
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(level)


# ------------------------------------------------------------------------------ daily summary
def test_daily_stats_rates_and_reset():
    stats = DailyStats()
    stats.record({"route": "advisory", "reply_route": "advisory"})
    stats.record({"retrieval_status": "ok", "reply_route": "answer", "llm_calls": 1,
                  "validator_failures": [], "prompt_tokens": 900, "completion_tokens": 100})
    stats.record({"retrieval_status": "ok", "reply_route": "template_fallback", "llm_calls": 2,
                  "validator_failures": ["figures#1", "figures#2"],
                  "fallback_used": "validator"})
    stats.record({"retrieval_status": "no_evidence", "reply_route": "no_evidence"})
    s = stats.summary()
    assert s["turns"] == 4
    assert s["routes"] == {"advisory": 1, "answer": 1, "no_evidence": 1, "template_fallback": 1}
    assert s["validator_failure_rate"] == 0.5 and s["no_evidence_rate"] == round(1 / 3, 4)
    assert s["fallbacks"] == {"validator": 1} and s["prompt_tokens"] == 900
    assert stats.flush()["turns"] == 4 and stats.summary()["turns"] == 0
    assert stats.summary()["validator_failure_rate"] is None


async def test_api_turns_feed_the_daily_summary(catalog, store, json_logs):
    app, services = build(catalog, store)
    async with client(app) as c:
        tid = (await c.post("/api/threads")).json()["id"]
        for text in MESSAGES[:3]:
            await c.post(f"/api/threads/{tid}/messages", json={"content": text})
    summary = log_daily_summary(services)
    assert summary["turns"] == 3 and sum(summary["routes"].values()) == 3
    line = [json.loads(x) for x in json_logs.getvalue().splitlines()
            if '"daily summary"' in x][0]
    assert line["summary"]["turns"] == 3
    assert services.stats.summary()["turns"] == 0


# ------------------------------------------------------------------------------ retention
async def test_retention_deletes_only_idle_threads(catalog, store):
    _, services = build(catalog, store)
    old, recent = store.create(), store.create()
    with store.sessions.begin() as s:
        s.execute(update(Thread).where(Thread.id == old.id)
                  .values(updated_at=datetime(2026, 9, 1)))
        s.execute(update(Thread).where(Thread.id == recent.id)
                  .values(updated_at=datetime(2026, 11, 1)))
    deleted = await run_retention(services, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert deleted == 1
    assert store.get(old.id) is None and store.get(recent.id) is not None


# ------------------------------------------------------------------------------ health
async def test_health_flags_sources_older_than_seven_days(catalog, store):
    dates = dict(catalog.fetched_at)
    dates["groww-kotak-multi-cap"] = date(2026, 11, 1)  # 9 days old
    dates["amfi-sip"] = date(2026, 11, 3)  # exactly 7 days: not stale yet
    cat = replace(catalog, fetched_at=dates)
    app, _ = build(cat, store, today=lambda: date(2026, 11, 10))
    async with client(app) as c:
        health = (await c.get("/api/health")).json()
    assert health["stale_sources"] == ["groww-kotak-multi-cap"]
    assert health["stale_after_days"] == 7 and health["status"] == "degraded"

    app, _ = build(catalog, store, today=lambda: date(2026, 11, 10))
    async with client(app) as c:
        assert (await c.get("/api/health")).json()["stale_sources"] == []


# ------------------------------------------------------------------------------ secrets
async def test_groq_key_never_leaves_the_server(catalog, store, json_logs):
    secret = "gsk_TESTSECRET_zz9"
    settings = get_settings().model_copy(update={"groq_api_key": SecretStr(secret)})
    fake = FakeGroq()  # no queued replies: every model call fails -> template fallback
    app, _ = build(catalog, store, settings=settings, fake=fake)
    async with client(app) as c:
        bodies = [(await c.get(p)).text for p in ("/api/meta", "/api/health", "/openapi.json")]
        tid = (await c.post("/api/threads")).json()["id"]
        bodies.append((await c.post(f"/api/threads/{tid}/messages",
                                    json={"content": "Exit load of Kotak Multi Cap?"})).text)
    assert all(secret not in b for b in bodies)
    assert secret not in json_logs.getvalue()
    assert secret not in repr(settings) and secret not in str(settings.model_dump())
    frontend = ROOT_DIR / "frontend"
    for f in frontend.rglob("*"):
        if f.is_file() and f.suffix in (".js", ".html", ".css"):
            text = f.read_text(encoding="utf-8")
            assert "GROQ" not in text.upper(), f


# ------------------------------------------------------------------------------ hot reload
def _echo(tag: str, delay: float = 0.0):
    class Echo:
        catalog = Catalog.load()

        async def answer(self, ctx, text):
            await asyncio.sleep(delay)
            reply = Reply("answer", f"{tag}.", URL, None)
            return PipelineResult(reply, "answer", text, None, (), {}, None)

    return Echo()


def _manifest(data_dir: Path, built_at: str, count: int = 1):
    (data_dir / "index_manifest.json").write_text(
        json.dumps({"built_at": built_at, "chunk_count": count}), encoding="utf-8")


async def test_reload_swaps_only_after_a_new_build_and_in_flight_requests_finish_on_old(
        catalog, store, tmp_path):
    old = _echo("old corpus", delay=0.2)
    new = _echo("new corpus")
    app, services = build(catalog, store, pipeline=old)
    _manifest(tmp_path, "2026-11-09T10:00:00")
    built = []
    reloader = CorpusReloader(services, tmp_path, build=lambda _dir: built.append(1) or new)
    assert await reloader.check() is False and built == []  # nothing new on disk

    async with client(app) as c:
        tid1 = (await c.post("/api/threads")).json()["id"]
        tid2 = (await c.post("/api/threads")).json()["id"]
        in_flight = asyncio.create_task(
            c.post(f"/api/threads/{tid1}/messages", json={"content": "q"}))
        await asyncio.sleep(0.05)  # the request is inside the old pipeline now
        _manifest(tmp_path, "2026-11-10T10:00:00")
        assert await reloader.check() is True
        assert services.pipeline is new and services.catalog is new.catalog
        assert (await in_flight).json()["answer"] == "old corpus."
        r = await c.post(f"/api/threads/{tid2}/messages", json={"content": "q"})
        assert r.json()["answer"] == "new corpus."
    assert await reloader.check() is False and built == [1]  # same build: no second reload


async def test_failed_reload_keeps_serving_and_does_not_retry_the_same_build(
        catalog, store, tmp_path, caplog):
    old = _echo("old corpus")
    app, services = build(catalog, store, pipeline=old)
    calls = []

    def broken(_dir):
        calls.append(1)
        raise RuntimeError("chunk counts differ")

    reloader = CorpusReloader(services, tmp_path, build=broken)
    _manifest(tmp_path, "2026-11-10T10:00:00")
    caplog.set_level(logging.WARNING)
    assert await reloader.check() is False
    assert await reloader.check() is False
    assert services.pipeline is old and calls == [1]
    assert "corpus reload failed" in caplog.text
    _manifest(tmp_path, "2026-11-10T11:00:00")  # the next ingest run is tried again
    assert await reloader.check() is False and calls == [1, 1]


def test_corpus_signature(tmp_path):
    assert corpus_signature(tmp_path) is None
    (tmp_path / "index_manifest.json").write_text("{broken", encoding="utf-8")
    assert corpus_signature(tmp_path) is None
    _manifest(tmp_path, "2026-11-10T10:00:00", 387)
    assert corpus_signature(tmp_path) == "2026-11-10T10:00:00|387"


async def test_real_reload_sees_new_chroma_vectors_and_new_fetch_dates(tmp_path):
    """End to end with Chroma, BM25, the manifest and the facts database (fake embedder)."""
    from app.pipeline.retriever import Retriever
    from app.store.models import Source
    from ingest.indexer import CorpusMismatch, build_indexes
    from tests.test_indexer import CORPUS, FakeEmbedder

    settings = get_settings()
    embedder = FakeEmbedder()
    engine = make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(Source.__table__.insert(), [{
            "id": "groww-kotak-multi-cap", "url": URL, "type": "scheme_page",
            "fetched_at": datetime(2026, 11, 9, 4, 0)}])

    from ingest.indexer import ChromaVectorIndex
    build_indexes(CORPUS, settings, ChromaVectorIndex(tmp_path / "chroma", embedder, fresh=True),
                  embedder, tmp_path)
    retriever = Retriever.from_settings(settings, engine=engine, embedder=embedder,
                                        data_dir=tmp_path, verify=True)
    catalog = retriever.catalog
    pipeline = AnswerPipeline(catalog, Router(catalog), retriever, None)
    assert catalog.fetched_at["groww-kotak-multi-cap"] == date(2026, 11, 9)
    assert retriever.vector.count() == len(CORPUS)

    # "python -m ingest" in another process: a new source, a newer fetch date. It must be a
    # real second process: inside one process Chroma shares its cached system, which would
    # hide the stale-vector problem the reload has to solve.
    assert pipeline.retriever.vector.query("exit load", k=10)  # vectors loaded in this process
    subprocess.run([sys.executable, "-c", WRITER, str(tmp_path)], cwd=BACKEND, check=True,
                   capture_output=True, timeout=120)
    assert retriever.vector.count() == len(CORPUS) + 1  # the old client sees the new count...
    stale = retriever.vector.query("systematic investment plan monthly", k=10)
    assert "amfi-sip::sec::0" not in [h.chunk_id for h in stale]  # ...but not the new vectors
    with engine.begin() as conn:
        conn.execute(update(Source).where(Source.id == "groww-kotak-multi-cap")
                     .values(fetched_at=datetime(2026, 11, 10, 4, 30)))

    fresh = pipeline.with_fresh_corpus(settings, tmp_path)
    hits = fresh.retriever.vector.query("systematic investment plan monthly", k=10)
    assert "amfi-sip::sec::0" in [h.chunk_id for h in hits]
    assert fresh.catalog.fetched_at["groww-kotak-multi-cap"] == date(2026, 11, 10)
    assert fresh.generator is pipeline.generator
    assert fresh.retriever.vector.embedder is embedder  # the model isn't loaded again
    # The old pipeline still answers from what it loaded.
    assert pipeline.catalog.fetched_at["groww-kotak-multi-cap"] == date(2026, 11, 9)
    assert pipeline.retriever.vector.query("exit load", k=10)

    # A manifest that disagrees with the indexes is refused.
    manifest = json.loads((tmp_path / "index_manifest.json").read_text(encoding="utf-8"))
    manifest["chunk_count"] = 99
    (tmp_path / "index_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CorpusMismatch, match="chunk counts differ"):
        pipeline.with_fresh_corpus(settings, tmp_path)
    engine.dispose()


async def test_scheduler_registers_the_jobs(catalog, store, tmp_path):
    _, services = build(catalog, store)
    reloader = CorpusReloader(services, tmp_path, build=lambda _dir: None)
    scheduler = start_jobs(services, reloader)
    try:
        ids = {j.id for j in scheduler.get_jobs()}
        assert ids == {"retention", "daily_summary", "corpus_reload"}
        summary = scheduler.get_job("daily_summary").trigger
        assert str(summary.timezone) == "Asia/Kolkata"
        await asyncio.sleep(0.05)  # retention also runs once at startup
    finally:
        scheduler.shutdown(wait=False)

