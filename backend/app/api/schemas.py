"""Request and response models for the HTTP API (architecture §8)."""

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field, field_serializer, field_validator


def _utc(dt: datetime) -> str:
    """Stored as naive UTC; sent as ISO 8601 with the zone."""
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class ThreadCreated(BaseModel):
    id: str
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return _utc(v)


class MessageOut(BaseModel):
    id: str
    role: str  # user | assistant
    content: str
    type: str | None = None  # assistant: answer, advisory_refusal, pii_blocked, ...
    citation_url: str | None = None
    last_updated: date | None = None
    rendered: str | None = None  # assistant: answer + Source line + footer
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return _utc(v)


class ThreadOut(BaseModel):
    id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]

    @field_serializer("created_at", "updated_at")
    def _ser(self, v: datetime) -> str:
        return _utc(v)


class ThreadUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("title is empty")
        return v


class ThreadSummary(BaseModel):
    id: str
    title: str | None


class MessageIn(BaseModel):
    # Upper bound here only stops absurd payloads; the configured limit (api.max_input_chars,
    # 500) is checked in the handler so it can change without a code change.
    content: str = Field(min_length=1, max_length=10_000)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message is empty")
        return v


class ReplyOut(BaseModel):
    message_id: str  # the assistant message
    user_message_id: str
    type: str
    answer: str
    citation_url: str
    last_updated: date | None
    rendered: str


class SchemeOut(BaseModel):
    id: str
    name: str
    amc: str
    category: str
    url: str


class MetaOut(BaseModel):
    welcome: str
    examples: list[str]
    disclaimer: str
    pii_notice: str
    max_input_chars: int
    schemes: list[SchemeOut]


class HealthOut(BaseModel):
    status: str  # ok | degraded
    index_loaded: bool
    llm_configured: bool
    last_ingestion: date | None
    sources: dict[str, date | None]  # source id -> last successful fetch (IST date)
    # Sources whose last successful fetch is older than ops.stale_source_days (or never).
    stale_sources: list[str] = []
    stale_after_days: int = 7
    # The corpus being served (Phase 11): a pulled bundle, or data/ built on this machine.
    corpus: str | None = None  # "bundle <id>" | "local"
    corpus_built_at: datetime | None = None  # when its index was built
    bundle_stale: bool = False  # pulling is enabled and the bundle is too old

    @field_serializer("corpus_built_at")
    def _ser_built(self, v: datetime | None) -> str | None:
        return _utc(v.astimezone(UTC).replace(tzinfo=None)) if v else None
