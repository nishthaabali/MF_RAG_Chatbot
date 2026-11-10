"""python -m ingest.facts_report [scheme_id ...] [--all]

Print stored scheme data for manual review: core facts, plus (for the named schemes, or all with
--all) every metric, the top holdings, similar funds and managers.
"""

import argparse
import sys
from datetime import UTC, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.store.db import init_db, make_engine, session_factory
from app.store.models import (
    FundManager,
    SchemeAnalysis,
    SchemeAttribute,
    SchemeFact,
    SchemeHistory,
    SchemeHolding,
    SchemeMetric,
    SchemePeer,
)

IST = timezone(timedelta(hours=5, minutes=30))
COLUMNS = (
    "expense_ratio",
    "exit_load",
    "min_sip",
    "min_lumpsum",
    "riskometer",
    "benchmark",
    "lock_in",
    "launch_date",
    "fund_managers",
)
TOP_HOLDINGS = 10


def _detail(session, sid: str) -> None:
    count = lambda model: session.scalar(  # noqa: E731
        select(func.count()).select_from(model).where(model.scheme_id == sid)
    )
    print(
        f"  stored rows  : {count(SchemeMetric)} metrics, {count(SchemeHolding)} holdings, "
        f"{count(SchemePeer)} similar funds, {count(FundManager)} managers, "
        f"{count(SchemeHistory)} history points, {count(SchemeAttribute)} other fields, "
        f"{count(SchemeAnalysis)} Groww judgements (flagged, not used)"
    )
    print("  metrics (* = visible on the page; others are Groww page data only):")
    for m in session.scalars(
        select(SchemeMetric).where(SchemeMetric.scheme_id == sid).order_by(SchemeMetric.id)
    ):
        rank = f" of {m.rank_of}" if m.rank_of else ""
        mark = "*" if m.displayed else " "
        print(f"   {mark} {m.label:58} {m.value:>12g}{rank} {m.unit:6} as of {m.as_of}")
    print(f"  top {TOP_HOLDINGS} holdings:")
    for h in session.scalars(
        select(SchemeHolding).where(SchemeHolding.scheme_id == sid).order_by(SchemeHolding.position)
    ).all()[:TOP_HOLDINGS]:
        sector = h.sector or ""
        print(f"     {h.company_name:45} {sector:24} {h.percent_of_assets}%  ({h.portfolio_date})")
    print("  similar funds on the page:")
    for p in session.scalars(
        select(SchemePeer).where(SchemePeer.scheme_id == sid).order_by(SchemePeer.position)
    ):
        print(f"     {p.peer_name:55} 1Y {p.return_1y}  3Y {p.return_3y}  5Y {p.return_5y}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.facts_report")
    parser.add_argument("scheme_ids", nargs="*", help="show full detail for these schemes")
    parser.add_argument("--all", action="store_true", help="show full detail for every scheme")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    engine = make_engine(get_settings().corpus_database_url)
    init_db(engine)
    with session_factory(engine)() as session:
        rows = session.scalars(select(SchemeFact).order_by(SchemeFact.scheme_id)).all()
        if not rows:
            print("scheme_facts is empty; run `python -m ingest` first.")
            return 1
        for r in rows:
            if args.scheme_ids and r.scheme_id not in args.scheme_ids:
                continue
            fetched = r.fetched_at.replace(tzinfo=r.fetched_at.tzinfo or UTC).astimezone(IST)
            print(f"\n{r.scheme_id}  ({r.scheme_name})")
            print(f"  source: {r.source_url}")
            print(f"  fetched: {fetched:%d %b %Y %H:%M} IST  via {r.extracted_from}")
            for col in COLUMNS:
                print(f"  {col:13}: {getattr(r, col)}")
            if r.review_note:
                print(f"  REVIEW NOTE  : {r.review_note}")
            if args.all or args.scheme_ids:
                _detail(session, r.scheme_id)
    print(f"\n{len(rows)} schemes in scheme_facts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
