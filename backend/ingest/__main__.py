"""python -m ingest [--from-disk] [--only <source_id> ...] [--force] [--rebuild-index] [--no-index]

Fetch and parse every allowlisted source, then chunk all parsed documents and bring the
vector (Chroma) and keyword (BM25) indexes up to date.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from ingest.indexer import IndexSummary, run_index
from ingest.pipeline import Summary, run_ingest


def _print_ingest(summary: Summary) -> None:
    print("\nIngestion summary")
    print(f"  sources processed : {summary.processed}")
    print(f"  fetched           : {summary.fetched}")
    print(f"  loaded from disk  : {summary.from_disk}")
    print(f"  unchanged (skipped parse): {summary.unchanged}")
    print(f"  parsed documents  : {summary.docs_written}")
    print(f"  scheme facts rows : {summary.facts_written}")
    print(f"  failures          : {len(summary.failures)}")
    for sid, err in summary.failures.items():
        print(f"    - {sid}: {err}")
    if summary.review:
        print("  review notes:")
        for sid, note in summary.review.items():
            print(f"    - {sid}: {note}")


def _print_index(summary: IndexSummary) -> None:
    print("\nIndex summary")
    if summary.rebuilt:
        print(f"  vector index rebuilt ({summary.rebuild_reason})")
    print(f"  chunks indexed    : {summary.chunks}")
    print(f"  sources embedded  : {len(summary.sources_embedded)}")
    print(f"  sources unchanged : {len(summary.sources_unchanged)} (metadata refreshed)")
    if summary.sources_removed:
        print(f"  sources removed   : {', '.join(summary.sources_removed)}")
    print(f"  sections dropped  : {sum(summary.dropped.values())}")
    for reason, n in sorted(summary.dropped.items(), key=lambda kv: -kv[1]):
        print(f"    - {reason}: {n}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description=__doc__.split("\n")[2])
    parser.add_argument(
        "--from-disk",
        action="store_true",
        help="use saved snapshots in data/raw/ instead of fetching",
    )
    parser.add_argument(
        "--only", nargs="+", metavar="SOURCE_ID", help="fetch and parse only these source ids"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-parse even when the snapshot hash is unchanged"
    )
    parser.add_argument(
        "--rebuild-index", action="store_true", help="drop and rebuild the vector index"
    )
    parser.add_argument("--no-index", action="store_true", help="fetch and parse only")
    parser.add_argument("--report", type=Path, metavar="JSON",
                        help="also write the run summary as JSON (read by ingest.gate)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    summary = asyncio.run(
        run_ingest(settings, only=args.only, from_disk=args.from_disk, force=args.force)
    )
    _print_ingest(summary)
    report = {"started_at": started, "only": args.only, "from_disk": args.from_disk,
              "ingest": _ingest_dict(summary), "index": None, "ok": False}
    if summary.succeeded == 0:
        _write_report(args.report, report)
        return 1
    if args.no_index:
        report["ok"] = True
        _write_report(args.report, report)
        return 0

    # Indexing always covers every parsed document (also with --only): BM25 is global, and
    # sources whose chunks did not change are not re-embedded.
    try:
        index_summary = run_index(settings, rebuild=args.rebuild_index)
    except Exception:
        logging.getLogger("ingest").exception(
            "indexing failed; the next run re-checks every source against the index and repairs it"
        )
        _write_report(args.report, report)
        return 1
    _print_index(index_summary)
    report["index"] = asdict(index_summary)
    report["ok"] = True
    _write_report(args.report, report)
    return 0


def _ingest_dict(summary: Summary) -> dict:
    return {**asdict(summary), "succeeded": summary.succeeded}


def _write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
