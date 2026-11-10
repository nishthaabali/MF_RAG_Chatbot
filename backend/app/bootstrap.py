"""Install the first data bundle on an empty server volume (deployment plan §4.4).

    cd backend && python -m app.bootstrap && uvicorn app.main:app ...

Runs once before the API starts. When the active corpus has no `index_manifest.json` (a new
Railway volume) and `data_refresh.release_repo` is set, it downloads the `data-latest` release,
verifies the archive checksum and every file, checks that the corpus loads, and only then
points data/bundles/CURRENT at it.

It always exits 0, with one log line saying what happened: a GitHub outage or a bad bundle must
not stop the API from starting. The server then runs in degraded mode and its bundle puller
and reload check pick the corpus up later.
"""

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from app.config import DATA_DIR, Settings, get_settings
from app.corpus import MANIFEST_NAME, active_corpus_dir, set_current
from app.jobs import download_bundle, fetch_release, github_client, write_bundle_meta

log = logging.getLogger(__name__)


def verify_loads(settings: Settings) -> Callable[[Path], None]:
    """Load the corpus the way the server will, with all its checks (verify_corpus)."""

    def check(corpus_dir: Path) -> None:
        from app.pipeline.retriever import Retriever

        retriever = Retriever.from_settings(settings, data_dir=corpus_dir, verify=True)
        retriever.store.engine.dispose()

    return check


async def bootstrap(settings: Settings, data_dir: Path = DATA_DIR, *, transport=None,
                    verify: Callable[[Path], None] | None = None) -> str:
    """present | disabled | installed | failed"""
    from ingest.bundle import install, prune

    cfg = settings.data_refresh
    corpus = active_corpus_dir(data_dir)
    if (corpus / MANIFEST_NAME).is_file():
        log.info("bootstrap: corpus present", extra={"corpus": corpus.name})
        return "present"
    if not cfg.release_repo:
        log.warning("bootstrap: no corpus and data_refresh.release_repo is empty; "
                    "the server starts without one")
        return "disabled"
    verify = verify or verify_loads(settings)
    target = None
    try:
        async with github_client(settings, transport) as client:
            remote, assets = await fetch_release(client, cfg)
            archive = await download_bundle(client, cfg, assets, data_dir)
        target, meta = await asyncio.to_thread(install, archive, data_dir,
                                               remote["archive_sha256"])
        archive.unlink(missing_ok=True)
        write_bundle_meta(target, meta, remote)
        await asyncio.to_thread(verify, target)
        set_current(data_dir, target.name)
    except Exception as exc:
        if target is not None:
            shutil.rmtree(target, ignore_errors=True)
        log.warning("bootstrap: bundle install failed; the server starts without a corpus",
                    extra={"exc_type": type(exc).__name__, "detail": str(exc)[:200]})
        return "failed"
    removed = await asyncio.to_thread(prune, data_dir, cfg.keep_bundles)
    log.info("bootstrap: bundle installed", extra={"bundle": meta["id"], "pruned": removed})
    return "installed"


def main() -> int:
    from app.observability import configure_logging

    try:
        settings = get_settings()
        configure_logging(settings.ops.log_format, settings.ops.log_level)
        asyncio.run(bootstrap(settings))
    except Exception as exc:  # never block the API from starting
        logging.basicConfig()
        log.error("bootstrap: unexpected %s; the server starts anyway", type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
