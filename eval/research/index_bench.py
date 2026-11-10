"""Retrieval regression check against the BUILT indexes (data/chroma, data/bm25.pkl).

Runs the labelled queries in retrieval_queries.yaml with the pipeline's scoping (scheme queries
-> that scheme's chunks; general queries -> amfi/sebi/rta_help) and reports dense, BM25 and
hybrid (RRF over top 8 + top 8) hit@1 / hit@5 / MRR over the text-answerable queries. Queries
marked `answered_from: sql` (holdings, similar funds) are excluded from those metrics and
reported separately, with a check that their content really is absent from the text index.
The raw figure over all queries is reported too. Phase 3 exit target: hybrid hit@5 >= 0.95 and
hit@1 >= 0.90 on the text-answerable queries.

    cd backend && python ../eval/research/index_bench.py [--out results.json]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from app.config import DATA_DIR, get_settings
from ingest.indexer import BM25_NAME, CHROMA_DIRNAME, BM25Index, ChromaVectorIndex, make_embedder

HERE = Path(__file__).parent
GENERAL = ["amfi", "sebi", "rta_help"]
RRF_K = 60


def scope(gold_source: str, scheme_by_source: dict[str, str]) -> dict:
    if gold_source.startswith("groww-"):
        return {"scheme_id": scheme_by_source[gold_source]}
    return {"doc_type": GENERAL}


def rank(ids: list[str], metas: dict[str, dict], q: dict) -> int | None:
    for r, cid in enumerate(ids, start=1):
        m = metas[cid]
        if re.fullmatch(q["gold_source"], m["source_id"]) and re.search(
            q["gold_section"], m.get("section", ""), re.I
        ):
            return r
    return None


def metrics(ranks: list[int | None]) -> dict:
    n = len(ranks)
    return {
        "hit@1": round(sum(r == 1 for r in ranks) / n, 3),
        "hit@5": round(sum(r is not None and r <= 5 for r in ranks) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    settings = get_settings()
    r = settings.retrieval
    queries = yaml.safe_load((HERE / "retrieval_queries.yaml").read_text(encoding="utf-8"))
    queries = queries["queries"]

    vector = ChromaVectorIndex(DATA_DIR / CHROMA_DIRNAME, make_embedder(settings))
    bm25 = BM25Index.load(DATA_DIR / BM25_NAME)
    metas = dict(zip(bm25.ids, bm25.metas, strict=True))
    scheme_by_source = {m["source_id"]: m["scheme_id"] for m in bm25.metas if "scheme_id" in m}

    out = {"dense": [], "bm25": [], "hybrid": []}
    misses, sql_queries, all_hybrid = [], [], []
    for q in queries:
        where = scope(q["gold_source"].split("|")[0], scheme_by_source)
        dense = [h.chunk_id for h in vector.query(q["q"], where, r.vector_top_k)]
        keyword = [h.chunk_id for h in bm25.query(q["q"], where, r.bm25_top_k)]
        fused: dict[str, float] = {}
        for ids in (dense, keyword):
            for pos, cid in enumerate(ids):
                fused[cid] = fused.get(cid, 0) + 1 / (RRF_K + pos + 1)
        hybrid = sorted(fused, key=lambda c: -fused[c])[: r.final_top_k]
        all_hybrid.append(rank(hybrid, metas, q))
        if q.get("answered_from") == "sql":
            in_index = any(rank([cid], metas, q) for cid in metas)
            sql_queries.append({"q": q["q"], "gold_content_in_text_index": in_index})
            continue
        for name, ids in (("dense", dense), ("bm25", keyword), ("hybrid", hybrid)):
            out[name].append(rank(ids, metas, q))
        if out["hybrid"][-1] != 1:
            misses.append({"q": q["q"], "hybrid_rank": out["hybrid"][-1], "got": hybrid[:3]})

    report = {
        "chunks": len(bm25.ids),
        "queries": len(queries),
        "text_answerable": len(out["hybrid"]),
        **{k: metrics(v) for k, v in out.items()},
        "hybrid_all_queries_raw": metrics(all_hybrid),
        "hybrid_not_first": misses,
        "answered_from_sql": sql_queries,
    }
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if args.out:
        args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    ok = report["hybrid"]["hit@5"] >= 0.95 and report["hybrid"]["hit@1"] >= 0.90
    ok = ok and not any(q["gold_content_in_text_index"] for q in sql_queries)
    print("Phase 3 target (hybrid hit@5 >= 0.95, hit@1 >= 0.90):", "MET" if ok else "NOT MET")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
