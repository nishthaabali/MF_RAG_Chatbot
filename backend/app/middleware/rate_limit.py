"""Per-IP rate limit on POST /api/threads/{id}/messages (Phase 10).

Each message can cost a Groq call, and the free tier allows about six answers a minute for the
whole server, so one client sending fast would use up everyone's budget. The limit is a sliding
window (default 20 messages per minute per IP), kept in memory: it is per process, which is
enough for the single-process deployment (architecture §7.3).

A refused request gets 429 with a friendly `detail` and a `Retry-After` header; it is not
counted against the window. Nothing about the client (IP) or the message is logged.
"""

import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

MESSAGE_PATH = re.compile(r"^/api/threads/[^/]+/messages/?$")
SWEEP_EVERY = 1000  # drop idle clients' entries after this many checks


def too_many(seconds: int) -> str:
    unit = "second" if seconds == 1 else "seconds"
    return (f"You're sending messages faster than the assistant can answer. "
            f"Please wait {seconds} {unit} and try again.")


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._checks = 0

    def hit(self, key: str) -> float | None:
        """Count a request; None if allowed, else the seconds until one would be."""
        now = self.clock()
        self._checks += 1
        if self._checks % SWEEP_EVERY == 0:
            self._sweep(now)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window - now
        hits.append(now)
        return None

    def _sweep(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or h[-1] <= now - self.window]:
            del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)


def client_key(scope: Scope, trust_forwarded_for: bool, ip_header: str = "") -> str:
    """The client's IP: `ip_header` when configured (an edge proxy such as Railway's sets it on
    every request), else the first X-Forwarded-For address if trusted, else the socket peer."""
    headers = scope.get("headers") or ()
    if ip_header:
        wanted = ip_header.strip().lower().encode("latin-1")
        for name, value in headers:
            if name == wanted:
                ip = value.decode("latin-1").split(",")[0].strip()
                if ip:
                    return ip
    if trust_forwarded_for:
        for name, value in headers:
            if name == b"x-forwarded-for":
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: int, window: float = 60.0,
                 trust_forwarded_for: bool = False, client_ip_header: str = "",
                 limiter: SlidingWindowLimiter | None = None):
        self.app = app
        self.limiter = limiter or SlidingWindowLimiter(limit, window)
        self.trust_forwarded_for = trust_forwarded_for
        self.client_ip_header = client_ip_header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (self.limiter.limit > 0 and scope["type"] == "http" and scope["method"] == "POST"
                and MESSAGE_PATH.match(scope["path"])):
            wait = self.limiter.hit(
                client_key(scope, self.trust_forwarded_for, self.client_ip_header))
            if wait is not None:
                seconds = max(1, math.ceil(wait))
                log.warning("rate limited", extra={"limit_per_minute": self.limiter.limit,
                                                   "retry_after_s": seconds})
                response = JSONResponse({"detail": too_many(seconds)}, status_code=429,
                                        headers={"Retry-After": str(seconds)})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
