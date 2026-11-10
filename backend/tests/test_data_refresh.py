"""Phase 11: the daily refresh workflow, the corpus/threads database split, and the server
pulling a published bundle (GitHub mocked with httpx.MockTransport)."""

from datetime import UTC, date, datetime

import httpx
import pytest
import yaml

from app.api.deps import Services
from app.config import ROOT_DIR, get_settings
from app.corpus import (
    active_corpus_dir,
    drop_legacy_tables,
    ensure_corpus_db,
    set_current,
    sqlite_conn,
)
from app.jobs import BundlePuller, CorpusReloader
from app.main import create_app
from app.pipeline.catalog import Catalog
from app.pipeline.orchestrator import AnswerPipeline
from app.pipeline.retriever import Retriever
from app.pipeline.router import Router
from app.store.db import make_engine
from app.store.models import CORPUS_TABLES, THREAD_TABLES
from app.store.threads import ThreadStore
from ingest.bundle import pack
from tests.corpus_fixture import corpus_settings, make_corpus

WORKFLOW = ROOT_DIR / ".github" / "workflows" / "daily-ingest.yml"
KOTAK_SOURCE = "groww-kotak-multi-cap"


# ------------------------------------------------------------------------------ workflow
@pytest.fixture(scope="module")
def workflow():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the key `on` as True
    return doc


def test_schedule_is_10am_ist_and_matches_settings(workflow):
    crons = [s["cron"] for s in workflow["on"]["schedule"]]
    assert crons == ["30 4 * * *"]  # 04:30 UTC = 10:00 IST
    assert crons[0] == get_settings().ingestion.schedule_cron


