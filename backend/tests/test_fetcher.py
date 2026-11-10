import json
from datetime import UTC, datetime

import httpx
import pytest

from app.config import load_settings
from ingest.fetcher import Fetcher, load_from_disk

NOW = datetime(2026, 11, 9, 20, 0, tzinfo=UTC)  # 10 Nov 2026 01:30 IST
GROWW = {
    "id": "groww-x",
    "url": "https://groww.in/mutual-funds/x",
    "type": "scheme_page",
    "render": "static",
}
AMFI = {
    "id": "amfi-x",
    "url": "https://www.mutualfundssahihai.com/en/x",
    "type": "amfi",
    "render": "static",
}
SCHEME_BODY = b'<html><script id="__NEXT_DATA__">{"mfServerSideData":{}}</script></html>'
ARTICLE_BODY = (
    b"<html><body><p>" + b"Exit load is a fee charged on early redemption. " * 10 + b"</p>"
)


class Recorder:
    def __init__(self):
        self.sleeps = []

    async def __call__(self, seconds):
        self.sleeps.append(seconds)


def make_fetcher(tmp_path, handler, browser=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    sleep = Recorder()
    fetcher = Fetcher(
        load_settings(), tmp_path, client=client, browser=browser, sleep=sleep, now=lambda: NOW
    )
    return fetcher, sleep


def site(pages: dict, robots: str | int = "User-agent: *\nAllow: /\n"):
    """MockTransport handler: pages maps path -> (status, body, headers) or a list of those."""
    calls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            if isinstance(robots, int):
                return httpx.Response(robots)
            return httpx.Response(200, text=robots)
        calls[path] = calls.get(path, 0) + 1
        spec = pages[path]
        if isinstance(spec, list):
            spec = spec[min(calls[path], len(spec)) - 1]
        status, body, headers = spec
        return httpx.Response(status, content=body, headers=headers)

    handler.calls = calls
    return handler


HTML = {"content-type": "text/html; charset=utf-8"}


async def test_success_saves_snapshot_and_metadata(tmp_path):
    fetcher, _ = make_fetcher(tmp_path, site({"/mutual-funds/x": (200, SCHEME_BODY, HTML)}))
    result = await fetcher.fetch(GROWW)
    assert result.status == "ok"
    assert result.path == tmp_path / "groww-x" / "2026-11-10.html"  # IST date
    assert result.path.read_bytes() == SCHEME_BODY
    meta = json.loads((tmp_path / "groww-x" / "2026-11-10.meta.json").read_text())
    assert meta["sha256"] == result.sha256
    assert meta["fetched_at"] == NOW.isoformat()


async def test_retries_on_503_with_backoff_then_succeeds(tmp_path):
    handler = site({"/mutual-funds/x": [(503, b"", {}), (503, b"", {}), (200, SCHEME_BODY, HTML)]})
    fetcher, sleep = make_fetcher(tmp_path, handler)
    result = await fetcher.fetch(GROWW)
    assert result.status == "ok"
    assert handler.calls["/mutual-funds/x"] == 3
    assert 1 in sleep.sleeps and 2 in sleep.sleeps  # exponential backoff


async def test_honours_retry_after_on_429(tmp_path):
    handler = site(
        {"/mutual-funds/x": [(429, b"", {"retry-after": "7"}), (200, SCHEME_BODY, HTML)]}
    )
    fetcher, sleep = make_fetcher(tmp_path, handler)
    assert (await fetcher.fetch(GROWW)).status == "ok"
    assert 7 in sleep.sleeps


async def test_gives_up_after_max_retries_and_writes_nothing(tmp_path):
    fetcher, _ = make_fetcher(tmp_path, site({"/mutual-funds/x": (503, b"", {})}))
    result = await fetcher.fetch(GROWW)
    assert result.status == "failed"
    assert "3 attempts" in result.error
    assert not (tmp_path / "groww-x").exists()


async def test_404_fails_without_retry(tmp_path):
    handler = site({"/mutual-funds/x": (404, b"", {})})
    fetcher, _ = make_fetcher(tmp_path, handler)
    result = await fetcher.fetch(GROWW)
    assert result.status == "failed" and "404" in result.error
    assert handler.calls["/mutual-funds/x"] == 1


async def test_block_page_is_rejected(tmp_path):
    block = b"<html><body>Access denied. Please verify you are human.</body></html>"
    fetcher, _ = make_fetcher(tmp_path, site({"/mutual-funds/x": (200, block, HTML)}))
    result = await fetcher.fetch(GROWW)
    assert result.status == "failed"
    assert "embedded data" in result.error


async def test_short_article_page_is_rejected(tmp_path):
    fetcher, _ = make_fetcher(tmp_path, site({"/en/x": (200, b"<html><p>Hi</p></html>", HTML)}))
    assert (await fetcher.fetch(AMFI)).status == "failed"


@pytest.mark.parametrize("robots", ["User-agent: *\nDisallow: /mutual-funds/\n", 403, 500])
async def test_robots_disallow_403_and_5xx_skip_the_page(tmp_path, robots):
    handler = site({"/mutual-funds/x": (200, SCHEME_BODY, HTML)}, robots=robots)
    fetcher, _ = make_fetcher(tmp_path, handler)
    result = await fetcher.fetch(GROWW)
    assert result.status == "skipped_robots"
    assert handler.calls == {}


async def test_missing_robots_404_allows_fetching(tmp_path):
    handler = site({"/mutual-funds/x": (200, SCHEME_BODY, HTML)}, robots=404)
    fetcher, _ = make_fetcher(tmp_path, handler)
    assert (await fetcher.fetch(GROWW)).status == "ok"


async def test_redirect_to_another_page_is_rejected(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/mutual-funds/x":
            return httpx.Response(301, headers={"location": "https://groww.in/mutual-funds/y"})
        return httpx.Response(200, content=SCHEME_BODY, headers=HTML)

    fetcher, _ = make_fetcher(tmp_path, handler)
    result = await fetcher.fetch(GROWW)
    assert result.status == "failed" and "redirected" in result.error


async def test_browser_render_is_used_for_browser_sources(tmp_path):
    seen = []

    async def browser(url):
        seen.append(url)
        return ARTICLE_BODY, "text/html", url

    source = {**AMFI, "render": "browser"}
    fetcher, _ = make_fetcher(tmp_path, site({}), browser=browser)
    assert (await fetcher.fetch(source)).status == "ok"
    assert seen == [AMFI["url"]]


async def test_polite_delay_between_requests(tmp_path):
    pages = {"/mutual-funds/x": (200, SCHEME_BODY, HTML)}
    fetcher, sleep = make_fetcher(tmp_path, site(pages))
    await fetcher.fetch(GROWW)
    await fetcher.fetch(GROWW)
    assert sleep.sleeps.count(load_settings().ingestion.rate_limit_seconds) == 1


# --------------------------------------------------------------------------- --from-disk


def test_from_disk_uses_newest_snapshot_and_its_saved_date(tmp_path):
    folder = tmp_path / "groww-x"
    folder.mkdir()
    (folder / "2026-11-01.html").write_bytes(b"old")
    (folder / "2026-11-05.html").write_bytes(SCHEME_BODY)
    (folder / "2026-11-05.meta.json").write_text(
        json.dumps({"fetched_at": "2026-11-05T03:00:00+00:00"})
    )
    result = load_from_disk(GROWW, tmp_path)
    assert result.status == "from_disk"
    assert result.path.name == "2026-11-05.html"
    assert result.fetched_at == datetime(2026, 11, 5, 3, 0, tzinfo=UTC)


def test_from_disk_without_metadata_uses_file_save_time(tmp_path):
    folder = tmp_path / "groww-x"
    folder.mkdir()
    path = folder / "manual.html"
    path.write_bytes(SCHEME_BODY)
    result = load_from_disk(GROWW, tmp_path)
    assert result.fetched_at == datetime.fromtimestamp(path.stat().st_mtime, UTC)


def test_from_disk_missing_snapshot_fails_cleanly(tmp_path):
    result = load_from_disk(GROWW, tmp_path)
    assert result.status == "failed" and "no saved snapshot" in result.error


# --------------------------------------------------------------------------- pagination

GLOSSARY = {
    "id": "amfi-glossary",
    "url": "https://www.mutualfundssahihai.com/en/glossary",
    "type": "amfi",
    "render": "browser",
    "paginate": {
        "pages": 'nav[aria-label="pagination"] a[aria-label^="Go to page"]',
        "items": "h3",
        "max_pages": 10,
    },
}
DEFINITION = "is a term defined for mutual fund investors in plain language. " * 3


def glossary_page(terms, pages=3, page_links=None):
    links = page_links or [f"/en/glossary?page={n}" for n in range(2, pages + 1)]
    items = "".join(f"<div><h3>{t}</h3><p>{t} {DEFINITION}</p></div>" for t in terms)
    nav = "".join(
        f'<li><a href="{href}" aria-label="Go to page {i}">{i}</a></li>'
        for i, href in enumerate(links, start=2)
    )
    return (
        "<html><body><header>Menu</header>"
        f'<main><h1>Glossary</h1><div class="list">{items}</div></main>'
        f'<nav aria-label="pagination"><ul><li><a href="/en/glossary?page=1" '
        f'aria-label="Page 1">1</a></li>{nav}</ul></nav></body></html>'
    ).encode()


def fake_browser(pages: dict):
    seen = []

    async def browser(url):
        seen.append(url)
        if url not in pages:
            raise TimeoutError(f"no such page {url}")
        return pages[url], "text/html", url

    browser.seen = seen
    return browser


BASE = GLOSSARY["url"]


async def test_paginated_listing_is_merged_into_one_snapshot(tmp_path):
    browser = fake_browser(
        {
            BASE: glossary_page(["Asset Management Company", "Benchmark"]),
            f"{BASE}?page=2": glossary_page(["Exit Load", "Expense Ratio"]),
            f"{BASE}?page=3": glossary_page(["Systematic Investment Plan"]),
        }
    )
    fetcher, sleep = make_fetcher(tmp_path, site({}), browser=browser)
    result = await fetcher.fetch(GLOSSARY)

    assert result.status == "ok"
    assert browser.seen == [BASE, f"{BASE}?page=2", f"{BASE}?page=3"]  # page 1 link not refetched
    html = result.path.read_text(encoding="utf-8")
    for term in ("Benchmark", "Exit Load", "Expense Ratio", "Systematic Investment Plan"):
        assert f"<h3>{term}</h3>" in html
    assert html.count("<h1>") == 1  # only items are merged, not whole pages
    assert 'aria-label="pagination"' not in html
    rate = load_settings().ingestion.rate_limit_seconds
    assert sleep.sleeps.count(rate) == 2  # polite delay before each extra page


async def test_failed_page_fails_the_whole_fetch(tmp_path):
    browser = fake_browser(
        {
            BASE: glossary_page(["Benchmark"]),
            f"{BASE}?page=2": glossary_page(["Exit Load"]),
            # page 3 missing -> browser times out on every retry
        }
    )
    fetcher, _ = make_fetcher(tmp_path, site({}), browser=browser)
    result = await fetcher.fetch(GLOSSARY)
    assert result.status == "failed" and "gave up" in result.error
    assert not (tmp_path / "amfi-glossary").exists()  # partial listing never saved


async def test_page_repeating_earlier_items_is_rejected(tmp_path):
    """The site ignoring ?page= and serving page 1 again must not pass as complete."""
    same = glossary_page(["Benchmark"], pages=2)
    browser = fake_browser({BASE: same, f"{BASE}?page=2": same})
    fetcher, _ = make_fetcher(tmp_path, site({}), browser=browser)
    result = await fetcher.fetch(GLOSSARY)
    assert result.status == "failed" and "repeats items" in result.error


async def test_pagination_link_to_another_url_is_rejected(tmp_path):
    page = glossary_page(["Benchmark"], page_links=["https://evil.example/en/glossary?page=2"])
    fetcher, _ = make_fetcher(tmp_path, site({}), browser=fake_browser({BASE: page}))
    result = await fetcher.fetch(GLOSSARY)
    assert result.status == "failed" and "leaves the allowlisted URL" in result.error


async def test_too_many_pages_is_rejected(tmp_path):
    source = {**GLOSSARY, "paginate": {**GLOSSARY["paginate"], "max_pages": 2}}
    fetcher, _ = make_fetcher(
        tmp_path, site({}), browser=fake_browser({BASE: glossary_page(["Benchmark"], pages=3)})
    )
    result = await fetcher.fetch(source)
    assert result.status == "failed" and "more than max_pages=2" in result.error


async def test_single_page_listing_needs_no_extra_requests(tmp_path):
    browser = fake_browser({BASE: glossary_page(["Benchmark", "Exit Load"], pages=1)})
    fetcher, _ = make_fetcher(tmp_path, site({}), browser=browser)
    assert (await fetcher.fetch(GLOSSARY)).status == "ok"
    assert browser.seen == [BASE]
