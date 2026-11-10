"""Vector and keyword indexes over the chunks (architecture §5.4, docs/chunking-research.md §5).

- Embeddings: bge-small-en-v1.5, normalised, max_seq_length 512. The bge query instruction is
  prefixed to queries only, never to passages.
- Vector store: one persistent Chroma collection (data/chroma/) in cosine space, behind the
  `VectorIndex` interface; score = 1 - cosine distance. Embeddings are always supplied by us:
  Chroma falls back to downloading and running its own default model whenever an add or query
  arrives without embeddings, even with `embedding_function=None`, so the wrapper refuses that.
- Incremental: a source is re-embedded only when its chunk ids/texts change (content hash in the
  manifest, verified against the ids actually in Chroma). Otherwise only metadata is updated,
  so `fetched_at` stays current without re-embedding. Sources gone from the corpus are deleted.
- Manifest (data/index_manifest.json): embedding model, dimension, query instruction, chunker
  version. Any mismatch with config rebuilds the collection from scratch.
- BM25: rebuilt over all chunks on every run and pickled with its chunk list (data/bm25.pkl).
"""

import hashlib
import json
import logging
import os
import pickle
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from app.config import DATA_DIR, Settings
from ingest.chunker import Chunk, ChunkResult, chunk_corpus, load_parsed, token_counter

log = logging.getLogger(__name__)

COLLECTION = "chunks"
CHROMA_DIRNAME = "chroma"
MANIFEST_NAME = "index_manifest.json"
BM25_NAME = "bm25.pkl"
BM25_FORMAT = 1
# Fields stored as Chroma metadata (everything on Chunk except the id and the text).
META_FIELDS = [f for f in Chunk.__dataclass_fields__ if f not in ("chunk_id", "text")]

Where = dict[str, str | Sequence[str]]  # field -> value, or list of allowed values


# --------------------------------------------------------------------------- embedder


class Embedder(Protocol):
    model_name: str
    dimension: int

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """bge via sentence-transformers on CPU; loaded on first use."""

    def __init__(self, model_name: str, query_instruction: str, max_seq_length: int = 512):
        self.model_name = model_name
        self.query_instruction = query_instruction
        self.max_seq_length = max_seq_length
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            # Use the cached copy when there is one: otherwise every start makes Hub requests,
            # which took ~60 s on a slow connection. Download only on first use.
            try:
                self._model = SentenceTransformer(
                    self.model_name, device="cpu", local_files_only=True
                )
            except OSError:
                self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.max_seq_length = self.max_seq_length
        return self._model

    @property
    def dimension(self) -> int:
        model = self.model
        get_dim = getattr(model, "get_embedding_dimension", None)  # newer sentence-transformers
        return int(get_dim() if get_dim else model.get_sentence_embedding_dimension())

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self.model.encode(
            list(texts), normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )

    def embed_query(self, text: str) -> np.ndarray:
        return self.model.encode(
            [self.query_instruction + text], normalize_embeddings=True, show_progress_bar=False
        )[0]


def make_embedder(settings: Settings) -> SentenceTransformerEmbedder:
    r = settings.retrieval
    return SentenceTransformerEmbedder(r.embedding_model, r.query_instruction, r.max_seq_length)


# --------------------------------------------------------------------------- vector index


@dataclass
class Hit:
    chunk_id: str
    score: float  # cosine similarity for dense hits; BM25 score for keyword hits
    text: str
    metadata: dict[str, Any]


class VectorIndex(Protocol):
    def upsert(self, chunks: Sequence[Chunk], embeddings: np.ndarray) -> None: ...
    def update_metadata(self, chunks: Sequence[Chunk]) -> None: ...
    def delete_source(self, source_id: str) -> None: ...
    def ids_for_source(self, source_id: str) -> set[str]: ...
    def source_ids(self) -> set[str]: ...
    def count(self) -> int: ...
    def query(self, text: str, where: Where | None = None, k: int = 8) -> list[Hit]: ...
    def reset(self) -> None: ...


