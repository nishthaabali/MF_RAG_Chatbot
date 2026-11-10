"""One shared Groq client for the classifier and the answer generator.

It keeps the service inside the account's limits (requests and tokens per minute and per day)
instead of discovering them through 429s, bounds concurrent calls with a semaphore, retries
once on rate-limit / server / network errors (honouring `retry-after`), and logs token usage
per call. It never logs message text.

With openai/gpt-oss-120b on the free tier (8K tokens/minute) one answer costs ~1-2.5K tokens, so
roughly 3-5 answers fit in a minute; beyond that a request waits up to `max_wait_seconds` and
then fails with `LLMUnavailable`, and the pipeline answers from a deterministic template.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import LLMSettings

log = logging.getLogger(__name__)

EXPECTED_COMPLETION_TOKENS = 350  # reserved per call before the real usage is known


class LLMUnavailable(Exception):
    """The call could not be made or failed after its retry. Carries no message text."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class ChatResult:
    content: str
    finish_reason: str | None
    usage: Usage
    latency_ms: int


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Cheap upper-ish estimate (~4 characters per token, plus per-message overhead)."""
    return sum(len(m.get("content", "")) // 4 + 8 for m in messages)


# ------------------------------------------------------------------------------ rate limiter
class RateLimited(Exception):
    pass


class RateLimiter:
    """Sliding-window budget for requests and tokens per minute and per day."""

    def __init__(self, rpm: int, tpm: int, rpd: int, tpd: int, *,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.rpm, self.tpm, self.rpd, self.tpd = rpm, tpm, rpd, tpd
        self.clock, self.sleep = clock, sleep
        self.events: deque[list[float]] = deque()  # [timestamp, tokens]
        self.blocked_until = float("-inf")
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= 86400:
            self.events.popleft()

    def _wait_needed(self, now: float, tokens: int) -> float:
        day_requests = len(self.events)
        day_tokens = sum(e[1] for e in self.events)
        if day_requests + 1 > self.rpd or day_tokens + tokens > self.tpd:
            return float("inf")  # daily budget spent: waiting a few seconds won't help
        minute = [e for e in self.events if now - e[0] < 60]
        wait = max(0.0, self.blocked_until - now)
        if len(minute) + 1 > self.rpm:
            wait = max(wait, minute[-self.rpm][0] + 60 - now)
        used = sum(e[1] for e in minute)
        if used + tokens > self.tpm:
            # wait until enough of the oldest calls leave the window
            freed = 0.0
            for e in minute:
                freed += e[1]
                if used - freed + tokens <= self.tpm:
                    wait = max(wait, e[0] + 60 - now)
                    break
            else:
                return float("inf") if tokens > self.tpm else 60.0
        return max(wait, 0.0)

    async def acquire(self, tokens: int, max_wait: float) -> list[float]:
        """Reserve budget for one call; raises RateLimited if it can't be had in time."""
        deadline = self.clock() + max_wait
        async with self._lock:
            while True:
                now = self.clock()
                self._prune(now)
                wait = self._wait_needed(now, tokens)
                if wait <= 0:
                    event = [now, float(tokens)]
                    self.events.append(event)
                    return event
                if now + wait > deadline:
                    raise RateLimited(f"needs {wait:.0f}s")
                await self.sleep(wait)

    @staticmethod
    def settle(event: list[float], actual_tokens: int) -> None:
        """Replace the estimate with the real usage once the response is in."""
        event[1] = float(actual_tokens)

    def block_for(self, seconds: float) -> None:
        """A 429 arrived anyway: make no new calls for `seconds`."""
        self.blocked_until = max(self.blocked_until, self.clock() + seconds)


