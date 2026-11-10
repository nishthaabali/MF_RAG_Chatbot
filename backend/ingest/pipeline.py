"""Fetch -> parse -> store, for every allowlisted source.

Outputs
- `sources` table: fetch state per URL (hash, snapshot path, last successful fetch date).
- `scheme_facts` table: core facts per scheme with provenance (source_url, fetched_at).
- `scheme_snapshots`: the complete embedded data object of each scheme page, verbatim.
- `scheme_metrics`, `scheme_holdings`, `scheme_peers`, `fund_managers`, `scheme_history`,
  `scheme_attributes`, `scheme_analysis`: everything else the page publishes, structured.
- data/parsed/<source_id>.json: title, description + sections of readable text (chunker input).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.config import DATA_DIR, Settings
from app.corpus import ensure_corpus_db
from app.store.db import init_db, make_engine, session_factory
from app.store.models import (
    CORPUS_TABLES,
    FundManager,
    SchemeAnalysis,
    SchemeAttribute,
    SchemeFact,
    SchemeHistory,
    SchemeHolding,
    SchemeMetric,
    SchemePeer,
    SchemeSnapshot,
    Source,
)
from ingest.corpus import load_sources
from ingest.fetcher import Fetcher, FetchResult, load_from_disk, utcnow
from ingest.parsers import GrowwParseError, parse_generic, parse_groww, parse_pdf
from ingest.parsers.groww import KEY_FIELDS, GrowwParse, missing_key_fields

log = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 80
_NAME_NOISE = re.compile(r"\b(direct|plan|growth|fund|option)\b", re.I)


@dataclass
class Summary:
    processed: int = 0
    fetched: int = 0
    from_disk: int = 0
    unchanged: int = 0
    facts_written: int = 0
    docs_written: int = 0
    failures: dict[str, str] = field(default_factory=dict)
    review: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        return self.processed - len(self.failures)


def _core_name(name: str) -> str:
    return " ".join(_NAME_NOISE.sub(" ", name.replace("&", "and")).split()).casefold()


def same_scheme(expected: str, shown: str) -> bool:
    """Guard against a URL silently opening a different scheme (FET-07)."""
    return fuzz.token_set_ratio(_core_name(expected), _core_name(shown)) >= NAME_MATCH_THRESHOLD


class ParseFailure(Exception):
    pass


def _parse(
    source: dict, result: FetchResult
) -> tuple[dict, dict | None, list[str], GrowwParse | None]:
    """Return (parsed document, facts or None, review notes, full Groww parse or None)."""
    body = result.path.read_bytes()
    if source["type"] == "scheme_page":
        try:
            parsed = parse_groww(body.decode("utf-8", errors="replace"))
        except GrowwParseError as exc:
            raise ParseFailure(str(exc)) from exc
        shown = parsed.facts["scheme_name"]
        if not same_scheme(source["name"], shown):
            raise ParseFailure(f"page shows {shown!r} but sources.yaml expects {source['name']!r}")
        notes = list(parsed.warnings)
        notes += [
            f"{k} not found on the page"
            for k in missing_key_fields(parsed.facts)
            if not any(w.startswith(k.replace("_", " ")) for w in parsed.warnings)
        ]
        doc = {"title": shown, "sections": parsed.sections}
        facts = {**parsed.facts, "extracted_from": parsed.extracted_from}
        return doc, facts, notes, parsed
    if "pdf" in (result.content_type or ""):
        doc = parse_pdf(body)
    else:
        doc = parse_generic(body.decode("utf-8", errors="replace"))
    if not doc["sections"]:
        raise ParseFailure("no readable text found")
    return doc, None, [], None


def _write_doc(parsed_dir: Path, source: dict, doc: dict, fetched_at: datetime) -> None:
    parsed_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_id": source["id"],
        "url": source["url"],
        "type": source["type"],
        "scheme_id": source.get("scheme_id"),
        "amc": source.get("amc"),
        "title": doc.get("title") or source.get("name"),
        "description": doc.get("description"),
        "fetched_at": fetched_at.isoformat(),
        "sections": doc["sections"],
    }
    path = parsed_dir / f"{source['id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _upsert_facts(
    session: Session, source: dict, facts: dict, notes: list[str], fetched_at: datetime
) -> tuple[bool, list[str]]:
    """Write facts unless a key field regressed from a value to NULL (PAR-18).

    On regression the previous row (and its older fetched_at) is kept, so answers keep using
    the last good value with its true date until the parser is fixed.
    """
    row = session.get(SchemeFact, source["scheme_id"])
    if row is not None:
        regressed = [k for k in KEY_FIELDS if getattr(row, k) is not None and facts.get(k) is None]
        if regressed:
            note = f"kept previous facts: {', '.join(regressed)} missing in the new snapshot"
            row.review_note = "; ".join([note, *notes])
            return False, [note, *notes]
    else:
        row = SchemeFact(scheme_id=source["scheme_id"])
        session.add(row)
    for key, value in facts.items():
        setattr(row, key, value)
    row.amc = source.get("amc")
    row.source_id = source["id"]
    row.source_url = source["url"]
    row.fetched_at = fetched_at
    row.review_note = "; ".join(notes) or None
    return True, notes


EXTENDED_TABLES = (
    SchemeMetric,
    SchemeHolding,
    SchemePeer,
    FundManager,
    SchemeHistory,
    SchemeAttribute,
    SchemeAnalysis,
)


def _replace_extended(
    session: Session, source: dict, parsed: GrowwParse, fetched_at: datetime
) -> None:
    """Replace every extended row for the scheme with the new snapshot's data.

    Only runs when the page's embedded data was present; if a page arrives without it (layout
    change, partial page), the previous rows stay so nothing is lost (as with PAR-18).
    """
    if not parsed.data or parsed.extended is None:
        return
    sid, src_id, url = source["scheme_id"], source["id"], source["url"]
    for model in EXTENDED_TABLES:
        session.query(model).filter(model.scheme_id == sid).delete()
    snapshot = session.get(SchemeSnapshot, sid) or SchemeSnapshot(scheme_id=sid)
    session.add(snapshot)
    snapshot.source_id, snapshot.source_url = src_id, url
    snapshot.fetched_at, snapshot.data = fetched_at, parsed.data

    x = parsed.extended
    common = {"scheme_id": sid, "source_id": src_id}
    session.add_all(
        SchemeMetric(**common, **m, source_url=url, fetched_at=fetched_at) for m in x.metrics
    )
    session.add_all(SchemeHolding(**common, **h, fetched_at=fetched_at) for h in x.holdings)
    session.add_all(SchemePeer(**common, **p, fetched_at=fetched_at) for p in x.peers)
    session.add_all(FundManager(**common, **m, fetched_at=fetched_at) for m in x.managers)
    session.add_all(SchemeHistory(**common, **h) for h in x.history)
    session.add_all(SchemeAttribute(**common, **a) for a in x.attributes)
    session.add_all(SchemeAnalysis(**common, **a) for a in x.analysis)


def _touch_facts(session: Session, source: dict, fetched_at: datetime) -> None:
    """Content unchanged but confirmed current: move the footer date forward."""
    if not source.get("scheme_id"):
        return
    row = session.get(SchemeFact, source["scheme_id"])
    if row is not None and row.source_id == source["id"]:
        row.fetched_at = fetched_at

    for model in (SchemeSnapshot, SchemeMetric, SchemeHolding, SchemePeer, FundManager):
        session.query(model).filter(
            model.scheme_id == source["scheme_id"], model.source_id == source["id"]
        ).update({"fetched_at": fetched_at})


async def run_ingest(
    settings: Settings,
    *,
    only: list[str] | None = None,
    from_disk: bool = False,
    force: bool = False,
    data_dir: Path = DATA_DIR,
    sources: list[dict] | None = None,
    fetcher: Fetcher | None = None,
) -> Summary:
    raw_dir, parsed_dir = data_dir / "raw", data_dir / "parsed"
    sources = sources if sources is not None else load_sources()
    if only:
        unknown = set(only) - {s["id"] for s in sources}
        if unknown:
            raise ValueError(f"unknown source id(s): {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s["id"] in only]

    ensure_corpus_db(settings)  # one-time copy out of an app.db from before Phase 11
    engine = make_engine(settings.corpus_database_url)
    init_db(engine, CORPUS_TABLES)
    Session_ = session_factory(engine)
    summary = Summary()
    own_fetcher = fetcher is None and not from_disk
    if own_fetcher:
        fetcher = Fetcher(settings, raw_dir)

    try:
        for source in sources:
            summary.processed += 1
            sid = source["id"]
            if from_disk:
                result = load_from_disk(source, raw_dir)
            else:
                result = await fetcher.fetch(source)

            with Session_.begin() as session:
                row = session.get(Source, sid) or Source(id=sid)
                session.add(row)
                row.url, row.type = source["url"], source["type"]
                row.scheme_id, row.amc = source.get("scheme_id"), source.get("amc")
                row.last_attempt_at = utcnow()
                row.last_error = result.error

                if result.status in ("failed", "skipped_robots"):
                    row.last_status = result.status
                    summary.failures[sid] = result.error or result.status
                    continue

                unchanged = (
                    row.sha256 == result.sha256
                    and (parsed_dir / f"{sid}.json").exists()
                    and not force
                )
                row.sha256 = result.sha256
                row.snapshot_path = str(result.path)
                row.content_type = result.content_type
                row.fetched_at = result.fetched_at
                if result.status == "from_disk":
                    summary.from_disk += 1
                else:
                    summary.fetched += 1

                if unchanged:
                    row.last_status = "unchanged"
                    summary.unchanged += 1
                    _touch_facts(session, source, result.fetched_at)
                    continue

                try:
                    doc, facts, notes, parsed = _parse(source, result)
                except ParseFailure as exc:
                    row.last_status = "failed"
                    row.last_error = f"parse failed: {exc}"
                    summary.failures[sid] = row.last_error
                    continue

                row.last_status = result.status
                _write_doc(parsed_dir, source, doc, result.fetched_at)
                summary.docs_written += 1
                if facts is not None:
                    written, notes = _upsert_facts(session, source, facts, notes, result.fetched_at)
                    summary.facts_written += int(written)
                    _replace_extended(session, source, parsed, result.fetched_at)
                    if notes:
                        summary.review[sid] = "; ".join(notes)
    finally:
        if own_fetcher:
            await fetcher.close()
        engine.dispose()
    return summary
