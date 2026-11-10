"""Phase 8 concurrency: many threads at once, each thread strictly in order (architecture §7.3).

`SlowEcho` stands in for the pipeline: it sleeps a random time (so requests interleave) and
answers "<thread tag> #<n> saw <k>", where k is the history length it was given. If two messages
of one thread ever ran at the same time, two replies would report the same k; if threads leaked
into each other, a reply would carry another thread's tag.
"""

import asyncio
import random
import re

import httpx
import pytest

from app.api.deps import Services, ThreadLocks
from app.config import get_settings
from app.main import create_app
from app.pipeline.catalog import Catalog
from app.pipeline.orchestrator import PipelineResult
from app.pipeline.templates import Reply
from app.store.db import make_engine
from app.store.threads import ThreadStore

THREADS, PER_THREAD = 10, 5
URL = "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth"


class SlowEcho:
    def __init__(self):
        self.active: dict[str, int] = {}  # thread tag -> calls in progress
        self.max_parallel_threads = 0
        self.overlap_within_thread = False

    async def answer(self, ctx, text):
        tag = text.split()[0]
        self.active[tag] = self.active.get(tag, 0) + 1
        if self.active[tag] > 1:
            self.overlap_within_thread = True
        busy = sum(1 for v in self.active.values() if v)
        self.max_parallel_threads = max(self.max_parallel_threads, busy)
        await asyncio.sleep(random.uniform(0.001, 0.02))
        self.active[tag] -= 1
        reply = Reply("answer", f"{text} saw {len(ctx.history)}.", URL, None)
        return PipelineResult(reply, "answer", text, None, (), {}, None)


@pytest.fixture
def setup(tmp_path):
    store = ThreadStore(make_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}"))
    echo = SlowEcho()
    # One test client sends all 50 messages; the per-IP rate limit is tested in test_ops.py.
    base = get_settings()
    settings = base.model_copy(update={
        "api": base.api.model_copy(update={"rate_limit_per_minute": 1000})})
    services = Services(settings=settings, pipeline=echo, store=store,
                        catalog=Catalog.load())
    app = create_app(services=services)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, echo, services


async def test_fifty_messages_across_ten_threads(setup):
    client, echo, services = setup
    random.seed(7)
    async with client as c:
        ids = [(await c.post("/api/threads")).json()["id"] for _ in range(THREADS)]

        async def conversation(i, tid):
            # one user typing: each message after the previous reply
            for n in range(1, PER_THREAD + 1):
                r = await c.post(f"/api/threads/{tid}/messages", json={"content": f"T{i} #{n}"})
                assert r.status_code == 200

        await asyncio.gather(*(conversation(i, tid) for i, tid in enumerate(ids)))
        threads = [(await c.get(f"/api/threads/{tid}")).json() for tid in ids]

    assert echo.max_parallel_threads > 1, "threads were not processed concurrently"
    assert not echo.overlap_within_thread
    for i, thread in enumerate(threads):
        msgs = thread["messages"]
        assert len(msgs) == 2 * PER_THREAD
        assert [m["role"] for m in msgs] == ["user", "assistant"] * PER_THREAD
        for n in range(PER_THREAD):  # complete, in order, only its own messages
            assert msgs[2 * n]["content"] == f"T{i} #{n + 1}"
            assert msgs[2 * n + 1]["content"] == f"T{i} #{n + 1} saw {min(2 * n, 10)}."
    assert len(services.locks) == 0


async def test_a_burst_to_one_thread_is_serialised(setup):
    """Five messages sent at once to the same thread (double-clicks, retries): each is
    processed after the previous one finished and saw its full history."""
    client, echo, _ = setup
    async with client as c:
        tid = (await c.post("/api/threads")).json()["id"]
        other = (await c.post("/api/threads")).json()["id"]
        sends = [c.post(f"/api/threads/{tid}/messages", json={"content": f"B #{n}"})
                 for n in range(1, 6)]
        sends += [c.post(f"/api/threads/{other}/messages", json={"content": f"O #{n}"})
                  for n in range(1, 6)]
        results = await asyncio.gather(*sends)
        assert all(r.status_code == 200 for r in results)
        msgs = (await c.get(f"/api/threads/{tid}")).json()["messages"]

    assert not echo.overlap_within_thread
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 5
    seen = []
    for n in range(5):
        question, reply = msgs[2 * n]["content"], msgs[2 * n + 1]["content"]
        assert question.startswith("B #")
        assert reply.startswith(question + " saw ")  # each reply sits right after its question
        seen.append(int(re.search(r"saw (\d+)", reply).group(1)))
    assert seen == [0, 2, 4, 6, 8]  # each run saw every earlier turn: no overlap
    assert sorted(m["content"] for m in msgs[::2]) == [f"B #{n}" for n in range(1, 6)]


async def test_delete_waits_for_a_reply_in_progress(setup):
    client, echo, services = setup
    async with client as c:
        tid = (await c.post("/api/threads")).json()["id"]
        send = asyncio.create_task(c.post(f"/api/threads/{tid}/messages",
                                          json={"content": "D #1"}))
        await asyncio.sleep(0)  # let the message take the thread's lock first
        delete = await c.delete(f"/api/threads/{tid}")
        sent = await send
    assert sent.status_code == 200 and delete.status_code == 204
    assert services.store.get(tid) is None  # nothing written after the delete


async def test_thread_locks_are_released():
    locks = ThreadLocks()
    async with locks.hold("a"):
        assert len(locks) == 1
        async with locks.hold("b"):
            assert len(locks) == 2
    assert len(locks) == 0
