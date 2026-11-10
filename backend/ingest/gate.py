"""Quality gate for the daily refresh: is today's corpus good enough to publish?

    python -m ingest.gate --report ingest_report.json [--previous prev/bundle.json] \
        [--out gate_report.json] [--summary summary.md]

Compares the corpus just built in data/ with the previous published bundle. Exit 0 = publish,
1 = keep yesterday's bundle. The rules (docs/implementation-plan.md, Phase 11):

- the ingest run finished (`ok`) and `check_sources` passes;
- at most 20% of sources failed to fetch (failed sources keep their old snapshot and date);
- every scheme's core facts are present, unless they were already missing yesterday: a field
  that becomes empty usually means Groww changed its page layout;
- the chunk count is within 20% of yesterday's; no chunk exceeds `chunking.max_tokens`;
- the index manifest matches config and all indexes agree (`verify_corpus`);
- smoke retrieval: "exit load Kotak Multi Cap" finds Kotak's `Costs and exit load` first.

The unit tests and the `--no-llm` eval run as separate workflow steps.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from app.config import DATA_DIR, Settings, get_settings
from ingest.bundle import describe

MAX_FETCH_FAILURE_RATE = 0.20
MAX_CHUNK_CHANGE = 0.20
SMOKE_QUERY = "exit load Kotak Multi Cap"
SMOKE_SCHEME = "kotak-multi-cap"
SMOKE_SECTION = "Costs and exit load"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False  # reported, but doesn't fail the gate


def evaluate(report: dict, current: dict, previous: dict | None) -> list[Check]:
    """The rules that need no model or index: run report and corpus description only."""
    checks: list[Check] = []
    ing = report.get("ingest") or {}
    checks.append(Check("ingest_finished", bool(report.get("ok")),
                        "ingest and index completed" if report.get("ok")
                        else "python -m ingest did not finish"))

    processed = ing.get("processed") or 0
    failed = sorted(ing.get("failures") or {})
    rate = len(failed) / processed if processed else 1.0
    checks.append(Check(
        "fetch_failures", processed > 0 and rate <= MAX_FETCH_FAILURE_RATE,
        f"{len(failed)}/{processed} sources failed ({rate:.0%}, limit "
        f"{MAX_FETCH_FAILURE_RATE:.0%})" + (f": {', '.join(failed)}" if failed else "")))
    if failed and rate <= MAX_FETCH_FAILURE_RATE:
        checks.append(Check("fetch_failures_kept_old_data", True,
                            f"kept previous snapshot and date for {', '.join(failed)}",
                            warning=True))

    before = (previous or {}).get("core_nulls") or {}
    newly = {s: sorted(set(fields) - set(before.get(s, ())))
             for s, fields in (current.get("core_nulls") or {}).items()}
    newly = {s: f for s, f in newly.items() if f}
    if previous is None:
        missing = {s: f for s, f in (current.get("core_nulls") or {}).items() if f}
        checks.append(Check("core_facts", True,
                            "first run, nothing to compare with" + (
                                f"; missing: {_fmt(missing)}" if missing else ""),
                            warning=bool(missing)))
    else:
        checks.append(Check("core_facts", not newly,
                            f"newly missing: {_fmt(newly)}" if newly
                            else "no core fact went missing"))
        gone = sorted(set(before) - set(current.get("core_nulls") or {}))
        checks.append(Check("schemes_present", not gone,
                            f"schemes gone: {', '.join(gone)}" if gone
                            else f"{current.get('schemes')} schemes"))

    now, then = current.get("chunk_count") or 0, (previous or {}).get("chunk_count")
    if then:
        change = (now - then) / then
        checks.append(Check("chunk_count", abs(change) <= MAX_CHUNK_CHANGE,
                            f"{then} -> {now} ({change:+.0%}, limit "
                            f"±{MAX_CHUNK_CHANGE:.0%})"))
    else:
        checks.append(Check("chunk_count", now > 0, f"{now} chunks (no previous count)"))
    return checks


def _fmt(missing: dict[str, list[str]]) -> str:
    return "; ".join(f"{s}: {', '.join(f)}" for s, f in sorted(missing.items()))


def index_checks(settings: Settings, data_dir: Path) -> list[Check]:
    """The rules that load the embedding model and the indexes."""
    from ingest.check_sources import run_checks
    from ingest.chunker import token_counter
    from ingest.indexer import (
        BM25_NAME,
        CHROMA_DIRNAME,
        BM25Index,
        ChromaVectorIndex,
        CorpusMismatch,
        make_embedder,
        verify_corpus,
    )

    errors = run_checks()
    checks = [Check("check_sources", not errors,
                    "; ".join(map(str, errors[:5])) if errors else "config files valid")]
    embedder = make_embedder(settings)
    vector = ChromaVectorIndex(data_dir / CHROMA_DIRNAME, embedder, fresh=True)
    bm25 = BM25Index.load(data_dir / BM25_NAME)
    try:
        manifest = verify_corpus(settings, vector, bm25, embedder, data_dir)
        checks.append(Check("index_consistent", True,
                            f"{manifest['chunk_count']} chunks in Chroma, BM25 and manifest"))
    except CorpusMismatch as exc:
        checks.append(Check("index_consistent", False, str(exc)))

    count = token_counter(settings.retrieval.embedding_model)
    longest = max((count(c.text) for c in bm25.chunks), default=0)
    limit = settings.chunking.max_tokens
    checks.append(Check("chunk_tokens", longest <= limit,
                        f"longest chunk {longest} tokens (limit {limit})"))

    hits = vector.query(SMOKE_QUERY, where={"scheme_id": SMOKE_SCHEME}, k=1)
    top = hits[0].metadata.get("section", "") if hits else None
    checks.append(Check("smoke_retrieval", bool(top and top.startswith(SMOKE_SECTION)),
                        f"{SMOKE_QUERY!r} -> {top!r}"))
    return checks


def markdown(checks: list[Check], passed: bool, current: dict, previous: dict | None) -> str:
    lines = [f"## Daily data refresh: {'PASSED' if passed else 'FAILED'}", ""]
    if not passed:
        lines += ["The previous bundle stays published and live.", ""]
    lines += ["| Check | Result | Detail |", "|---|---|---|"]
    for c in checks:
        mark = "warn" if c.warning else ("ok" if c.ok else "**FAIL**")
        lines.append(f"| {c.name} | {mark} | {c.detail.replace('|', '/')} |")
    changed = [s for s, v in (current.get("sources") or {}).items()
               if previous and (previous.get("sources") or {}).get(s, {}).get("sha256")
               != v.get("sha256")]
    lines += ["", f"Sources with changed content since the previous bundle: {len(changed)}"
              + (f" ({', '.join(changed)})" if changed else "")]
    return "\n".join(lines) + "\n"


def run(report_path: Path, previous_path: Path | None, *, settings: Settings | None = None,
        data_dir: Path = DATA_DIR, with_index: bool = True) -> tuple[bool, dict, str]:
    settings = settings or get_settings()
    from app.corpus import sqlite_path

    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() \
        else {}
    previous = None
    if previous_path and previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
    current = describe(data_dir, sqlite_path(settings.corpus_database_url))
    checks = evaluate(report, current, previous)
    if with_index:
        checks += index_checks(settings, data_dir)
    passed = all(c.ok for c in checks)
    out = {"passed": passed, "previous_bundle": (previous or {}).get("id"),
           "checks": [asdict(c) for c in checks]}
    return passed, out, markdown(checks, passed, current, previous)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.gate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", type=Path, required=True, help="ingest_report.json")
    ap.add_argument("--previous", type=Path, help="bundle.json of the previous bundle")
    ap.add_argument("--out", type=Path, help="write the verdict as JSON")
    ap.add_argument("--summary", type=Path, help="write a Markdown summary (job page)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    passed, out, md = run(args.report, args.previous)
    if args.out:
        args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
