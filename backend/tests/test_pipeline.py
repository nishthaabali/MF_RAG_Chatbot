import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.config import load_settings
from app.store.db import make_engine, session_factory
from app.store.models import SchemeFact, Source
from ingest.fetcher import FetchResult
from ingest.pipeline import run_ingest, same_scheme

FIXTURES = Path(__file__).parent / "fixtures"
GROWW_HTML = (FIXTURES / "groww_kotak_multi_cap.html").read_bytes()
AMFI_HTML = (FIXTURES / "amfi_exit_load.html").read_bytes()

KOTAK = {
    "id": "groww-kotak-multi-cap",
    "url": "https://groww.in/mutual-funds/kotak-multi-cap",
    "type": "scheme_page",
    "render": "static",
    "scheme_id": "kotak-multi-cap",
    "amc": "kotak",
    "name": "Kotak Multi Cap Fund",
    "category": "Equity - Multi Cap",
}
AMFI = {
    "id": "amfi-exit-load",
    "url": "https://www.mutualfundssahihai.com/en/x",
    "type": "amfi",
    "render": "static",
    "name": "Exit load",
}

DAY1 = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
DAY2 = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)


class FakeFetcher:
    """Returns scripted bodies per source id, writing snapshots like the real fetcher."""

    def __init__(self, raw_dir: Path, bodies: dict, when: datetime):
        self.raw_dir, self.bodies, self.when = raw_dir, bodies, when

    async def fetch(self, source):
        body = self.bodies.get(source["id"])
        if body is None:
            return FetchResult(source["id"], "failed", error="HTTP 503")
        folder = self.raw_dir / source["id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.when:%Y-%m-%d}.html"
        path.write_bytes(body)
        return FetchResult(
            source["id"], "ok", path, hashlib.sha256(body).hexdigest(), "text/html", self.when
        )

    async def close(self):
        pass


def settings_for(tmp_path):
    # Both databases temporary: with a real app.db, the corpus migration would copy from it.
    return load_settings().model_copy(update={
        "corpus_database_url": f"sqlite:///{(tmp_path / 'corpus.db').as_posix()}",
        "database_url": f"sqlite:///{(tmp_path / 'app.db').as_posix()}"})


async def ingest(tmp_path, bodies, when, sources=(KOTAK, AMFI), **kw):
    fetcher = FakeFetcher(tmp_path / "raw", bodies, when)
    return await run_ingest(
        settings_for(tmp_path), data_dir=tmp_path, sources=list(sources), fetcher=fetcher, **kw
    )


def facts(tmp_path) -> SchemeFact:
    engine = make_engine(settings_for(tmp_path).corpus_database_url)
    with session_factory(engine)() as s:
        row = s.get(SchemeFact, "kotak-multi-cap")
    engine.dispose()
    return row


def source_row(tmp_path, sid) -> Source:
    engine = make_engine(settings_for(tmp_path).corpus_database_url)
    with session_factory(engine)() as s:
        row = s.get(Source, sid)
    engine.dispose()
    return row


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def test_first_run_writes_facts_with_provenance_and_parsed_docs(tmp_path):
    summary = await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML, AMFI["id"]: AMFI_HTML}, DAY1)
    assert summary.failures == {}
    assert summary.facts_written == 1 and summary.docs_written == 2
    row = facts(tmp_path)
    assert row.expense_ratio == "0.70%"
    assert row.min_sip == "₹100"
    assert row.riskometer == "Very High"
    assert row.source_url == KOTAK["url"]
    assert _aware(row.fetched_at) == DAY1
    assert row.amc == "kotak"
    doc = json.loads((tmp_path / "parsed" / "amfi-exit-load.json").read_text(encoding="utf-8"))
    assert doc["type"] == "amfi" and doc["sections"]


async def test_unchanged_content_skips_parse_but_moves_footer_date(tmp_path):
    bodies = {KOTAK["id"]: GROWW_HTML, AMFI["id"]: AMFI_HTML}
    await ingest(tmp_path, bodies, DAY1)
    summary = await ingest(tmp_path, bodies, DAY2)
    assert summary.unchanged == 2 and summary.docs_written == 0
    assert _aware(facts(tmp_path).fetched_at) == DAY2


async def test_force_reparses_unchanged_content(tmp_path):
    bodies = {KOTAK["id"]: GROWW_HTML, AMFI["id"]: AMFI_HTML}
    await ingest(tmp_path, bodies, DAY1)
    summary = await ingest(tmp_path, bodies, DAY2, force=True)
    assert summary.unchanged == 0 and summary.docs_written == 2