def _metadata(chunk: Chunk) -> dict[str, Any]:
    """Chroma rejects None values, so optional fields are simply omitted."""
    return {k: v for k in META_FIELDS if (v := getattr(chunk, k)) is not None}


def chroma_where(where: Where | None) -> dict | None:
    if not where:
        return None
    clauses = [
        {key: {"$in": list(value)}} if isinstance(value, (list, tuple, set)) else {key: value}
        for key, value in where.items()
    ]
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class ChromaVectorIndex:
    def __init__(self, path: Path, embedder: Embedder, collection: str = COLLECTION,
                 fresh: bool = False):
        """`fresh=True` opens the directory as it is on disk now. Chroma caches one client
        system per path, and a cached system keeps the HNSW vectors it loaded: after another
        process (`python -m ingest`) rewrites the index, the old client reports the new count
        but its queries still search only the old vectors (verified with chromadb 1.5). A fresh
        system reads everything again; clients already open keep working on their own copy, so
        requests in flight during a reload finish normally."""
        import chromadb

        self.embedder = embedder
        self.name = collection
        if fresh:
            from chromadb.api.shared_system_client import SharedSystemClient

            SharedSystemClient._identifier_to_system.pop(str(path), None)
            SharedSystemClient._identifier_to_refcount.pop(str(path), None)
        self.client = chromadb.PersistentClient(
            path=str(path), settings=chromadb.Settings(anonymized_telemetry=False)
        )
        self.collection = self._open()

    def _open(self):
        return self.client.get_or_create_collection(
            self.name, metadata={"hnsw:space": "cosine"}, embedding_function=None
        )

    @property
    def space(self) -> str:
        return (self.collection.metadata or {}).get("hnsw:space", "l2")

    def reset(self) -> None:
        if self.name in [c.name for c in self.client.list_collections()]:
            self.client.delete_collection(self.name)
        self.collection = self._open()

    def upsert(self, chunks: Sequence[Chunk], embeddings: np.ndarray) -> None:
        if not chunks:
            return
        if embeddings is None or len(embeddings) != len(chunks):
            raise ValueError("every chunk needs a precomputed embedding")
        self.collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=np.asarray(embeddings, dtype=np.float32).tolist(),
            documents=[c.text for c in chunks],
            metadatas=[_metadata(c) for c in chunks],
        )

    def update_metadata(self, chunks: Sequence[Chunk]) -> None:
        if chunks:
            self.collection.update(
                ids=[c.chunk_id for c in chunks], metadatas=[_metadata(c) for c in chunks]
            )

    def delete_source(self, source_id: str) -> None:
        self.collection.delete(where={"source_id": source_id})

    def ids_for_source(self, source_id: str) -> set[str]:
        return set(self.collection.get(where={"source_id": source_id}, include=[])["ids"])

    def source_ids(self) -> set[str]:
        metas = self.collection.get(include=["metadatas"])["metadatas"] or []
        return {m["source_id"] for m in metas}

    def count(self) -> int:
        return self.collection.count()

    def query(self, text: str, where: Where | None = None, k: int = 8) -> list[Hit]:
        return self.query_embedding(self.embedder.embed_query(text), where, k)

    def query_embedding(
        self, embedding: np.ndarray, where: Where | None = None, k: int = 8
    ) -> list[Hit]:
        if not self.count():
            return []
        res = self.collection.query(
            query_embeddings=[np.asarray(embedding, dtype=np.float32).tolist()],
            n_results=k,
            where=chroma_where(where),
            include=["documents", "metadatas", "distances"],
        )
        return [
            Hit(cid, 1.0 - float(dist), doc, meta)
            for cid, dist, doc, meta in zip(
                res["ids"][0],
                res["distances"][0],
                res["documents"][0],
                res["metadatas"][0],
                strict=True,
            )
        ]


# --------------------------------------------------------------------------- BM25

BM25_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")


def bm25_tokenize(text: str) -> list[str]:
    """Lowercase words and numbers, keeping decimals together ("0.70", "1.5"); drops ₹ and %."""
    return BM25_TOKEN.findall(text.lower())


