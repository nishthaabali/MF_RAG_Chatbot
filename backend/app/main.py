"""FastAPI app (architecture §7-§8).

    cd backend && uvicorn app.main:app            # UI at http://127.0.0.1:8000/, API docs at /docs

The pipeline (embedding model, Chroma, BM25, facts database, Groq client) is built once at
startup and shared by every request; threads and messages live in the same SQLite database
(WAL mode). `create_app(services=...)` lets tests inject a fake pipeline and a temporary store.

At startup the server also switches to JSON logs and starts its background jobs (app/jobs.py):
corpus hot-reload after `python -m ingest`, pulling the daily data bundle (Phase 11),
thread retention and the daily summary. Message
posts are rate-limited per client IP (app/middleware/rate_limit.py).
"""

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import messages, meta, threads
from app.api.deps import Services
from app.config import ROOT_DIR, Settings, get_settings
from app.middleware.rate_limit import RateLimitMiddleware
from app.observability import configure_logging

log = logging.getLogger(__name__)

FRONTEND_DIR = ROOT_DIR / "frontend"

GENERIC_ERROR = "Something went wrong. Please try again."


class UIFiles(StaticFiles):
    """The chat UI, always revalidated. The assets have no version in their names, so without
    this a browser may keep running an old app.js after a deploy (heuristic caching)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def load_pipeline(settings: Settings, pipeline_cls):
    """The full pipeline, or a degraded one when no corpus is installed or it can't be loaded.

    A fresh server volume has no corpus until the first bundle arrives (app/bootstrap.py, then
    the bundle puller). The server still starts: /api/health reports `index_loaded: false`,
    questions get the honest "sources are still being loaded" reply, and the reload check
    switches to the corpus as soon as one lands.
    """
    from app.corpus import MANIFEST_NAME, active_corpus_dir

    corpus_dir = active_corpus_dir()
    if not (corpus_dir / MANIFEST_NAME).is_file():
        log.warning("no corpus installed yet; starting in degraded mode",
                    extra={"corpus": str(corpus_dir)})
        return pipeline_cls.degraded(settings)
    try:
        return pipeline_cls.from_settings(settings)
    except Exception as exc:
        log.error("corpus failed to load; starting in degraded mode",
                  extra={"corpus": str(corpus_dir), "exc_type": type(exc).__name__,
                         "detail": str(exc)[:200]})
        return pipeline_cls.degraded(settings)


def build_services(settings: Settings) -> Services:
    """Load everything once: indexes, facts, catalog, Groq client, thread store."""
    from app.pipeline.orchestrator import AnswerPipeline
    from app.store.db import make_engine
    from app.store.threads import ThreadStore

    pipeline = load_pipeline(settings, AnswerPipeline)
    if pipeline.generator is None:
        log.warning("GROQ_API_KEY is not set: answers come from templates only")
    store = ThreadStore(make_engine(settings.database_url))
    return Services(settings=settings, pipeline=pipeline, store=store, catalog=pipeline.catalog)


def create_app(services: Services | None = None, settings: Settings | None = None,
               frontend_dir: Path | None = FRONTEND_DIR) -> FastAPI:
    settings = services.settings if services else (settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.jobs import BundlePuller, CorpusReloader, start_jobs

        configure_logging(settings.ops.log_format, settings.ops.log_level)
        if getattr(app.state, "services", None) is None:
            app.state.services = await asyncio.to_thread(build_services, settings)
        services = app.state.services
        app.state.reloader = CorpusReloader(services)
        app.state.puller = BundlePuller(services, app.state.reloader)
        scheduler = start_jobs(services, app.state.reloader, app.state.puller)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="Mutual Fund FAQ Assistant",
        description="Facts-only answers about 19 mutual fund schemes, each with one source. "
                    "No investment advice.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    # Added before CORS, so CORS stays the outer layer and a 429 still carries its headers.
    app.add_middleware(RateLimitMiddleware, limit=settings.api.rate_limit_per_minute,
                       trust_forwarded_for=settings.api.trust_forwarded_for,
                       client_ip_header=settings.api.client_ip_header)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_origin_regex=settings.api.cors_origin_regex or None,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(threads.router)
    app.include_router(messages.router)
    app.include_router(meta.router)

    # The chat UI (Phase 9): plain HTML/CSS/JS served from the same origin as the API, so no
    # CORS or build step is needed. Mounted last so /api/* and /docs take precedence.
    if frontend_dir is not None and (frontend_dir / "index.html").is_file():
        app.mount("/", UIFiles(directory=frontend_dir, html=True), name="frontend")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Type and location only: an exception message could echo the user's text.
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{frame.filename}:{frame.lineno}" if frame else "?"
        log.error("unhandled %s at %s on %s %s", type(exc).__name__, where,
                  request.method, request.url.path)
        return JSONResponse({"detail": GENERIC_ERROR}, status_code=500)

    return app


app = create_app()