async def test_failed_fetch_keeps_previous_snapshot_and_date(tmp_path):
    await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML, AMFI["id"]: AMFI_HTML}, DAY1)
    summary = await ingest(tmp_path, {AMFI["id"]: AMFI_HTML}, DAY2)
    assert summary.failures == {KOTAK["id"]: "HTTP 503"}
    src = source_row(tmp_path, KOTAK["id"])
    assert src.last_status == "failed"
    assert _aware(src.fetched_at) == DAY1
    assert _aware(facts(tmp_path).fetched_at) == DAY1
    assert facts(tmp_path).expense_ratio == "0.70%"


async def test_key_field_regression_keeps_previous_facts(tmp_path):
    await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML}, DAY1, sources=[KOTAK])
    json_value = b'"expense_ratio": "0.6958"'
    assert json_value in GROWW_HTML
    broken = GROWW_HTML.replace(json_value, b'"expense_ratio": null').replace(
        b">Expense ratio<", b">Removed<"
    )
    summary = await ingest(tmp_path, {KOTAK["id"]: broken}, DAY2, sources=[KOTAK])
    assert summary.facts_written == 0
    row = facts(tmp_path)
    assert row.expense_ratio == "0.70%"
    assert _aware(row.fetched_at) == DAY1
    assert "kept previous facts: expense_ratio" in row.review_note


async def test_page_showing_a_different_scheme_is_rejected(tmp_path):
    wrong = {**KOTAK, "name": "HDFC Corporate Bond Fund"}
    summary = await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML}, DAY1, sources=[wrong])
    assert "page shows" in summary.failures[KOTAK["id"]]
    assert facts(tmp_path) is None


async def test_database_uses_wal_mode(tmp_path):
    await ingest(tmp_path, {AMFI["id"]: AMFI_HTML}, DAY1, sources=[AMFI])
    engine = make_engine(settings_for(tmp_path).corpus_database_url)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    engine.dispose()


def test_same_scheme_tolerates_plan_wording_but_not_other_schemes():
    assert same_scheme("HDFC Flexi Cap Fund", "HDFC Flexi Cap Direct Plan Growth")
    assert same_scheme("Edelweiss Mid Cap Fund", "Edelweiss Mid Cap Direct Plan Growth")
    assert same_scheme("HDFC Pharma And Healthcare Fund", "HDFC Pharma and Healthcare Fund")
    assert not same_scheme("ABSL Short Term Fund", "Aditya Birla Sun Life Corporate Bond Fund")
    assert not same_scheme("Kotak Multi Cap Fund", "Kotak Flexicap Fund")


async def test_all_page_data_is_stored_in_its_tables(tmp_path):
    from app.store.models import (
        FundManager,
        SchemeAnalysis,
        SchemeAttribute,
        SchemeHistory,
        SchemeHolding,
        SchemeMetric,
        SchemePeer,
        SchemeSnapshot,
    )

    await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML}, DAY1, sources=[KOTAK])
    engine = make_engine(settings_for(tmp_path).corpus_database_url)
    with session_factory(engine)() as s:
        count = lambda model: s.query(model).filter(model.scheme_id == "kotak-multi-cap").count()  # noqa: E731
        assert s.get(SchemeSnapshot, "kotak-multi-cap").data["isin"] == "INF174KA1HV3"
        assert count(SchemeMetric) > 40
        assert count(SchemeHolding) == 77
        assert count(SchemePeer) == 5
        assert count(FundManager) == 2
        assert count(SchemeHistory) > 0
        assert count(SchemeAttribute) > 50
        assert count(SchemeAnalysis) > 0
        nav = s.query(SchemeMetric).filter_by(scheme_id="kotak-multi-cap", metric="nav").one()
        assert nav.source_url == KOTAK["url"] and _aware(nav.fetched_at) == DAY1
    engine.dispose()

    # A second parse replaces rows instead of duplicating them.
    await ingest(tmp_path, {KOTAK["id"]: GROWW_HTML}, DAY2, sources=[KOTAK], force=True)
    engine = make_engine(settings_for(tmp_path).corpus_database_url)
    with session_factory(engine)() as s:
        assert s.query(SchemeHolding).filter_by(scheme_id="kotak-multi-cap").count() == 77
    engine.dispose()