def _matches(meta: dict[str, Any], where: Where | None) -> bool:
    for key, value in (where or {}).items():
        allowed = value if isinstance(value, (list, tuple, set)) else [value]
        if meta.get(key) not in allowed:
            return False
    return True


class BM25Index:
    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)
        self.ids = [c.chunk_id for c in self.chunks]
        self.metas = [_metadata(c) for c in self.chunks]
        corpus = [bm25_tokenize(c.text) for c in self.chunks]
        self.bm25 = BM25Okapi(corpus) if corpus else None

    def save(self, path: Path) -> None:
        payload = {"format": BM25_FORMAT, "chunks": [asdict(c) for c in self.chunks]}
        _atomic_write(path, pickle.dumps(payload))

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        """Load a BM25 index this application wrote (pickle: only load trusted local files)."""
        payload = pickle.loads(path.read_bytes())  # noqa: S301 - our own data/ file
        if payload.get("format") != BM25_FORMAT:
            raise ValueError(f"unsupported BM25 index format in {path}")
        return cls([Chunk(**c) for c in payload["chunks"]])

    def query(self, text: str, where: Where | None = None, k: int = 8) -> list[Hit]:
        if self.bm25 is None:
            return []
        scores = self.bm25.get_scores(bm25_tokenize(text))
        order = [i for i in np.argsort(-scores) if _matches(self.metas[i], where)][:k]
        return [
            Hit(self.ids[i], float(scores[i]), self.chunks[i].text, self.metas[i]) for i in order
        ]


# --------------------------------------------------------------------------- manifest


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def expected_settings(settings: Settings, embedder: Embedder) -> dict[str, Any]:
    """What the stored vectors depend on. The model name comes from the embedder that actually
    produces the vectors (built from `retrieval.embedding_model` in production)."""
    return {
        "embedding_model": embedder.model_name,
        "dimension": embedder.dimension,
        "query_instruction": settings.retrieval.query_instruction,
        "max_seq_length": settings.retrieval.max_seq_length,
        "chunker_version": settings.chunking.version,
        "distance": "cosine",
        "normalized": True,
    }


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class CorpusMismatch(Exception):
    """The indexes on disk don't match each other or this server's settings."""