def test_workflow_shape(workflow):
    on = workflow["on"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"rebuild_index", "only"}
    assert workflow["concurrency"] == {"group": "daily-ingest", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["ingest"]["timeout-minutes"] <= 30
    assert "permissions" not in jobs["ingest"]  # read-only: it never writes to the repo
    assert jobs["publish"]["needs"] == "ingest"
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    run = "\n".join(s.get("run", "") for s in jobs["ingest"]["steps"])
    for needed in ("python -m ingest ", "python -m ingest.gate", "pytest -q -m \"not slow\"",
                   "eval/run.py --no-llm --ignore-drift", "python -m ingest.bundle pack",
                   "python -m ingest.bundle unpack"):
        assert needed in run, needed
    assert "GROQ_API_KEY" not in WORKFLOW.read_text(encoding="utf-8")


def test_dispatch_inputs_reach_the_shell_only_through_env(workflow):
    # `${{ inputs.only }}` pasted into a script would let a dispatch input inject commands.
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{ inputs." not in step.get("run", ""), step.get("name")


# ------------------------------------------------------------------------------ database split
def test_threads_and_corpus_tables_are_separate(tmp_path):
    store = ThreadStore(make_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}"))
    store.create()
    with sqlite_conn(tmp_path / "app.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {t.name for t in THREAD_TABLES}
    assert not {t.name for t in CORPUS_TABLES} & {t.name for t in THREAD_TABLES}


def test_migration_copies_corpus_tables_once(tmp_path):
    legacy_settings = corpus_settings(tmp_path, tmp_path / "old")
    make_corpus(tmp_path / "old", legacy_settings)  # an old corpus.db standing in for app.db
    (tmp_path / "old" / "corpus.db").rename(tmp_path / "threads.db")
    ThreadStore(make_engine(legacy_settings.database_url)).create()

    settings = corpus_settings(tmp_path, tmp_path / "data")
    assert ensure_corpus_db(settings) is True
    with sqlite_conn(tmp_path / "data" / "corpus.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheme_facts").fetchone()[0] == 2
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "threads" not in names and "messages" not in names
    assert ensure_corpus_db(settings) is False  # already done

    dropped = drop_legacy_tables(settings)
    assert "scheme_facts" in dropped and "sources" in dropped
    with sqlite_conn(tmp_path / "threads.db") as conn:
        left = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert left == {"threads", "messages"}
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1


def test_no_migration_without_a_legacy_corpus(tmp_path):
    settings = corpus_settings(tmp_path, tmp_path / "data")
    ThreadStore(make_engine(settings.database_url))
    assert ensure_corpus_db(settings) is False
    assert not (tmp_path / "data" / "corpus.db").exists()


# ------------------------------------------------------------------------------ server pull
REPO = "owner/mf-faq"


def github(bundle_json: bytes, archive: bytes, calls: list):
    assets = [{"name": "bundle.json", "url": "https://api.github.com/assets/1"},
              {"name": "data-bundle.tar.gz", "url": "https://api.github.com/assets/2"},
              {"name": "data-bundle.tar.gz.sha256", "url": "https://api.github.com/assets/3"}]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == f"/repos/{REPO}/releases/tags/data-latest":
            return httpx.Response(200, json={"tag_name": "data-latest", "assets": assets})
        if request.url.path == "/assets/1":
            return httpx.Response(200, content=bundle_json)
        if request.url.path == "/assets/2":
            # GitHub redirects asset downloads to its storage host.
            return httpx.Response(302, headers={"location": "https://objects.example/b.tgz"})
        if request.url.host == "objects.example":
            assert "authorization" not in request.headers  # token not sent to storage
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def server(tmp_path):
    """A server running on a locally built corpus (fetched 09 Nov), plus a published bundle
    built elsewhere a day later (fetched 10 Nov, one more chunk)."""
    from tests.test_indexer import CORPUS, chunk

    data = tmp_path / "server-data"
    settings = corpus_settings(tmp_path, data).model_copy(update={
        "data_refresh": get_settings().data_refresh.model_copy(update={"release_repo": REPO})})
    embedder = make_corpus(data, settings)
    retriever = Retriever.from_settings(settings, embedder=embedder, data_dir=data, verify=True)
    pipeline = AnswerPipeline(retriever.catalog, Router(retriever.catalog), retriever, None)
    store = ThreadStore(make_engine(settings.database_url))
    services = Services(settings=settings, pipeline=pipeline, store=store,
                        catalog=pipeline.catalog, today=lambda: date(2026, 11, 10))

    ci = tmp_path / "ci-data"
    ci_settings = corpus_settings(tmp_path / "ci", ci)
    extra = chunk("amfi-sip", 0, "A systematic investment plan invests a fixed amount monthly")
    make_corpus(ci, ci_settings, fetched=datetime(2026, 11, 10, 4, 40), chunks=[*CORPUS, extra],
                embedder=embedder)
    archive, meta = pack(tmp_path / "dist", settings=ci_settings, data_dir=ci,
                         now=datetime(2026, 11, 10, 4, 50, tzinfo=UTC))
    published = (tmp_path / "dist" / "bundle.json").read_bytes()
    return services, data, archive.read_bytes(), published, meta


async def test_puller_installs_a_newer_bundle_and_the_server_switches(server):
    services, data, archive, published, meta = server
    calls = []
    reloader = CorpusReloader(services, data)
    puller = BundlePuller(services, reloader, data, transport=github(published, archive, calls))
    assert services.catalog.fetched_at[KOTAK_SOURCE] == date(2026, 11, 9)

    assert await puller.check() == "installed"
    active = active_corpus_dir(data)
    assert active.parent == data / "bundles" and active.name.startswith(meta["id"])
    assert services.catalog.fetched_at[KOTAK_SOURCE] == date(2026, 11, 10)  # new footers
    hits = services.pipeline.retriever.vector.query("systematic investment plan", k=10)
    assert "amfi-sip::sec::0" in [h.chunk_id for h in hits]
    assert services.pipeline.retriever.data_dir == active
    assert (active / "corpus.db").is_file() and not (active / "app.db").exists()
    assert not list((data / "bundles").glob(".staging-*"))

    # Same bundle again: only bundle.json is fetched, nothing is downloaded or reloaded.
    calls.clear()
    assert await puller.check() == "up_to_date"
    assert "/assets/2" not in calls
    assert await reloader.check() is False

    app = create_app(services=services, frontend_dir=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        health = (await c.get("/api/health")).json()
    assert health["corpus"] == f"bundle {meta['id']}" and health["bundle_stale"] is False


async def test_a_corrupt_download_changes_nothing(server):
    services, data, archive, published, _ = server
    before = services.pipeline
    puller = BundlePuller(services, CorpusReloader(services, data), data,
                          transport=github(published, archive[:-100] + b"x" * 100, []))
    assert await puller.check() == "failed"
    assert services.pipeline is before and active_corpus_dir(data) == data
    assert [p.name for p in (data / "bundles").iterdir() if not p.name.startswith(".")] == []


async def test_a_bundle_that_fails_its_checks_is_not_activated(server):
    services, data, archive, published, _ = server
    before = services.pipeline
    # A server configured with another chunker version must not switch to this bundle.
    services.settings = services.settings.model_copy(update={
        "chunking": services.settings.chunking.model_copy(update={"version": "99"})})
    puller = BundlePuller(services, CorpusReloader(services, data), data,
                          transport=github(published, archive, []))
    assert await puller.check() == "failed"
    assert services.pipeline is before and active_corpus_dir(data) == data
    assert not (data / "bundles" / "CURRENT").exists()


async def test_pulling_is_off_without_a_repo(server):
    services, data, *_ = server
    services.settings = services.settings.model_copy(update={
        "data_refresh": services.settings.data_refresh.model_copy(update={"release_repo": ""})})
    calls = []
    puller = BundlePuller(services, CorpusReloader(services, data), data,
                          transport=github(b"{}", b"", calls))
    assert await puller.check() == "disabled" and calls == []


async def test_health_flags_a_missing_or_old_bundle_only_when_pulling(server):
    services, data, *_ = server
    app = create_app(services=services, frontend_dir=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        health = (await c.get("/api/health")).json()
    assert health["corpus"] == "local" and health["bundle_stale"] is True
    assert health["status"] == "degraded" and health["corpus_built_at"].endswith("Z")

    services.settings = services.settings.model_copy(update={
        "data_refresh": services.settings.data_refresh.model_copy(update={"release_repo": ""})})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        assert (await c.get("/api/health")).json()["bundle_stale"] is False


def test_an_installed_bundle_is_used_after_a_restart(server, tmp_path):
    """CURRENT survives a restart: a fresh pipeline loads the bundle, not data/."""
    _, data, archive, _, meta = server
    from ingest.bundle import install

    path = tmp_path / "b.tar.gz"
    path.write_bytes(archive)
    target, _ = install(path, data, meta["archive_sha256"])
    set_current(data, target.name)
    assert active_corpus_dir(data) == target
    with sqlite_conn(target / "corpus.db") as conn:
        fetched = conn.execute("SELECT fetched_at FROM sources").fetchone()[0]
    assert fetched.startswith("2026-11-10")


def test_catalog_fixture_matches_the_real_source_id():
    assert KOTAK_SOURCE in Catalog.load().source_urls
