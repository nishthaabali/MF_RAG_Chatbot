"""Background jobs run inside the API process (Phases 10 and 11).

- **Corpus hot-reload:** every `ops.reload_check_seconds`, look at the active corpus's
  `index_manifest.json` (data/, or the bundle data/bundles/CURRENT names). `python -m ingest`
  writes it last, after facts, Chroma and BM25, so a changed manifest means a finished index
  run. The server then builds a new pipeline from disk, checks it (`verify_corpus`) and swaps
  it in. A request in flight keeps the pipeline it started with, so it is answered entirely
  from the old corpus or entirely from the new one. Footer dates come from the reloaded
  catalog, so new answers show the new fetch dates.
- **Bundle pull (Phase 11):** when `data_refresh.release_repo` is set, check the `data-latest`
  GitHub Release daily (and at startup). A newer bundle is downloaded, checksum-verified,
  unpacked into its own folder, loaded and checked; only then does CURRENT point at it.
- **Retention:** delete threads idle longer than `threads.retention_days` (daily, and once at
  startup).
- **Daily summary:** one log line with counts by route, validator failure rate and
  no-evidence rate since the previous summary.

The daily ingestion itself runs in GitHub Actions (.github/workflows/daily-ingest.yml).
"""

import asyncio
import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.api.deps import Services
from app.config import DATA_DIR
from app.corpus import MANIFEST_NAME, active_corpus_dir, bundles_dir, set_current

log = logging.getLogger(__name__)

BUNDLE_META = "bundle.json"
GITHUB_API = "https://api.github.com"