def verify_corpus(settings: Settings, vector: VectorIndex, bm25: "BM25Index",
                  embedder: Embedder, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Check a built corpus before a running server switches to it; returns the manifest.

    The manifest is written last by `build_indexes`, so a matching manifest means indexing
    finished. Its settings must equal what this server would embed queries with, and Chroma,
    BM25 and the manifest must agree on the chunk count.
    """
    manifest = read_manifest(data_dir / MANIFEST_NAME)
    if not manifest.get("settings"):
        raise CorpusMismatch("no index manifest")
    expected = expected_settings(settings, embedder)
    changed = sorted(k for k, v in expected.items() if manifest["settings"].get(k) != v)
    if changed:
        raise CorpusMismatch(f"manifest settings differ: {', '.join(changed)}")
    counts = {"manifest": manifest.get("chunk_count"), "vector": vector.count(),
              "bm25": len(bm25.ids)}
    if len(set(counts.values())) != 1:
        raise CorpusMismatch(f"chunk counts differ: {counts}")
    return manifest


def content_hash(chunks: Iterable[Chunk]) -> str:
    """Hash of what gets embedded: ids and texts (metadata such as fetched_at excluded)."""
    h = hashlib.sha256()
    for c in sorted(chunks, key=lambda c: c.chunk_id):
        h.update(c.chunk_id.encode())
        h.update(b"\0")
        h.update(c.text.encode())
        h.update(b"\0")
    return h.hexdigest()


# --------------------------------------------------------------------------- build


@dataclass
class IndexSummary:
    chunks: int = 0
    sources_embedded: list[str] = field(default_factory=list)
    sources_unchanged: list[str] = field(default_factory=list)
    sources_removed: list[str] = field(default_factory=list)
    rebuilt: bool = False
    rebuild_reason: str | None = None
    dropped: dict[str, int] = field(default_factory=dict)  # reason -> sections


def build_indexes(
    chunks: Sequence[Chunk],
    settings: Settings,
    vector: VectorIndex,
    embedder: Embedder,
    data_dir: Path = DATA_DIR,
    rebuild: bool = False,
) -> IndexSummary:
    summary = IndexSummary(chunks=len(chunks))
    manifest_path = data_dir / MANIFEST_NAME
    manifest = read_manifest(manifest_path)
    expected = expected_settings(settings, embedder)

    changed = [k for k, v in expected.items() if manifest.get("settings", {}).get(k) != v]
    if rebuild or changed:
        summary.rebuilt = True
        if rebuild:
            summary.rebuild_reason = "requested"
        elif not manifest.get("settings"):
            summary.rebuild_reason = "new index (no manifest)"
        else:
            summary.rebuild_reason = f"changed: {', '.join(changed)}"
        log.info("rebuilding vector index (%s)", summary.rebuild_reason)
        vector.reset()
        manifest = {}
    hashes: dict[str, str] = dict(manifest.get("sources", {}))

    by_source: dict[str, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_source[c.source_id].append(c)

    for sid in sorted(vector.source_ids() - set(by_source)):
        vector.delete_source(sid)
        hashes.pop(sid, None)
        summary.sources_removed.append(sid)

    for sid, group in sorted(by_source.items()):
        digest = content_hash(group)
        ids = {c.chunk_id for c in group}
        if hashes.get(sid) == digest and vector.ids_for_source(sid) == ids:
            vector.update_metadata(group)  # e.g. a newer fetched_at for unchanged content
            summary.sources_unchanged.append(sid)
            continue
        vector.delete_source(sid)
        vector.upsert(group, embedder.embed_passages([c.text for c in group]))
        hashes[sid] = digest
        summary.sources_embedded.append(sid)

    BM25Index(chunks).save(data_dir / BM25_NAME)
    manifest = {
        "settings": expected,
        "sources": dict(sorted(hashes.items())),
        "chunk_count": len(chunks),
        "built_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(manifest_path, json.dumps(manifest, indent=2).encode("utf-8"))
    return summary


def latest_fetch_dates(settings: Settings) -> dict[str, str]:
    """`sources.fetched_at` per source: the last successful fetch, also when content was
    unchanged (the parsed JSON keeps the date its content last changed)."""
    from sqlalchemy import select

    from app.store.db import make_engine, session_factory
    from app.store.models import Source

    engine = make_engine(settings.corpus_database_url)
    try:
        with session_factory(engine)() as session:
            rows = session.execute(select(Source.id, Source.fetched_at)).all()
    except Exception as exc:  # no database yet: fall back to the parsed documents' dates
        log.warning("could not read source fetch dates (%s); using parsed dates", exc)
        return {}
    finally:
        engine.dispose()
    out = {}
    for sid, fetched_at in rows:
        if fetched_at is not None:
            if fetched_at.tzinfo is None:  # SQLite drops the timezone; values are stored in UTC
                fetched_at = fetched_at.replace(tzinfo=UTC)
            out[sid] = fetched_at.isoformat()
    return out


def run_index(
    settings: Settings,
    data_dir: Path = DATA_DIR,
    rebuild: bool = False,
    embedder: Embedder | None = None,
    vector: VectorIndex | None = None,
) -> IndexSummary:
    """Chunk every parsed document and bring both indexes up to date."""
    docs = load_parsed(data_dir / "parsed")
    dates = latest_fetch_dates(settings)
    for doc in docs:
        doc["fetched_at"] = dates.get(doc["source_id"], doc["fetched_at"])

    result: ChunkResult = chunk_corpus(
        docs, settings.chunking, token_counter(settings.retrieval.embedding_model)
    )
    embedder = embedder or make_embedder(settings)
    vector = vector or ChromaVectorIndex(data_dir / CHROMA_DIRNAME, embedder)
    summary = build_indexes(result.chunks, settings, vector, embedder, data_dir, rebuild)
    for d in result.dropped:
        reason = re.sub(r" \(.*\)$", "", d.reason)
        summary.dropped[reason] = summary.dropped.get(reason, 0) + 1
    return summary
