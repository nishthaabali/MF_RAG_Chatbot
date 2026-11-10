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


def _js_function(js: str, name: str) -> str:
    """The source of a top-level `function name(...) { ... }` in app.js (brace-matched)."""
    start = js.index(f"function {name}(")
    depth, i = 0, js.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(js[i], 0)
        i += 1
        if depth == 0:
            return js[start:i]


def test_knowledge_scope_counts_every_compared_scheme_as_cited():
    """A comparison reply cites one page per column, but its citation_url is only the first
    column's. "Cited in this chat" must mark every compared scheme, not just the first."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "citedSourceUrls(state.activeId" in js  # the scheme panel uses the helper
    a, b, c = (f"https://groww.in/mutual-funds/{s}" for s in ("a", "b", "c"))
    msgs = [
        {"role": "user", "content": "compare A and B"},
        {"role": "assistant", "type": "comparison", "citation_url": a,
         "comparison": {"columns": [{"url": a}, {"url": b}], "rows": []}},
        {"role": "assistant", "type": "answer", "citation_url": c, "comparison": None},
        {"role": "assistant", "type": "pii_blocked", "citation_url": None},
    ]
    call = f"[...citedSourceUrls({json.dumps(msgs)})]"
    script = (_js_function(js, "citedSourceUrls")
              + f"\nprocess.stdout.write(JSON.stringify({call}));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert sorted(json.loads(out.stdout)) == [a, b, c]


async def test_ui_assets_are_always_revalidated(catalog, store):
    """Asset names carry no version, so a browser must revalidate them or it may keep running
    an old app.js after a deploy. The API routes are unaffected."""
    app, _, _ = build(catalog, store)
    async with await client_for(app) as c:
        for path in ("/", "/app.js", "/styles.css"):
            r = await c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
        assert "cache-control" not in (await c.get("/api/meta")).headers
