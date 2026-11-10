"""Inspect the chunk embeddings stored in the Phase 3 Chroma index (data/chroma/).

Usage (from the repo root):
    python eval/research/view_embeddings.py                      # summary + first 5 chunks
    python eval/research/view_embeddings.py --limit 20 --dims 16 # more chunks, more dims
    python eval/research/view_embeddings.py --source amfi-elss   # filter by source_id
    python eval/research/view_embeddings.py --id "amfi-exit-load::sec::0" --full
    python eval/research/view_embeddings.py --export data/embeddings.csv

Read-only: opens the persisted collection directly and never adds or queries, so Chroma
never loads its default embedding model.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import chromadb
import numpy as np
from chromadb.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CHROMA_DIR = ROOT / "data" / "chroma"
COLLECTION = "chunks"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=5, help="chunks to print (default 5, 0 = all)")
    ap.add_argument("--dims", type=int, default=8, help="embedding dimensions to print per chunk")
    ap.add_argument("--full", action="store_true", help="print the full embedding vector")
    ap.add_argument("--id", help="show a single chunk by id")
    ap.add_argument("--source", help="only chunks whose metadata source_id equals this")
    ap.add_argument("--export", type=Path, help="write all (filtered) chunks + embeddings to .csv or .json")
    args = ap.parse_args()

    if not CHROMA_DIR.exists():
        print(f"No Chroma index at {CHROMA_DIR}. Run `python -m ingest` first.", file=sys.stderr)
        return 1

    client = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(COLLECTION, embedding_function=None)

    get_kwargs: dict = {"include": ["embeddings", "metadatas", "documents"]}
    if args.id:
        get_kwargs["ids"] = [args.id]
    if args.source:
        get_kwargs["where"] = {"source_id": args.source}
    res = col.get(**get_kwargs)

    ids = res["ids"]
    embs = np.asarray(res["embeddings"]) if len(ids) else np.zeros((0, 0))
    metas, docs = res["metadatas"], res["documents"]

    print("=" * 80)
    print(f"Collection   : {col.name}  ({CHROMA_DIR})")
    print(f"Metadata     : {col.metadata}")
    print(f"Total chunks : {col.count()}   matched: {len(ids)}")
    if len(ids):
        norms = np.linalg.norm(embs, axis=1)
        print(f"Dimension    : {embs.shape[1]}")
        print(f"L2 norms     : min={norms.min():.4f} max={norms.max():.4f} mean={norms.mean():.4f}")
        print(f"Value range  : min={embs.min():.4f} max={embs.max():.4f} mean={embs.mean():.5f}")
    print("=" * 80)

    if args.export:
        export(args.export, ids, embs, metas, docs)
        print(f"Exported {len(ids)} chunks to {args.export}")

    n = len(ids) if args.limit == 0 or args.id else min(args.limit, len(ids))
    np.set_printoptions(precision=4, suppress=True, linewidth=100)
    for i in range(n):
        vec = embs[i]
        text = (docs[i] or "").replace("\n", " ")
        print(f"\n[{i + 1}] id: {ids[i]}")
        print(f"    metadata : {json.dumps(metas[i], ensure_ascii=False)}")
        print(f"    text     : {text[:200]}{'...' if len(text) > 200 else ''}")
        print(f"    norm     : {np.linalg.norm(vec):.4f}")
        if args.full:
            print(f"    embedding ({len(vec)} dims):\n{vec}")
        else:
            print(f"    embedding[:{args.dims}] of {len(vec)}: {vec[: args.dims]}")
    if n < len(ids):
        print(f"\n... {len(ids) - n} more (use --limit 0 for all, or --export)")
    return 0


def export(path: Path, ids, embs, metas, docs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        rows = [
            {"id": i, "metadata": m, "document": d, "embedding": e.tolist()}
            for i, e, m, d in zip(ids, embs, metas, docs)
        ]
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        dim = embs.shape[1] if len(ids) else 0
        w.writerow(["id", "metadata", "document", *[f"e{k}" for k in range(dim)]])
        for i, e, m, d in zip(ids, embs, metas, docs):
            w.writerow([i, json.dumps(m, ensure_ascii=False), d, *(f"{x:.6f}" for x in e)])


if __name__ == "__main__":
    raise SystemExit(main())
