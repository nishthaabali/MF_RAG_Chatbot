"""Phase 9: the chat UI is served by FastAPI from frontend/ without shadowing the API."""

import re
from dataclasses import replace

import pytest

from app.config import ROOT_DIR
from app.main import create_app
from app.pipeline.catalog import Catalog
from app.store.db import make_engine
from app.store.threads import ThreadStore
from tests.test_answer_pipeline import CONFIRMED
from tests.test_api import build, client_for

FRONTEND = ROOT_DIR / "frontend"


@pytest.fixture(scope="module")
def catalog():
    cat = Catalog.load()
    return replace(cat, fetched_at={sid: CONFIRMED for sid in cat.source_urls})


@pytest.fixture
def store(tmp_path):
    return ThreadStore(make_engine(f"sqlite:///{(tmp_path / 'threads.db').as_posix()}"))


async def test_index_is_served_with_disclaimer_and_assets(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        r = await c.get("/")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        html = r.text
        assert "Facts-only. No investment advice." in html
        assert 'aria-live="polite"' in html
        for asset in ("styles.css", "app.js"):
            assert f'"{asset}"' in html
            res = await c.get(f"/{asset}")
            assert res.status_code == 200 and res.text


async def test_api_and_docs_still_win_over_static_mount(catalog, store):
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        meta = (await c.get("/api/meta")).json()
        assert len(meta["examples"]) == 3 and meta["disclaimer"]
        assert (await c.get("/api/threads/does-not-exist")).status_code == 404
        assert (await c.get("/docs")).status_code == 200
        assert (await c.get("/openapi.json")).status_code == 200


def test_no_mount_without_frontend(tmp_path):
    app = create_app(services=None, frontend_dir=tmp_path)  # empty dir: no index.html
    assert all(getattr(r, "name", None) != "frontend" for r in app.routes)


def test_ui_takes_thread_titles_from_the_server():
    """The server builds titles (from the PII placeholder when a message is withheld), so the UI
    must never use what was typed as a title: it would end up in localStorage."""
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert re.search(r"title:\s*full\.title", js)
    assert not re.search(r"title:\s*(text|rawText|userMsg)", js)


async def test_chat_management_controls_are_served(catalog, store):
    """docs/issues.md #3 and #4: pin, rename, Markdown export and the collapsible sidebar."""
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        html = (await c.get("/")).text
        js = (await c.get("/app.js")).text
    for control in ('id="collapse-sidebar"', 'id="pin-current"', 'id="rename-current"',
                    'id="export-current"', 'id="thread-menu"'):
        assert control in html
    assert 'aria-controls="sidebar"' in html
    assert "text/markdown" in js and 'method: "PATCH"' in js
