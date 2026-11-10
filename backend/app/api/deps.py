"""Shared API services: the pipeline and indexes (loaded once), the thread store, and the
per-thread locks that keep messages of one thread in order (architecture §7.3)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.observability import DailyStats
from app.pipeline.catalog import Catalog
from app.store.threads import ThreadStore


class ThreadLocks:
    """One asyncio.Lock per thread id. Messages to the same thread run one at a time and in
    arrival order (asyncio.Lock is FIFO); different threads never wait for each other.

    A lock is dropped as soon as nobody holds or waits for it, so the dict stays small.
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, thread_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        self._users[thread_id] = self._users.get(thread_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[thread_id] -= 1
            if self._users[thread_id] == 0:
                del self._users[thread_id]
                del self._locks[thread_id]

    def __len__(self) -> int:
        return len(self._locks)


@dataclass
class Services:
    settings: Settings
    pipeline: object  # app.pipeline.orchestrator.AnswerPipeline (or a test double)
    store: ThreadStore
    catalog: Catalog
    locks: ThreadLocks = field(default_factory=ThreadLocks)
    stats: DailyStats = field(default_factory=DailyStats)
    today: Callable[[], date] | None = None  # tests pin "today"; else ops.timezone's date


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
