"""Polite fetcher for the allowlisted sources (architecture §5.1).

- `render: static` uses httpx; `render: browser` uses headless Chromium via Playwright.
- Descriptive User-Agent, a fixed delay between requests, retries with exponential backoff on
  429 / 5xx / network errors (honouring Retry-After), and a robots.txt check per host.
- A response is accepted only if it passes a content sanity check, so a CAPTCHA or block page
  is treated as a failed fetch rather than as new content (edge case FET-06).
- Snapshots go to data/raw/<source_id>/<YYYY-MM-DD>.html|pdf with a .meta.json sidecar.
  On failure nothing is written: the previous snapshot and its date stay in use.
- Paginated listings (a source with a `paginate` block, e.g. the AMFI glossary, which shows
  10 terms per page) are fetched page by page and merged into one snapshot. If any page
  fails, the whole fetch fails, so a partial listing never replaces a complete one.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import Settings

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 60
MIN_TEXT_CHARS = 200


class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    source_id: str
    status: str  # ok | failed | skipped_robots | from_disk
    path: Path | None = None
    sha256: str | None = None
    content_type: str | None = None
    fetched_at: datetime | None = None
    error: str | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _same_target(requested: str, final: str) -> bool:
    a, b = urlparse(requested), urlparse(final)
    return (a.netloc.lower(), a.path.rstrip("/")) == (b.netloc.lower(), b.path.rstrip("/"))


def check_content(source: dict, body: bytes, content_type: str) -> None:
    """Reject block pages, consent walls and empty shells (FET-06)."""
    if "pdf" in content_type:
        if not body.startswith(b"%PDF"):
            raise FetchError("response claims to be a PDF but is not one")
        return
    text = body.decode("utf-8", errors="replace")
    if source["type"] == "scheme_page":
        if "__NEXT_DATA__" not in text or "mfServerSideData" not in text:
            raise FetchError(
                "scheme page is missing its embedded data (block page or layout change)"
            )
        return
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    visible = " ".join(re.sub(r"<[^>]+>", " ", visible).split())
    if len(visible) < MIN_TEXT_CHARS:
        raise FetchError(f"page has only {len(visible)} characters of text")
    if re.search(r"access denied|verify you are human|captcha", visible[:2000], re.I):
        raise FetchError("page looks like a block or CAPTCHA page")


class RobotsCache:
    """robots.txt per host, following urllib.robotparser's conventions.

    404 and other 4xx -> allow all; 401/403 -> disallow all;
    5xx or unreachable -> disallow (edge case FET-02).
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client = client
        self.user_agent = user_agent
        self._parsers: dict[str, robotparser.RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._parsers:
            self._parsers[host] = await self._load(host)
        parser = self._parsers[host]
        return parser.can_fetch(self.user_agent, url) if parser else False

    async def _load(self, host: str) -> robotparser.RobotFileParser | None:
        parser = robotparser.RobotFileParser()
        try:
            resp = await self.client.get(f"{host}/robots.txt", follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallowed", host, exc)
            return None
        if resp.status_code in (401, 403):
            log.warning(
                "robots.txt for %s returned %s; treating as disallowed", host, resp.status_code
            )
            return None
        if 400 <= resp.status_code < 500:
            parser.parse([])  # no robots.txt: everything allowed
        elif resp.status_code >= 500:
            log.warning(
                "robots.txt for %s returned %s; treating as disallowed", host, resp.status_code
            )
            return None
        else:
            parser.parse(resp.text.splitlines())
        return parser


BrowserFetch = Callable[[str], Awaitable[tuple[bytes, str, str]]]


class PlaywrightBrowser:
    """Lazily started headless Chromium; returns (body, content_type, final_url)."""

    def __init__(self, user_agent: str, timeout_seconds: float):
        self.user_agent = user_agent
        self.timeout_ms = timeout_seconds * 1000
        self._pw = None
        self._browser = None

    async def __call__(self, url: str) -> tuple[bytes, str, str]:
        if self._browser is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch()
        context = await self._browser.new_context(user_agent=self.user_agent)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            return (await page.content()).encode("utf-8"), "text/html", page.url
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            await self._pw.stop()
            self._browser = self._pw = None


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        raw_dir: Path,
        client: httpx.AsyncClient | None = None,
        browser: BrowserFetch | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = utcnow,
    ):
        cfg = settings.ingestion
        self.raw_dir = raw_dir
        self.rate_limit = cfg.rate_limit_seconds
        self.max_retries = cfg.max_retries
        self.user_agent = cfg.user_agent
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent},
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
        )
        self._own_browser = browser is None
        self.browser = browser or PlaywrightBrowser(cfg.user_agent, cfg.timeout_seconds)
        self.robots = RobotsCache(self.client, cfg.user_agent)
        self.sleep = sleep
        self.now = now
        self._requests_made = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        if self._own_client:
            await self.client.aclose()
        if self._own_browser:
            await self.browser.close()

    async def fetch(self, source: dict) -> FetchResult:
        sid, url = source["id"], source["url"]
        try:
            if not await self.robots.allowed(url):
                log.warning("%s: blocked by robots.txt", sid)
                return FetchResult(sid, "skipped_robots", error="disallowed by robots.txt")
            body, content_type = await self._get_with_retries(source)
            if source.get("paginate") and "html" in content_type:
                body = await self._fetch_remaining_pages(source, body)
            check_content(source, body, content_type)
        except FetchError as exc:
            log.warning("%s: fetch failed: %s", sid, exc)
            return FetchResult(sid, "failed", error=str(exc))
        return self._save(sid, url, body, content_type)

    async def _fetch_remaining_pages(self, source: dict, first: bytes) -> bytes:
        """Fetch pages 2..N of a paginated listing and merge their items into page 1.

        `paginate.pages` selects the links to the other pages; `paginate.items` selects one
        element per listed item (e.g. each term's heading). Only links to the allowlisted URL
        itself (same host and path, different query) are followed.
        """
        cfg = source["paginate"]
        sid, url = source["id"], source["url"]
        max_pages = int(cfg.get("max_pages", DEFAULT_MAX_PAGES))
        doc = BeautifulSoup(first, "html.parser")
        container = _items_container(doc, cfg["items"], sid, 1)
        seen = {_item_key(e) for e in container.select(cfg["items"])}

        links: list[str] = []
        for a in doc.select(cfg["pages"]):
            link = urljoin(url, a.get("href") or "")
            if link not in links and link != url:
                if not _same_target(url, link):
                    raise FetchError(f"pagination link {link} leaves the allowlisted URL")
                if not await self.robots.allowed(link):
                    raise FetchError(f"pagination link {link} is disallowed by robots.txt")
                links.append(link)
        if len(links) + 1 > max_pages:
            raise FetchError(f"listing has {len(links) + 1} pages, more than max_pages={max_pages}")

        for n, link in enumerate(links, start=2):
            body, content_type = await self._get_with_retries(source, link)
            if "html" not in content_type:
                raise FetchError(f"page {n} ({link}) is not HTML")
            page = BeautifulSoup(body, "html.parser")
            items = _items_container(page, cfg["items"], sid, n)
            keys = {_item_key(e) for e in items.select(cfg["items"])}
            if keys <= seen:
                raise FetchError(f"page {n} ({link}) repeats items already fetched")
            seen |= keys
            for child in list(items.children):
                container.append(child)

        while (a := doc.select_one(cfg["pages"])) is not None:  # page links no longer apply
            (a.find_parent("nav") or a).decompose()
        log.info("%s: merged %d pages, %d items", sid, len(links) + 1, len(seen))
        return str(doc).encode("utf-8")

    async def _get_with_retries(self, source: dict, url: str | None = None) -> tuple[bytes, str]:
        url = url or source["url"]
        last_error = "unknown error"
        for attempt in range(self.max_retries):
            await self._polite_delay()
            delay = 2**attempt
            try:
                if source.get("render") == "browser":
                    body, content_type, final_url = await self.browser(url)
                else:
                    resp = await self.client.get(url)
                    if resp.status_code in RETRY_STATUSES:
                        last_error = f"HTTP {resp.status_code}"
                        delay = max(delay, _retry_after(resp))
                        await self.sleep(delay)
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}")
                    body = resp.content
                    content_type = resp.headers.get("content-type", "text/html").lower()
                    final_url = str(resp.url)
                if not _same_target(url, final_url):
                    raise FetchError(f"redirected to {final_url}; not the allowlisted URL")
                return body, content_type
            except FetchError:
                raise
            except Exception as exc:  # network errors, browser timeouts
                last_error = f"{type(exc).__name__}: {exc}"
                await self.sleep(delay)
        raise FetchError(f"gave up after {self.max_retries} attempts ({last_error})")

    async def _polite_delay(self) -> None:
        if self._requests_made:
            await self.sleep(self.rate_limit)
        self._requests_made += 1

    def _save(self, sid: str, url: str, body: bytes, content_type: str) -> FetchResult:
        fetched_at = self.now()
        ext = "pdf" if "pdf" in content_type else "html"
        folder = self.raw_dir / sid
        folder.mkdir(parents=True, exist_ok=True)
        stem = fetched_at.astimezone(IST).strftime("%Y-%m-%d")
        path = folder / f"{stem}.{ext}"
        path.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        meta = {
            "url": url,
            "fetched_at": fetched_at.isoformat(),
            "sha256": sha,
            "content_type": content_type,
        }
        (folder / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return FetchResult(sid, "ok", path, sha, content_type, fetched_at)


DEFAULT_MAX_PAGES = 10


def _item_key(el) -> str:
    return " ".join(el.get_text(" ").split()).casefold()


def _items_container(doc: BeautifulSoup, selector: str, sid: str, page_no: int):
    """Smallest element containing every listed item on the page."""
    items = doc.select(selector)
    if not items:
        raise FetchError(f"{sid}: page {page_no} has no items matching {selector!r}")
    node = items[0].parent
    # Tags compare by content in bs4, so ancestry must be checked by identity.
    while node is not None and not all(any(p is node for p in i.parents) for i in items):
        node = node.parent
    if node is None:
        raise FetchError(f"{sid}: page {page_no} items have no common container")
    return node


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("retry-after", 0)), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return 0


def load_from_disk(source: dict, raw_dir: Path) -> FetchResult:
    """Use the newest saved snapshot for a source (--from-disk mode, edge cases FET-12/FET-13).

    The date comes from the .meta.json sidecar when present; otherwise from the file's
    modification time, which is when it was saved by hand, not when ingestion ran.
    """
    sid = source["id"]
    folder = raw_dir / sid
    snapshots = (
        sorted(p for p in folder.glob("*") if p.suffix in (".html", ".htm", ".pdf") and p.is_file())
        if folder.is_dir()
        else []
    )
    if not snapshots:
        return FetchResult(sid, "failed", error=f"no saved snapshot in {folder}")
    path = snapshots[-1]
    body = path.read_bytes()
    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
    else:
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    content_type = "application/pdf" if path.suffix == ".pdf" else "text/html"
    try:
        check_content(source, body, content_type)
    except FetchError as exc:
        return FetchResult(sid, "failed", error=f"saved snapshot rejected: {exc}")
    return FetchResult(
        sid, "from_disk", path, hashlib.sha256(body).hexdigest(), content_type, fetched_at
    )
