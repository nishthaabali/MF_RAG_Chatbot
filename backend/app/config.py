"""Typed settings: config/settings.yaml with environment-variable overrides.

`APP_SETTINGS_FILE` picks another YAML file, e.g. config/settings.production.yaml on Railway
(deployment plan D5): YAML values are constructor arguments, so nested settings such as
`api.cors_origins` can't be overridden by environment variables. Secrets stay in the environment.
"""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_FILE = ROOT_DIR / "config" / "settings.yaml"
SETTINGS_FILE_ENV = "APP_SETTINGS_FILE"
DATA_DIR = ROOT_DIR / "data"


class LLMSettings(BaseModel):
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0
    max_tokens: int = 1024  # max_completion_tokens; reasoning tokens count towards it
    reasoning_effort: str | None = "low"  # gpt-oss models: low | medium | high
    history_messages: int = 6
    timeout_seconds: float = 30
    classifier_llm: bool = True  # second opinion for messages the rules don't settle
    # Account limits (Groq free tier for the model above); the client keeps under them.
    requests_per_minute: int = 30
    requests_per_day: int = 1000
    tokens_per_minute: int = 8000
    tokens_per_day: int = 200000
    max_concurrent: int = 2
    max_wait_seconds: float = 30  # longest a request waits for budget before the fallback
    max_evidence_tokens: int = 1000  # evidence sent per answer, to stay inside tokens/minute


class RetrievalSettings(BaseModel):
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Prefixed to queries only, never to passages (bge retrieval instruction).
    query_instruction: str = "Represent this sentence for searching relevant passages: "
    max_seq_length: int = 512
    vector_top_k: int = 8
    bm25_top_k: int = 8
    final_top_k: int = 5
    min_relevance: float = 0.60


class ChunkingSettings(BaseModel):
    """Section-based chunking (docs/chunking-research.md §6.1)."""

    version: str = "1"  # bump whenever chunk text or ids would change; forces a re-index
    max_tokens: int = 350  # whole chunk incl. header, bge tokenizer; model limit is 512
    min_tokens: int = 12  # shorter sections are banner/nav stubs
    # Scheme-page section headings (regex, matched from the start) left out of the text index
    # because they are answered from SQL: scheme_holdings, scheme_peers.
    exclude_sections: list[str] = Field(
        default_factory=lambda: [r"Portfolio holdings\b", r"Similar funds listed on Groww\b"]
    )


class IngestionSettings(BaseModel):
    # Run by .github/workflows/daily-ingest.yml. GitHub cron is UTC: 04:30 UTC = 10:00 IST.
    # A test keeps this equal to the workflow's schedule.
    schedule_cron: str = "30 4 * * *"
    rate_limit_seconds: float = 2
    user_agent: str = "MutualFundFAQBot/0.1 (facts-only educational project)"
    timeout_seconds: float = 30
    max_retries: int = 3


class ThreadSettings(BaseModel):
    retention_days: int = 30


class APISettings(BaseModel):
    rate_limit_per_minute: int = 20
    max_input_chars: int = 500
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Optional regex for extra allowed origins (Vercel preview deployments). Empty = none.
    # Anchor it and keep it to this project's own preview hostnames.
    cors_origin_regex: str = ""
    # Rate limits are per client IP. Behind a reverse proxy every request comes from the proxy,
    # so set this to true there to use the first X-Forwarded-For address instead. Never enable
    # it without a proxy that overwrites the header: clients could then pick their own key.
    trust_forwarded_for: bool = False
    # Header carrying the real client IP, set by an edge proxy that overwrites it on every
    # request (Railway: x-real-ip). Empty = the socket address. Takes precedence over
    # trust_forwarded_for. Like that flag, never set it where clients can reach the app directly.
    client_ip_header: str = ""


class OpsSettings(BaseModel):
    """Logging, background jobs and health checks (Phase 10, architecture §13)."""

    timezone: str = "Asia/Kolkata"  # for job schedules and "today" in health checks
    log_format: str = "json"  # json | text
    log_level: str = "INFO"
    stale_source_days: int = 7  # /api/health flags sources not fetched for longer than this
    retention_cron: str = "30 3 * * *"  # delete threads idle > threads.retention_days
    summary_cron: str = "59 23 * * *"  # daily summary log line (counts since the last one)
    reload_check_seconds: int = 60  # look for a newly built index; 0 disables hot-reload


class DataRefreshSettings(BaseModel):
    """Pulling the corpus bundle the daily GitHub Actions run publishes (Phase 11)."""

    release_repo: str = ""  # "owner/repo"; empty disables pulling
    release_tag: str = "data-latest"
    bundle_asset: str = "data-bundle.tar.gz"
    manifest_asset: str = "bundle.json"
    pull_cron: str = "45 10 * * *"  # ops.timezone; the workflow starts at 10:00 IST
    keep_bundles: int = 2  # installed bundle folders kept in data/bundles/
    stale_after_days: int = 2  # /api/health flags an older bundle while pulling is enabled
    timeout_seconds: float = 120


class Settings(BaseSettings):
    """Values come from settings.yaml; GROQ_API_KEY, APP_ENV, DATABASE_URL override/supply env."""

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    citation_policy: str = "groww"
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    threads: ThreadSettings = Field(default_factory=ThreadSettings)
    api: APISettings = Field(default_factory=APISettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    data_refresh: DataRefreshSettings = Field(default_factory=DataRefreshSettings)

    # Server-side only: SecretStr keeps it out of repr(), logs and error messages. Read it with
    # .get_secret_value() only where the Groq client is built.
    groq_api_key: SecretStr = SecretStr("")
    app_env: str = "development"
    # Read-only GitHub token for pulling bundles from a private repository (optional).
    data_refresh_token: SecretStr = SecretStr("")
    # Threads and messages. Never bundled or published.
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'app.db').as_posix()}"
    # Ingested corpus: sources, scheme facts, metrics, ... (Phase 11 split it from app.db so
    # the published data bundle can never contain conversations). When a pulled bundle is
    # active, the server uses that bundle's own corpus.db instead (app/corpus.py).
    corpus_database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'corpus.db').as_posix()}"

    @field_validator("database_url", "corpus_database_url")
    @classmethod
    def _anchor_relative_sqlite(cls, value: str) -> str:
        """`sqlite:///data/app.db` means <project root>/data/app.db, whatever the cwd is."""
        prefix = "sqlite:///"
        if value.startswith(prefix):
            path = Path(value[len(prefix):])
            if str(path) not in ("", ":memory:") and not path.is_absolute():
                return f"{prefix}{(ROOT_DIR / path).as_posix()}"
        return value


def settings_file() -> Path:
    """$APP_SETTINGS_FILE (relative to the cwd, else to the project root), or settings.yaml."""
    name = os.environ.get(SETTINGS_FILE_ENV, "").strip()
    if not name:
        return DEFAULT_SETTINGS_FILE
    path = Path(name)
    if not path.is_absolute() and not path.exists() and (ROOT_DIR / path).exists():
        path = ROOT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"{SETTINGS_FILE_ENV} names a missing file: {name}")
    return path


def load_settings(path: Path | None = None) -> Settings:
    """Load YAML defaults, then let environment variables / .env override top-level fields."""
    source = path or settings_file()
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return Settings(**data)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
