import json
import re
import zlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.config import load_settings
from ingest.chunker import Chunk, chunk_document, token_counter
from ingest.indexer import (
    BM25_NAME,
    MANIFEST_NAME,
    BM25Index,
    ChromaVectorIndex,
    SentenceTransformerEmbedder,
    bm25_tokenize,
    build_indexes,
    chroma_where,
)
from ingest.parsers import parse_groww

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_settings()


class FakeEmbedder:
    """Deterministic hashed bag-of-words vectors; records how many passages it embedded."""

    model_name = "fake-embedder"
    dimension = 64

    def __init__(self):
        self.embedded: list[str] = []

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dimension)
        for tok in bm25_tokenize(text):
            v[zlib.crc32(tok.encode()) % self.dimension] += 1
        return v / (np.linalg.norm(v) or 1)

    def embed_passages(self, texts):
        self.embedded += list(texts)
        return np.array([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def chunk(source_id, n, text, **kw):
    scheme = kw.pop("scheme_id", None)
    doc_type = kw.pop("doc_type", "scheme_page" if scheme else "amfi")
    return Chunk(
        chunk_id=f"{source_id}::sec::{n}",
        text=text,
        source_id=source_id,
        source_url=f"https://example.org/{source_id}",
        scheme_id=scheme,
        amc=None,
        doc_type=doc_type,
        section="sec",
        section_kind="facts" if scheme else "prose",
        fetched_at=kw.pop("fetched_at", "2026-11-09T00:00:00+00:00"),
        as_of=None,
        chunker_version=SETTINGS.chunking.version,
    )


CORPUS = [
    chunk("groww-kotak", 0, "Kotak Multi Cap exit load 1% within 1 year", scheme_id="kotak"),
    chunk("groww-kotak", 1, "Kotak Multi Cap expense ratio 0.70%", scheme_id="kotak"),
    chunk("groww-hdfc", 0, "HDFC Flexi Cap exit load 1% within 1 year", scheme_id="hdfc"),
    chunk("amfi-exit-load", 0, "An exit load is charged on early redemption of units"),
]


@pytest.fixture
def env(tmp_path):
    embedder = FakeEmbedder()
    vector = ChromaVectorIndex(tmp_path / "chroma", embedder)
    return tmp_path, embedder, vector


def build(env, chunks, settings=SETTINGS, rebuild=False):
    data_dir, embedder, vector = env
    return build_indexes(chunks, settings, vector, embedder, data_dir, rebuild)


# --------------------------------------------------------------------------- vector index


def test_collection_uses_cosine_space_and_scores_are_similarities(env):
    _, embedder, vector = env
    build(env, CORPUS)
    assert vector.space == "cosine"
    target = CORPUS[1]
    (top, *_) = vector.query_embedding(embedder._vec(target.text), k=4)
    assert top.chunk_id == target.chunk_id
    assert top.score == pytest.approx(1.0, abs=1e-5)


def test_first_build_indexes_every_chunk_and_writes_manifest(env):
    data_dir, embedder, vector = env
    summary = build(env, CORPUS)
    assert vector.count() == len(CORPUS) and len(embedder.embedded) == len(CORPUS)
    assert summary.sources_embedded == ["amfi-exit-load", "groww-hdfc", "groww-kotak"]
    manifest = json.loads((data_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["settings"]["embedding_model"] == "fake-embedder"
    assert manifest["settings"]["dimension"] == 64
    assert manifest["settings"]["chunker_version"] == SETTINGS.chunking.version
    assert manifest["settings"]["distance"] == "cosine"
    assert set(manifest["sources"]) == {"amfi-exit-load", "groww-hdfc", "groww-kotak"}
    assert manifest["chunk_count"] == len(CORPUS)


def test_reingesting_unchanged_corpus_neither_duplicates_nor_reembeds(env):
    _, embedder, vector = env
    build(env, CORPUS)
    first_ids = sorted(vector.collection.get(include=[])["ids"])
    embedder.embedded.clear()
    summary = build(env, CORPUS)
    assert embedder.embedded == []
    assert summary.sources_embedded == [] and len(summary.sources_unchanged) == 3
    assert sorted(vector.collection.get(include=[])["ids"]) == first_ids
    assert vector.count() == len(CORPUS)


def test_newer_fetch_date_updates_metadata_without_reembedding(env):
    _, embedder, vector = env
    build(env, CORPUS)
    embedder.embedded.clear()
    refreshed = [replace(c, fetched_at="2026-11-10T00:00:00+00:00") for c in CORPUS]
    build(env, refreshed)
    assert embedder.embedded == []
    metas = vector.collection.get(include=["metadatas"])["metadatas"]
    assert {m["fetched_at"] for m in metas} == {"2026-11-10T00:00:00+00:00"}


def test_changed_source_is_replaced_and_its_stale_chunks_removed(env):
    _, embedder, vector = env
    build(env, CORPUS)
    embedder.embedded.clear()
    kotak_now = [chunk("groww-kotak", 0, "Kotak Multi Cap exit load 0.5%", scheme_id="kotak")]
    corpus = [c for c in CORPUS if c.source_id != "groww-kotak"] + kotak_now
    summary = build(env, corpus)
    assert summary.sources_embedded == ["groww-kotak"]
    assert embedder.embedded == [kotak_now[0].text]
    assert vector.ids_for_source("groww-kotak") == {"groww-kotak::sec::0"}  # ::1 is gone
    assert vector.count() == len(corpus)


def test_source_removed_from_corpus_is_deleted(env):
    _, _, vector = env
    build(env, CORPUS)
    summary = build(env, [c for c in CORPUS if c.source_id != "groww-hdfc"])
    assert summary.sources_removed == ["groww-hdfc"]
    assert vector.ids_for_source("groww-hdfc") == set()


def test_chunks_missing_from_store_are_restored_even_if_manifest_matches(env):
    """A run that failed between delete and add must be repaired by the next run."""
    _, embedder, vector = env
    build(env, CORPUS)
    vector.delete_source("groww-kotak")
    embedder.embedded.clear()
    summary = build(env, CORPUS)
    assert summary.sources_embedded == ["groww-kotak"]
    assert vector.ids_for_source("groww-kotak") == {"groww-kotak::sec::0", "groww-kotak::sec::1"}


@pytest.mark.parametrize(
    "change",
    [
        {"retrieval": {"embedding_model": "other-model"}},
        {"retrieval": {"query_instruction": "query: "}},
        {"chunking": {"version": "999"}},
    ],
)
def test_manifest_mismatch_rebuilds_from_scratch(env, change):
    _, embedder, vector = env
    build(env, CORPUS)
    vector.collection.upsert(  # a stray vector only a full rebuild would remove
        ids=["stray"], embeddings=[embedder._vec("stray").tolist()], metadatas=[{"source_id": "x"}]
    )
    embedder.embedded.clear()
    data = SETTINGS.model_dump()
    for section, values in change.items():
        data[section].update(values)
    settings = type(SETTINGS)(**data)
    if "embedding_model" in change.get("retrieval", {}):
        embedder.model_name = change["retrieval"]["embedding_model"]
    summary = build(env, CORPUS, settings=settings)
    assert summary.rebuilt and "changed" in summary.rebuild_reason
    assert len(embedder.embedded) == len(CORPUS)
    assert "stray" not in vector.collection.get(include=[])["ids"]
    assert vector.space == "cosine"


def test_rebuild_flag_forces_full_reembed(env):
    _, embedder, _ = env
    build(env, CORPUS)
    embedder.embedded.clear()
    summary = build(env, CORPUS, rebuild=True)
    assert summary.rebuilt and summary.rebuild_reason == "requested"
    assert len(embedder.embedded) == len(CORPUS)


def test_scheme_and_doc_type_filters(env):
    _, _, vector = env
    build(env, CORPUS)
    hits = vector.query("exit load", where={"scheme_id": "kotak"}, k=8)
    assert hits and {h.metadata["scheme_id"] for h in hits} == {"kotak"}
    general = vector.query("exit load", where={"doc_type": ["amfi", "sebi", "rta_help"]}, k=8)
    assert [h.chunk_id for h in general] == ["amfi-exit-load::sec::0"]
    assert chroma_where({"scheme_id": "a", "doc_type": ["x"]}) == {
        "$and": [{"scheme_id": "a"}, {"doc_type": {"$in": ["x"]}}]
    }


def test_upsert_without_embeddings_is_refused(env):
    """Chroma would otherwise download and run its own default embedding model."""
    _, _, vector = env
    with pytest.raises(ValueError, match="precomputed embedding"):
        vector.upsert(CORPUS[:1], None)


def test_optional_none_metadata_is_omitted(env):
    _, _, vector = env
    build(env, CORPUS)
    meta = vector.collection.get(ids=["amfi-exit-load::sec::0"], include=["metadatas"])
    assert "scheme_id" not in meta["metadatas"][0] and "as_of" not in meta["metadatas"][0]


# --------------------------------------------------------------------------- BM25


def test_bm25_tokenizer_keeps_decimals_and_amounts():
    assert bm25_tokenize("Expense ratio: 0.70%. Min SIP ₹100; 1.5 lacs") == [
        "expense", "ratio", "0.70", "min", "sip", "100", "1.5", "lacs",
    ]  # fmt: skip


def test_bm25_is_rebuilt_saved_and_filterable(env):
    data_dir, _, _ = env
    build(env, CORPUS)
    index = BM25Index.load(data_dir / BM25_NAME)
    assert index.ids == [c.chunk_id for c in CORPUS]
    top = index.query("expense ratio 0.70", k=1)[0]
    assert top.chunk_id == "groww-kotak::sec::1"
    hdfc = index.query("exit load", where={"scheme_id": "hdfc"}, k=8)
    assert [h.chunk_id for h in hdfc] == ["groww-hdfc::sec::0"]

    build(env, CORPUS[:2])  # rebuilt from scratch each run: no leftovers
    assert BM25Index.load(data_dir / BM25_NAME).ids == [c.chunk_id for c in CORPUS[:2]]


# --------------------------------------------------------------------------- real model


@pytest.mark.slow
def test_smoke_query_with_real_model(tmp_path):
    """Exit criterion: "exit load Kotak Multi Cap" returns Kotak's Costs and exit load first."""
    parsed = parse_groww((FIXTURES / "groww_kotak_multi_cap.html").read_text(encoding="utf-8"))
    doc = {
        "source_id": "groww-kotak-multi-cap",
        "url": "https://groww.in/mutual-funds/kotak-multi-cap-fund-direct-growth",
        "type": "scheme_page",
        "scheme_id": "kotak-multi-cap",
        "amc": "kotak",
        "title": parsed.facts["scheme_name"],
        "fetched_at": "2026-11-09T00:00:00+00:00",
        "sections": parsed.sections,
    }
    r = SETTINGS.retrieval
    chunks = chunk_document(doc, SETTINGS.chunking, token_counter(r.embedding_model)).chunks
    embedder = SentenceTransformerEmbedder(r.embedding_model, r.query_instruction)
    vector = ChromaVectorIndex(tmp_path / "chroma", embedder)
    build_indexes(chunks, SETTINGS, vector, embedder, tmp_path)

    top = vector.query("exit load Kotak Multi Cap", where={"scheme_id": "kotak-multi-cap"}, k=3)
    assert top[0].metadata["section"] == "Costs and exit load"
    assert 0.5 < top[0].score <= 1.0
    assert re.search(r"Exit load", top[0].text)


# --------------------------------------------------------------------------- run_index


def test_run_index_dates_chunks_from_last_successful_fetch(tmp_path):
    """Unchanged content is not re-parsed, so data/parsed keeps the date the content last
    changed; chunks must carry the newer `sources.fetched_at` (footer = last confirmed)."""
    from datetime import UTC, datetime

    from app.store.db import init_db, make_engine, session_factory
    from app.store.models import Source
    from ingest.indexer import run_index

    parsed_dir = tmp_path / "parsed"
    parsed_dir.mkdir()
    doc = {
        "source_id": "amfi-exit-load",
        "url": "https://www.mutualfundssahihai.com/en/exit-load",
        "type": "amfi",
        "scheme_id": None,
        "amc": None,
        "title": "Exit load",
        "fetched_at": "2026-11-01T00:00:00+00:00",  # when the content last changed
        "sections": [{"heading": "Exit load", "text": "An exit load is a fee " * 10}],
    }
    (parsed_dir / "amfi-exit-load.json").write_text(json.dumps(doc), encoding="utf-8")

    settings = SETTINGS.model_copy(
        update={"corpus_database_url": f"sqlite:///{(tmp_path / 'app.db').as_posix()}"}
    )
    engine = make_engine(settings.corpus_database_url)
    init_db(engine)
    with session_factory(engine).begin() as session:
        session.add(
            Source(
                id="amfi-exit-load",
                url=doc["url"],
                type="amfi",
                fetched_at=datetime(2026, 11, 9, 3, 0, tzinfo=UTC),  # confirmed unchanged today
            )
        )
    engine.dispose()

    embedder = FakeEmbedder()
    vector = ChromaVectorIndex(tmp_path / "chroma", embedder)
    summary = run_index(settings, data_dir=tmp_path, embedder=embedder, vector=vector)

    assert summary.chunks == 1
    (meta,) = vector.collection.get(include=["metadatas"])["metadatas"]
    assert meta["fetched_at"] == "2026-11-09T03:00:00+00:00"