def today_in(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def corpus_signature(corpus_dir: Path = DATA_DIR) -> str | None:
    """Identifies one finished index build; None when there is no readable manifest."""
    try:
        manifest = json.loads((corpus_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    built = manifest.get("built_at")
    return f"{built}|{manifest.get('chunk_count')}" if built else None


def read_bundle_meta(corpus_dir: Path) -> dict | None:
    try:
        return json.loads((corpus_dir / BUNDLE_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------ GitHub release
def github_client(settings, transport=None):
    """An httpx client for the GitHub API (with the read-only token, if one is set)."""
    import httpx

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "mf-faq-data-refresh"}
    token = settings.data_refresh_token.get_secret_value()
    if token:  # httpx drops it on the redirect to the asset storage host
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(transport=transport, headers=headers, follow_redirects=True,
                             timeout=settings.data_refresh.timeout_seconds)


async def fetch_release(client, cfg) -> tuple[dict, dict]:
    """The release's bundle.json (remote metadata) and its assets by name."""
    r = await client.get(f"{GITHUB_API}/repos/{cfg.release_repo}/releases/tags/"
                         f"{cfg.release_tag}")
    r.raise_for_status()
    assets = {a["name"]: a for a in r.json().get("assets", [])}
    if cfg.manifest_asset not in assets or cfg.bundle_asset not in assets:
        raise RuntimeError(f"release {cfg.release_tag} lacks its bundle assets")
    r = await client.get(assets[cfg.manifest_asset]["url"],
                         headers={"Accept": "application/octet-stream"})
    r.raise_for_status()
    return json.loads(r.content), assets


async def download_bundle(client, cfg, assets: dict, data_dir: Path) -> Path:
    """Stream the bundle archive into data/bundles/.download/ and return its path."""
    download = bundles_dir(data_dir) / ".download"
    download.mkdir(parents=True, exist_ok=True)
    archive = download / cfg.bundle_asset
    async with client.stream("GET", assets[cfg.bundle_asset]["url"],
                             headers={"Accept": "application/octet-stream"}) as resp:
        resp.raise_for_status()
        with archive.open("wb") as f:
            async for block in resp.aiter_bytes(1 << 20):
                f.write(block)
    return archive


def write_bundle_meta(target: Path, meta: dict, remote: dict) -> None:
    """Record what was installed (read back by the puller and /api/health)."""
    (target / BUNDLE_META).write_text(json.dumps({**meta, **remote}, indent=1), encoding="utf-8")


class CorpusReloader:
    def __init__(self, services: Services, data_dir: Path = DATA_DIR,
                 build: Callable[[Path], object] | None = None):
        self.services = services
        self.data_dir = data_dir
        self._build = build or self._default_build
        self.signature = self._signature(active_corpus_dir(data_dir))  # what is loaded now
        self._lock = asyncio.Lock()
        self.reloads = 0

    @staticmethod
    def _signature(corpus_dir: Path) -> str | None:
        sig = corpus_signature(corpus_dir)
        return f"{corpus_dir.name}|{sig}" if sig else None

    def _default_build(self, corpus_dir: Path):
        pipeline = self.services.pipeline
        if not hasattr(pipeline, "with_fresh_corpus"):
            raise RuntimeError("this pipeline can't be reloaded")
        return pipeline.with_fresh_corpus(self.services.settings, corpus_dir)

    async def check(self) -> bool:
        """Reload if a new index build has finished since the last (attempted) load."""
        corpus_dir = active_corpus_dir(self.data_dir)
        signature = self._signature(corpus_dir)
        if signature is None or signature == self.signature:
            return False
        return await self.switch_to(corpus_dir)

    async def switch_to(self, corpus_dir: Path, commit: Callable[[], None] | None = None) -> bool:
        """Load and check `corpus_dir`; if it is good, run `commit` (e.g. point CURRENT at it)
        and swap it in. On failure nothing changes."""
        async with self._lock:
            signature = self._signature(corpus_dir)
            try:
                pipeline = await asyncio.to_thread(self._build, corpus_dir)
                if commit is not None:
                    commit()
            except Exception as exc:
                # Remember the signature anyway: retrying a broken build every minute would
                # reload the same files again. The next finished ingest run changes it.
                if commit is None:
                    self.signature = signature
                log.warning("corpus reload failed; still serving the loaded corpus",
                            extra={"exc_type": type(exc).__name__, "detail": str(exc)[:200]})
                return False
            # Two plain assignments with no await between them: no request sees half of it.
            self.services.pipeline = pipeline
            self.services.catalog = pipeline.catalog
            self.signature = signature
            self.reloads += 1
            dates = [d for d in pipeline.catalog.fetched_at.values() if d is not None]
            log.info("corpus reloaded", extra={
                "corpus": corpus_dir.name,
                "signature": signature,
                "sources": len(pipeline.catalog.source_urls),
                "last_fetch": max(dates).isoformat() if dates else None,
            })
            return True


class BundlePuller:
    """Installs the newest bundle from the `data-latest` GitHub Release (Phase 11)."""

    def __init__(self, services: Services, reloader: CorpusReloader, data_dir: Path = DATA_DIR,
                 transport=None):
        self.services = services
        self.reloader = reloader
        self.data_dir = data_dir
        self.transport = transport  # tests: httpx.MockTransport
        self._lock = asyncio.Lock()

    def _client(self):
        return github_client(self.services.settings, self.transport)

    async def check(self) -> str:
        """disabled | up_to_date | installed | failed"""
        cfg = self.services.settings.data_refresh
        if not cfg.release_repo:
            return "disabled"
        async with self._lock:
            try:
                return await self._check(cfg)
            except Exception as exc:
                log.warning("bundle pull failed; still serving the loaded corpus",
                            extra={"exc_type": type(exc).__name__, "detail": str(exc)[:200]})
                return "failed"

    async def _check(self, cfg) -> str:
        from ingest.bundle import install, prune

        async with self._client() as client:
            remote, assets = await fetch_release(client, cfg)
            local = read_bundle_meta(active_corpus_dir(self.data_dir)) or {}
            if remote.get("archive_sha256") == local.get("archive_sha256") or (
                    local.get("created_at") and remote["created_at"] <= local["created_at"]):
                log.info("bundle up to date", extra={"bundle": local.get("id")})
                return "up_to_date"
            archive = await download_bundle(client, cfg, assets, self.data_dir)

        target, meta = await asyncio.to_thread(install, archive, self.data_dir,
                                               remote["archive_sha256"])
        archive.unlink(missing_ok=True)
        write_bundle_meta(target, meta, remote)
        ok = await self.reloader.switch_to(
            target, commit=lambda: set_current(self.data_dir, target.name))
        if not ok:
            shutil.rmtree(target, ignore_errors=True)
            return "failed"
        removed = await asyncio.to_thread(prune, self.data_dir, cfg.keep_bundles)
        log.info("bundle installed", extra={"bundle": meta["id"], "pruned": removed})
        return "installed"


async def run_retention(services: Services, now: datetime | None = None) -> int:
    days = services.settings.threads.retention_days
    before = (now or datetime.now(UTC)).replace(tzinfo=None) - timedelta(days=days)
    deleted = await asyncio.to_thread(services.store.delete_idle, before)
    log.info("retention", extra={"threads_deleted": deleted, "retention_days": days})
    return deleted


def log_daily_summary(services: Services) -> dict:
    summary = services.stats.flush()
    log.info("daily summary", extra={"summary": summary})
    return summary


def start_jobs(services: Services, reloader: CorpusReloader | None = None,
               puller: BundlePuller | None = None):
    """Start the scheduler on the running event loop; the caller shuts it down."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    ops = services.settings.ops
    tz = ZoneInfo(ops.timezone)
    now = datetime.now(tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(run_retention, CronTrigger.from_crontab(ops.retention_cron, timezone=tz),
                      args=[services], id="retention", next_run_time=now,
                      coalesce=True, max_instances=1)
    scheduler.add_job(log_daily_summary, CronTrigger.from_crontab(ops.summary_cron, timezone=tz),
                      args=[services], id="daily_summary", coalesce=True, max_instances=1)
    if reloader is not None and ops.reload_check_seconds > 0:
        scheduler.add_job(reloader.check, "interval", seconds=ops.reload_check_seconds,
                          id="corpus_reload", coalesce=True, max_instances=1)
    refresh = services.settings.data_refresh
    if puller is not None and refresh.release_repo:
        scheduler.add_job(puller.check, CronTrigger.from_crontab(refresh.pull_cron, timezone=tz),
                          id="bundle_pull", next_run_time=now, coalesce=True, max_instances=1)
    scheduler.start()
    return scheduler
