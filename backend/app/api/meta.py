"""GET /api/meta (what the UI shows on an empty thread) and GET /api/health."""

import json
from datetime import datetime

from fastapi import APIRouter

from app.api.deps import ServicesDep
from app.api.schemas import HealthOut, MetaOut, SchemeOut
from app.corpus import active_corpus_dir
from app.jobs import read_bundle_meta, today_in

router = APIRouter(prefix="/api", tags=["meta"])

DISCLAIMER = "Facts-only. No investment advice."
EXAMPLES = [
    "What is the expense ratio of Parag Parikh Flexi Cap Fund?",
    "What is the exit load of Kotak Multi Cap Fund?",
    "How do I download my capital gains statement?",
]
PII_NOTICE = ("Please don't enter PAN, Aadhaar, account numbers, OTPs, phone numbers or email "
              "addresses.")


@router.get("/meta", response_model=MetaOut)
async def meta(services: ServicesDep) -> MetaOut:
    catalog = services.catalog
    schemes = [
        SchemeOut(id=s.id, name=s.name, amc=catalog.amcs[s.amc].name if s.amc in catalog.amcs
                  else s.amc, category=s.category, url=s.url)
        for s in sorted(catalog.schemes.values(), key=lambda s: s.name)
    ]
    return MetaOut(
        welcome=(f"Hi! I answer factual questions about {len(schemes)} mutual fund schemes, "
                 "with a source for each answer."),
        examples=EXAMPLES,
        disclaimer=DISCLAIMER,
        pii_notice=PII_NOTICE,
        max_input_chars=services.settings.api.max_input_chars,
        schemes=schemes,
    )


@router.get("/health", response_model=HealthOut)
async def health(services: ServicesDep) -> HealthOut:
    pipeline = services.pipeline
    retriever = getattr(pipeline, "retriever", None)
    index_loaded = retriever is not None and getattr(retriever, "vector", None) is not None \
        and getattr(retriever, "bm25", None) is not None
    llm = getattr(pipeline, "generator", None) is not None
    dates = {sid: services.catalog.fetched_at.get(sid) for sid in services.catalog.source_urls}
    known = [d for d in dates.values() if d is not None]
    ops = services.settings.ops
    today = services.today() if services.today else today_in(ops.timezone)
    stale = sorted(sid for sid, d in dates.items()
                   if d is None or (today - d).days > ops.stale_source_days)
    corpus, built_at, bundle_stale = _corpus_info(services, retriever, today)
    return HealthOut(
        status="ok" if index_loaded and llm and not stale and not bundle_stale else "degraded",
        index_loaded=index_loaded,
        llm_configured=llm,
        last_ingestion=max(known) if known else None,
        sources=dates,
        stale_sources=stale,
        stale_after_days=ops.stale_source_days,
        corpus=corpus,
        corpus_built_at=built_at,
        bundle_stale=bundle_stale,
    )


def _corpus_info(services, retriever, today):
    corpus_dir = getattr(retriever, "data_dir", None) or active_corpus_dir()
    meta = read_bundle_meta(corpus_dir)
    refresh = services.settings.data_refresh
    built = None
    try:
        manifest = json.loads((corpus_dir / "index_manifest.json").read_text(encoding="utf-8"))
        built = datetime.fromisoformat(manifest["built_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    stale = False
    if refresh.release_repo:  # this server is expected to receive a bundle every day
        created = datetime.fromisoformat(meta["created_at"]).date() if meta else None
        stale = created is None or (today - created).days > refresh.stale_after_days
    return (f"bundle {meta['id']}" if meta else "local"), built, stale