# ------------------------------------------------------------------------------ client
class LLMClient:
    def __init__(self, settings: LLMSettings, api_key: str = "", *, client=None,
                 limiter: RateLimiter | None = None, retry_delay: float = 1.0):
        self.settings = settings
        self.retry_delay = retry_delay  # pause before the retry when there is no retry-after
        if client is None:
            if not api_key:
                raise ValueError("GROQ_API_KEY is not set")
            from groq import AsyncGroq

            client = AsyncGroq(api_key=api_key, timeout=settings.timeout_seconds, max_retries=0)
        self.client = client
        self.limiter = limiter or RateLimiter(
            settings.requests_per_minute, settings.tokens_per_minute,
            settings.requests_per_day, settings.tokens_per_day,
        )
        self.semaphore = asyncio.Semaphore(settings.max_concurrent)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        json_schema: dict[str, Any] | None = None,
        max_completion_tokens: int | None = None,
    ) -> ChatResult:
        """One chat completion, with at most one retry. Raises LLMUnavailable on failure."""
        s = self.settings
        max_tokens = max_completion_tokens or s.max_tokens
        estimate = estimate_tokens(messages) + min(max_tokens, EXPECTED_COMPLETION_TOKENS)
        kwargs: dict[str, Any] = {
            "model": s.model,
            "messages": messages,
            "temperature": s.temperature,
            "max_completion_tokens": max_tokens,
        }
        if s.reasoning_effort:
            kwargs["reasoning_effort"] = s.reasoning_effort
            kwargs["include_reasoning"] = False  # reasoning text is never shown or stored
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": json_schema.get("title", "reply"), "strict": True,
                                "schema": json_schema},
            }

        for attempt in (1, 2):
            try:
                event = await self.limiter.acquire(estimate, s.max_wait_seconds)
            except RateLimited as exc:
                log.warning("llm %s: local rate budget exhausted (%s)", purpose, exc)
                raise LLMUnavailable("rate_budget") from exc
            started = time.monotonic()
            try:
                async with self.semaphore:
                    response = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                retry_after = _retry_after(exc)
                kind = _error_kind(exc)
                self.limiter.settle(event, estimate if kind == "rate_limit" else 0)
                log.warning("llm %s: %s (attempt %d)", purpose, kind, attempt)
                pause = retry_after if retry_after is not None else self.retry_delay
                if kind == "rate_limit":
                    self.limiter.block_for(retry_after if retry_after is not None else 10)
                retryable = kind in ("rate_limit", "server", "network")
                if attempt == 1 and retryable and pause <= s.max_wait_seconds:
                    await asyncio.sleep(pause)
                    continue
                raise LLMUnavailable(kind) from exc

            latency = int((time.monotonic() - started) * 1000)
            usage = _usage(getattr(response, "usage", None))
            self.limiter.settle(event, usage.total_tokens or estimate)
            choice = response.choices[0]
            log.info(
                "llm call %s",
                json.dumps({
                    "purpose": purpose, "model": s.model, "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "reasoning_tokens": usage.reasoning_tokens, "latency_ms": latency,
                    "finish_reason": choice.finish_reason,
                }),
            )
            return ChatResult(choice.message.content or "", choice.finish_reason, usage, latency)
        raise LLMUnavailable("retries")  # pragma: no cover - loop always returns or raises


def _usage(raw) -> Usage:
    if raw is None:
        return Usage()
    details = getattr(raw, "completion_tokens_details", None)
    return Usage(
        int(getattr(raw, "prompt_tokens", 0) or 0),
        int(getattr(raw, "completion_tokens", 0) or 0),
        int(getattr(raw, "total_tokens", 0) or 0),
        int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
    )


def _error_kind(exc: Exception) -> str:
    try:
        import groq
    except ImportError:  # pragma: no cover
        groq = None
    if groq is not None:
        if isinstance(exc, groq.RateLimitError):
            return "rate_limit"
        if isinstance(exc, groq.APITimeoutError | groq.APIConnectionError):
            return "network"
        if isinstance(exc, groq.InternalServerError):
            return "server"
        if isinstance(exc, groq.APIStatusError):
            return "server" if exc.status_code >= 500 else f"http_{exc.status_code}"
    if isinstance(exc, TimeoutError | ConnectionError):
        return "network"
    return "error"


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
