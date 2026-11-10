"""Deployment (docs/deployment-plan.md §4): configurable API base URL in the UI, the production
settings file, the real client IP behind Railway's edge, CORS for the Vercel frontend, booting
with an empty volume (degraded mode + app.bootstrap), and the container/platform config files."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import SecretStr

from app.api.deps import Services
from app.bootstrap import bootstrap
from app.config import ROOT_DIR, SETTINGS_FILE_ENV, get_settings, load_settings, settings_file
from app.corpus import active_corpus_dir, set_current
from app.jobs import BundlePuller, CorpusReloader
from app.main import create_app, load_pipeline
from app.middleware.rate_limit import client_key
from app.pipeline.orchestrator import AnswerPipeline, ThreadContext
from app.store.db import make_engine
from app.store.threads import ThreadStore
from ingest.bundle import pack
from tests.corpus_fixture import corpus_settings, make_corpus
from tests.test_data_refresh import KOTAK_SOURCE, REPO, github
from tests.test_ops import build, client, settings_with

FRONTEND = ROOT_DIR / "frontend"
PROD_SETTINGS = ROOT_DIR / "config" / "settings.production.yaml"
DEV_SETTINGS = ROOT_DIR / "config" / "settings.yaml"


# ------------------------------------------------------------------------------ §4.1 frontend
def test_ui_reads_the_api_base_from_config_js():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts.index("config.js") < scripts.index("app.js")
    config = (FRONTEND / "config.js").read_text(encoding="utf-8")
    assert 'window.MF_API_BASE = "/api";' in config  # same origin by default (local uvicorn)
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "window.MF_API_BASE" in app_js and 'const API = "/api";' not in app_js


def test_vercel_build_writes_config_js_from_the_env():
    vercel = json.loads((FRONTEND / "vercel.json").read_text(encoding="utf-8"))
    assert "API_BASE_URL" in vercel["buildCommand"] and "config.js" in vercel["buildCommand"]
    assert vercel["outputDirectory"] == "."
    sources = {h["source"] for h in vercel["headers"]}
    assert "/config.js" in sources  # short cache, so a changed API URL takes effect quickly


async def test_config_js_is_served_locally(tmp_path):
    from app.pipeline.catalog import Catalog

    store = ThreadStore(make_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}"))
    catalog = Catalog.load()
    _, services = build(catalog, store)
    app = create_app(services=services)  # with the real frontend mount
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/config.js")
    assert r.status_code == 200 and "MF_API_BASE" in r.text


# ------------------------------------------------------------------------------ §4.2 settings
def _keys(node, prefix=""):
    if not isinstance(node, dict):
        return {prefix}
    out = set()
    for k, v in node.items():
        out |= _keys(v, f"{prefix}.{k}" if prefix else str(k))
    return out


def test_production_settings_have_the_same_keys_as_development():
    dev = yaml.safe_load(DEV_SETTINGS.read_text(encoding="utf-8"))
    prod = yaml.safe_load(PROD_SETTINGS.read_text(encoding="utf-8"))
    assert _keys(prod) == _keys(dev)


def test_production_values():
    s = load_settings(PROD_SETTINGS)
    assert s.api.client_ip_header == "x-real-ip"
    assert s.api.trust_forwarded_for is False
    assert all(o.startswith("https://") for o in s.api.cors_origins)
    assert not any("localhost" in o or "127.0.0.1" in o for o in s.api.cors_origins)
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", s.data_refresh.release_repo)
    assert s.ops.log_format == "json" and s.ops.reload_check_seconds == 60
    # Everything not deliberately different is the same as development.
    dev = load_settings(DEV_SETTINGS)
    for section in ("llm", "retrieval", "ingestion", "chunking", "threads"):
        assert getattr(s, section) == getattr(dev, section), section
    # No secrets in the committed file: they are Railway variables.
    prod = yaml.safe_load(PROD_SETTINGS.read_text(encoding="utf-8"))
    assert not {"groq_api_key", "data_refresh_token", "database_url"} & set(prod)


def test_app_settings_file_selects_the_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv(SETTINGS_FILE_ENV, raising=False)
    assert settings_file() == ROOT_DIR / "config" / "settings.yaml"

    monkeypatch.setenv(SETTINGS_FILE_ENV, str(PROD_SETTINGS))
    assert settings_file() == PROD_SETTINGS
    assert load_settings().api.client_ip_header == "x-real-ip"

    # Relative to the cwd (Railway runs from /app/backend) ...
    monkeypatch.chdir(ROOT_DIR / "backend")
    monkeypatch.setenv(SETTINGS_FILE_ENV, "../config/settings.production.yaml")
    assert settings_file().resolve() == PROD_SETTINGS.resolve()
    # ... or to the project root.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(SETTINGS_FILE_ENV, "config/settings.production.yaml")
    assert settings_file().resolve() == PROD_SETTINGS.resolve()

    monkeypatch.setenv(SETTINGS_FILE_ENV, "config/nope.yaml")
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        settings_file()


# ------------------------------------------------------------------------------ §4.3 client IP
def scope(headers=(), peer="10.0.0.1"):
    return {"client": (peer, 5000),
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


def test_client_ip_header_is_used_only_when_configured():
    s = scope([("x-real-ip", "203.0.113.7"), ("x-forwarded-for", "198.51.100.1")])
    assert client_key(s, False) == "10.0.0.1"  # not configured: the socket peer
    assert client_key(s, False, "x-real-ip") == "203.0.113.7"
    assert client_key(s, True, "X-Real-IP") == "203.0.113.7"  # wins over X-Forwarded-For
    assert client_key(s, True) == "198.51.100.1"
    # Configured but missing or empty: fall back, never share one empty key.
    assert client_key(scope(), False, "x-real-ip") == "10.0.0.1"
    assert client_key(scope([("x-real-ip", " ")]), False, "x-real-ip") == "10.0.0.1"


async def test_rate_limit_is_per_real_client_behind_the_edge(tmp_path):
    from app.pipeline.catalog import Catalog

    catalog = Catalog.load()
    for header, second_user in (("x-real-ip", 200), ("", 429)):
        store = ThreadStore(make_engine(f"sqlite:///{(tmp_path / f'{header}.db').as_posix()}"))
        settings = settings_with(api={"rate_limit_per_minute": 1, "client_ip_header": header})
        app, _ = build(catalog, store, settings=settings)
        async with client(app, ip="10.9.9.9") as edge:  # every request arrives from the edge
            tid = (await edge.post("/api/threads")).json()["id"]
            url, body = f"/api/threads/{tid}/messages", {"content": "Kotak exit load?"}
            r = await edge.post(url, json=body, headers={"X-Real-IP": "203.0.113.1"})
            assert r.status_code == 200
            r = await edge.post(url, json=body, headers={"X-Real-IP": "203.0.113.1"})
            assert r.status_code == 429
            r = await edge.post(url, json=body, headers={"X-Real-IP": "203.0.113.2"})
            assert r.status_code == second_user


# ------------------------------------------------------------------------------ §4.6 CORS
async def test_cors_allows_only_the_configured_frontends(tmp_path):
    from app.pipeline.catalog import Catalog

    store = ThreadStore(make_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}"))
    settings = settings_with(api={
        "cors_origins": ["https://mf-faq.vercel.app"],
        "cors_origin_regex": r"^https://mf-faq-[a-z0-9]+-team\.vercel\.app$",
    })
    app, _ = build(Catalog.load(), store, settings=settings)
    allowed = ("https://mf-faq.vercel.app", "https://mf-faq-abc123-team.vercel.app")
    refused = ("https://evil.example", "https://mf-faq-abc123-team.vercel.app.evil.example",
               "http://localhost:5173")
    async with client(app) as c:
        for origin in allowed:
            r = await c.get("/api/meta", headers={"Origin": origin})
            assert r.headers.get("access-control-allow-origin") == origin
        for origin in refused:
            r = await c.get("/api/meta", headers={"Origin": origin})
            assert "access-control-allow-origin" not in r.headers, origin
        pre = await c.options("/api/threads/x/messages", headers={
            "Origin": allowed[0], "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        assert pre.status_code == 200


# ------------------------------------------------------------------------------ §4.4 empty volume
@pytest.fixture
def empty_server(tmp_path, monkeypatch):
    """A server whose data folder is empty, plus a bundle published by the daily workflow."""
    from tests.test_indexer import CORPUS

    data = tmp_path / "volume"
    data.mkdir()
    settings = corpus_settings(tmp_path, data).model_copy(update={
        "data_refresh": get_settings().data_refresh.model_copy(update={"release_repo": REPO})})
    ci = tmp_path / "ci-data"
    ci_settings = corpus_settings(tmp_path / "ci", ci)
    embedder = make_corpus(ci, ci_settings, fetched=datetime(2026, 11, 10, 4, 40),
                           chunks=CORPUS)
    archive, meta = pack(tmp_path / "dist", settings=ci_settings, data_dir=ci,
                         now=datetime(2026, 11, 10, 4, 50, tzinfo=UTC))
    published = (tmp_path / "dist" / "bundle.json").read_bytes()
    # The real model isn't needed: loading a corpus uses the fake embedder it was built with.
    monkeypatch.setattr("ingest.indexer.make_embedder", lambda _settings: embedder)
    return settings, data, archive.read_bytes(), published, meta


def test_load_pipeline_degrades_without_a_corpus(empty_server, monkeypatch):
    settings, data, *_ = empty_server
    monkeypatch.setattr("app.corpus.active_corpus_dir", lambda *a: data)
    pipeline = load_pipeline(settings.model_copy(update={"groq_api_key": SecretStr("")}),
                             AnswerPipeline)
    assert pipeline.retriever is None and len(pipeline.catalog.schemes) == 19


def test_load_pipeline_degrades_when_the_corpus_is_broken(empty_server, monkeypatch):
    settings, data, *_ = empty_server
    (data / "index_manifest.json").write_text("{}", encoding="utf-8")  # no indexes behind it
    monkeypatch.setattr("app.corpus.active_corpus_dir", lambda *a: data)
    pipeline = load_pipeline(settings, AnswerPipeline)
    assert pipeline.retriever is None


async def test_degraded_pipeline_is_honest_and_keeps_its_guards(empty_server):
    settings, *_ = empty_server
    pipeline = AnswerPipeline.degraded(settings, use_llm=False)
    ctx = ThreadContext()

    r = await pipeline.answer(ctx, "What is the exit load of Kotak Multi Cap Fund?")
    assert r.route == "corpus_unavailable" and r.reply.type == "unavailable"
    assert "still being loaded" in r.reply.answer
    assert r.reply.citation_url == pipeline.catalog.scheme_link("kotak-multi-cap").url
    assert "None" not in r.reply.rendered

    r = await pipeline.answer(ctx, "What is an exit load?")
    assert r.route == "corpus_unavailable"
    assert r.reply.citation_url == pipeline.catalog.education_link("default").url

    r = await pipeline.answer(ctx, "My PAN is ABCDE1234F, what is the exit load?")
    assert r.route == "pii_blocked" and "ABCDE1234F" not in r.stored_user_text
    r = await pipeline.answer(ctx, "Should I invest in Parag Parikh Flexi Cap?")
    assert r.route == "advisory"


async def test_empty_volume_boots_degraded_then_switches_to_the_pulled_bundle(empty_server):
    settings, data, archive, published, meta = empty_server
    pipeline = AnswerPipeline.degraded(settings, use_llm=False)
    store = ThreadStore(make_engine(settings.database_url))
    services = Services(settings=settings, pipeline=pipeline, store=store,
                        catalog=pipeline.catalog)
    app = create_app(services=services, frontend_dir=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/api/health")
        assert r.status_code == 200  # the deploy health check passes while degraded
        health = r.json()
        assert health["index_loaded"] is False and health["status"] == "degraded"
        tid = (await c.post("/api/threads")).json()["id"]
        r = await c.post(f"/api/threads/{tid}/messages",
                         json={"content": "What is the exit load of Kotak Multi Cap Fund?"})
        assert r.status_code == 200 and r.json()["type"] == "unavailable"

    reloader = CorpusReloader(services, data)
    assert reloader.signature is None
    puller = BundlePuller(services, reloader, data, transport=github(published, archive, []))
    assert await puller.check() == "installed"
    assert services.pipeline.retriever is not None
    assert services.catalog.fetched_at[KOTAK_SOURCE].isoformat() == "2026-11-10"
    assert active_corpus_dir(data).name.startswith(meta["id"])


async def test_reload_check_picks_up_a_bundle_installed_by_hand(empty_server):
    settings, data, archive, _, meta = empty_server
    from ingest.bundle import install

    pipeline = AnswerPipeline.degraded(settings, use_llm=False)
    services = Services(settings=settings, pipeline=pipeline,
                        store=ThreadStore(make_engine(settings.database_url)),
                        catalog=pipeline.catalog)
    reloader = CorpusReloader(services, data)
    assert await reloader.check() is False  # nothing there yet
    path = data.parent / "b.tar.gz"
    path.write_bytes(archive)
    target, _ = install(path, data, meta["archive_sha256"])
    set_current(data, target.name)
    assert await reloader.check() is True
    assert services.pipeline.retriever.data_dir == target


# ------------------------------------------------------------------------------ app.bootstrap
async def test_bootstrap_installs_the_first_bundle(empty_server):
    settings, data, archive, published, meta = empty_server
    checked = []
    status = await bootstrap(settings, data, transport=github(published, archive, []),
                             verify=checked.append)
    assert status == "installed"
    active = active_corpus_dir(data)
    assert active.name.startswith(meta["id"]) and checked == [active]
    assert json.loads((active / "bundle.json").read_text(encoding="utf-8"))["archive_sha256"]
    assert not list((data / "bundles" / ".download").glob("*"))

    calls = []  # a corpus is present now: nothing is fetched again
    assert await bootstrap(settings, data, transport=github(published, archive, calls),
                           verify=checked.append) == "present"
    assert calls == []


async def test_bootstrap_with_the_real_corpus_check(empty_server):
    settings, data, archive, published, _ = empty_server
    assert await bootstrap(settings, data, transport=github(published, archive, [])) \
        == "installed"


async def test_bootstrap_never_activates_a_bad_bundle(empty_server):
    settings, data, archive, published, _ = empty_server
    corrupt = archive[:-100] + b"x" * 100
    assert await bootstrap(settings, data, transport=github(published, corrupt, []),
                           verify=lambda _: None) == "failed"
    assert not (data / "bundles" / "CURRENT").exists()

    def broken(_):
        raise RuntimeError("index mismatch")

    assert await bootstrap(settings, data, transport=github(published, archive, []),
                           verify=broken) == "failed"
    assert not (data / "bundles" / "CURRENT").exists()
    assert [p for p in (data / "bundles").iterdir() if not p.name.startswith(".")] == []


async def test_bootstrap_survives_github_being_down(empty_server):
    settings, data, *_ = empty_server
    down = httpx.MockTransport(lambda request: httpx.Response(503))
    assert await bootstrap(settings, data, transport=down) == "failed"


async def test_bootstrap_without_a_repo_does_nothing(empty_server):
    settings, data, *_ = empty_server
    settings = settings.model_copy(update={
        "data_refresh": settings.data_refresh.model_copy(update={"release_repo": ""})})
    calls = []
    assert await bootstrap(settings, data, transport=github(b"{}", b"", calls)) == "disabled"
    assert calls == []


def test_bootstrap_main_always_exits_0(monkeypatch):
    import app.bootstrap as boot

    async def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(boot, "bootstrap", explode)
    monkeypatch.setattr("app.observability.configure_logging", lambda *a: None)
    assert boot.main() == 0


# ------------------------------------------------------------------------------ §4.5 container
def test_container_and_platform_config():
    dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "download.pytorch.org/whl/cpu" in dockerfile and "pip install -e ." in dockerfile
    assert get_settings().retrieval.embedding_model in dockerfile  # the model that is baked in
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "python -m app.bootstrap && exec uvicorn app.main:app" in dockerfile
    assert "--workers 1" in dockerfile and "${PORT:-8000}" in dockerfile

    ignored = (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".git", ".env", "data/", ".venv"):
        assert entry in ignored, entry

    railway = json.loads((ROOT_DIR / "railway.json").read_text(encoding="utf-8"))
    assert railway["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    assert railway["deploy"]["healthcheckPath"] == "/api/health"
    assert railway["deploy"]["numReplicas"] == 1


def test_data_dir_is_the_volume_mount_point():
    # Railway mounts the volume at /app/data; ROOT_DIR is /app in the image (editable install).
    from app.config import DATA_DIR

    assert DATA_DIR == ROOT_DIR / "data"
    assert Path(get_settings().database_url.removeprefix("sqlite:///")).parent == DATA_DIR
