"""Structured logging and daily counters (architecture §13).

The server logs one JSON object per line. Each answered message produces one `turn` record
with whitelisted fields only (`turn_fields`): ids, route, scores, failure codes, latency and
token counts. Message text never reaches a log record: the trace it is built from holds none,
and anything outside the whitelist is dropped, so a future trace key can't leak text either.

Exceptions are logged as type and location only. An exception message or a traceback's source
lines can echo the user's text, so the formatter leaves both out.
"""

import json
import logging
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_RECORD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
    "color_message",  # uvicorn's copy of msg with ANSI colour codes
}
_HANDLER_FLAG = "_mf_faq_handler"

# Trace keys allowed into a `turn` log record (all ids, codes, numbers or booleans).
TURN_FIELDS = (
    "route", "reply_type", "query_class", "classified_by", "scheme_id", "pii_kinds",
    "retrieval_status", "retrieval_top_score", "no_evidence_reason", "evidence",
    "history_sent", "intent_inherited", "validator_failures", "validator_fixes",
    "fallback_used", "llm_calls", "prompt_tokens", "completion_tokens",
)


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": _utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                out[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _exc, tb = record.exc_info
            out["exc_type"] = exc_type.__name__
            frames = traceback.extract_tb(tb) if tb else []
            if frames:
                out["exc_at"] = f"{frames[-1].filename}:{frames[-1].lineno}"
        return json.dumps(out, default=str, ensure_ascii=False)


def configure_logging(log_format: str = "json", level: str = "INFO") -> None:
    """Route all logs (including uvicorn's) through one stderr handler. Idempotent."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(JsonFormatter() if log_format == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for name in ("httpx", "httpcore", "chromadb", "sentence_transformers", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)


def turn_fields(trace: dict[str, Any]) -> dict[str, Any]:
    """The whitelisted part of a pipeline trace, for the `turn` log record."""
    return {k: trace[k] for k in TURN_FIELDS if k in trace}


class DailyStats:
    """Counts since the last summary: replies by route, validator failures, no-evidence.

    In-memory and per process; a restart starts a new count (the summary says since when).
    """

    def __init__(self, now=lambda: datetime.now(UTC)):
        self._now = now
        self.reset()

    def reset(self) -> None:
        self.since = self._now()
        self.turns = 0
        self.routes: Counter[str] = Counter()
        self.retrieved = 0  # turns that reached retrieval
        self.no_evidence = 0
        self.generated = 0  # turns where the model answered at least once
        self.validator_failed = 0  # ... and at least one validator check failed
        self.fallbacks: Counter[str] = Counter()
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record(self, trace: dict[str, Any]) -> None:
        self.turns += 1
        self.routes[str(trace.get("reply_route") or trace.get("route") or "unknown")] += 1
        if "retrieval_status" in trace:
            self.retrieved += 1
            if trace.get("reply_route") == "no_evidence":
                self.no_evidence += 1
        if trace.get("llm_calls"):
            self.generated += 1
            if trace.get("validator_failures"):
                self.validator_failed += 1
        if trace.get("fallback_used"):
            self.fallbacks[str(trace["fallback_used"])] += 1
        self.prompt_tokens += int(trace.get("prompt_tokens") or 0)
        self.completion_tokens += int(trace.get("completion_tokens") or 0)

    def summary(self) -> dict[str, Any]:
        def rate(n: int, d: int) -> float | None:
            return round(n / d, 4) if d else None

        return {
            "since": self.since.isoformat(timespec="seconds"),
            "turns": self.turns,
            "routes": dict(sorted(self.routes.items())),
            "validator_failure_rate": rate(self.validator_failed, self.generated),
            "validator_failed": self.validator_failed,
            "generated": self.generated,
            "no_evidence_rate": rate(self.no_evidence, self.retrieved),
            "no_evidence": self.no_evidence,
            "retrieved": self.retrieved,
            "fallbacks": dict(sorted(self.fallbacks.items())),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }

    def flush(self) -> dict[str, Any]:
        """The summary so far, then start counting again."""
        out = self.summary()
        self.reset()
        return out
