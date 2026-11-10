"""A small, complete corpus folder for Phase 11 tests: corpus.db, parsed/, raw/, Chroma (fake
embedder), BM25 and the index manifest, built by the real indexer."""

import json
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.store.db import init_db, make_engine, session_factory
from app.store.models import CORPUS_TABLES, SchemeFact, Source
from ingest.indexer import ChromaVectorIndex, build_indexes
from tests.test_indexer import CORPUS, FakeEmbedder

SCHEMES = {
    "kotak-multi-cap": {"expense_ratio": "0.70%", "exit_load": "1% within 1 year",
                        "min_sip": "₹100", "riskometer": "Very High",
                        "benchmark": "Nifty 500 Multicap 50:25:25 TRI"},
    "hdfc-flexi-cap": {"expense_ratio": None, "exit_load": "1% within 1 year",
                       "min_sip": "₹100", "riskometer": "Very High",
                       "benchmark": "Nifty 500 TRI"},
}


def corpus_settings(tmp_path: Path, data_dir: Path):
    return get_settings().model_copy(update={
        "corpus_database_url": f"sqlite:///{(data_dir / 'corpus.db').as_posix()}",
        "database_url": f"sqlite:///{(tmp_path / 'threads.db').as_posix()}",
    })


def make_corpus(data_dir: Path, settings, *, fetched=datetime(2026, 11, 9, 4, 0),
                chunks=CORPUS, schemes=SCHEMES, embedder=None) -> FakeEmbedder:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.corpus_database_url)
    init_db(engine, CORPUS_TABLES)
    with session_factory(engine).begin() as s:
        s.add(Source(id="groww-kotak-multi-cap", type="scheme_page", sha256="a" * 64,
                     url="https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth",
                     snapshot_path=str(data_dir / "raw" / "groww-kotak-multi-cap" /
                                       f"{fetched:%Y-%m-%d}.html"),
                     last_status="fetched", fetched_at=fetched))
        s.flush()  # the facts' foreign key needs the source row first
        for sid, facts in schemes.items():
            s.add(SchemeFact(scheme_id=sid, scheme_name=sid, source_url="https://x",
                             source_id="groww-kotak-multi-cap",
                             fetched_at=fetched, **facts))
    engine.dispose()
    (data_dir / "parsed").mkdir(exist_ok=True)
    (data_dir / "parsed" / "groww-kotak-multi-cap.json").write_text(
        json.dumps({"source_id": "groww-kotak-multi-cap", "sections": []}), encoding="utf-8")
    raw = data_dir / "raw" / "groww-kotak-multi-cap"
    raw.mkdir(parents=True, exist_ok=True)
    for day in ("2026-11-08", f"{fetched:%Y-%m-%d}"):
        (raw / f"{day}.html").write_text(f"<html>{day}</html>", encoding="utf-8")
        (raw / f"{day}.meta.json").write_text("{}", encoding="utf-8")
    embedder = embedder or FakeEmbedder()
    build_indexes(list(chunks), settings,
                  ChromaVectorIndex(data_dir / "chroma", embedder, fresh=True), embedder,
                  data_dir)
    return embedder
