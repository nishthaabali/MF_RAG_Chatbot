"""Where the ingested corpus lives (Phase 11).

A corpus is one folder: `corpus.db` (sources, scheme facts, metrics, ...), `chroma/`,
`bm25.pkl`, `index_manifest.json`, `parsed/` and `raw/`. Two places hold one:

- `data/` itself: what `python -m ingest` writes on this machine (development, and the
  GitHub Actions runner). Its database is `settings.corpus_database_url`.
- `data/bundles/<bundle id>/`: a bundle the server pulled from the daily GitHub Release.
  `data/bundles/CURRENT` names the active one. A new bundle is unpacked into its own folder
  and the pointer is switched only after the server has loaded and checked it, so nothing
  that a running server has open is ever overwritten (on Windows, open files can't be
  replaced at all).

Threads stay in `settings.database_url` (data/app.db), which is never part of a corpus.

    python -m app.corpus                 # where the corpus is, and migrate an old app.db
    python -m app.corpus --drop-legacy   # also drop corpus tables left behind in app.db
"""

import argparse
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import DATA_DIR, Settings, get_settings

log = logging.getLogger(__name__)

BUNDLES_DIRNAME = "bundles"
CURRENT_NAME = "CURRENT"
CORPUS_DB_NAME = "corpus.db"
MANIFEST_NAME = "index_manifest.json"


def bundles_dir(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / BUNDLES_DIRNAME


def active_corpus_dir(data_dir: Path = DATA_DIR) -> Path:
    """The installed bundle CURRENT points at, else `data_dir` itself."""
    try:
        name = (bundles_dir(data_dir) / CURRENT_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return data_dir
    folder = bundles_dir(data_dir) / name
    if name and "/" not in name and "\\" not in name and (folder / MANIFEST_NAME).is_file():
        return folder
    log.warning("data/bundles/CURRENT names no usable bundle; using data/")
    return data_dir


def is_bundle_dir(corpus_dir: Path) -> bool:
    return corpus_dir.parent.name == BUNDLES_DIRNAME


def corpus_url(settings: Settings, corpus_dir: Path) -> str:
    if is_bundle_dir(corpus_dir):
        return f"sqlite:///{(corpus_dir / CORPUS_DB_NAME).as_posix()}"
    return settings.corpus_database_url


def set_current(data_dir: Path, bundle_name: str) -> None:
    """Point CURRENT at an installed bundle (atomic replace)."""
    pointer = bundles_dir(data_dir) / CURRENT_NAME
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(bundle_name + "\n", encoding="utf-8")
    os.replace(tmp, pointer)


def sqlite_path(url: str) -> Path | None:
    u = make_url(url)
    if u.get_backend_name() != "sqlite" or u.database in (None, "", ":memory:"):
        return None
    return Path(u.database)


@contextmanager
def sqlite_conn(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that is closed afterwards (`with sqlite_conn()` only commits, and an
    open connection keeps the file locked on Windows)."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master "
                                       "WHERE type = 'table'")}


def ensure_corpus_db(settings: Settings) -> bool:
    """One-time migration: copy corpus tables out of an app.db written before the split.

    Runs only when corpus.db has no `sources` rows yet and app.db has some. The old tables
    stay in app.db until `--drop-legacy`; nothing reads them any more. Returns True if it
    copied anything.
    """
    from app.store.db import init_db, make_engine
    from app.store.models import CORPUS_TABLES

    corpus, legacy = sqlite_path(settings.corpus_database_url), sqlite_path(settings.database_url)
    if corpus is None or legacy is None or corpus == legacy or not legacy.is_file():
        return False
    if corpus.is_file():
        with sqlite_conn(corpus) as conn:
            if "sources" in _tables(conn) and conn.execute(
                    "SELECT 1 FROM sources LIMIT 1").fetchone():
                return False
    with sqlite_conn(legacy) as conn:
        if "sources" not in _tables(conn) or not conn.execute(
                "SELECT 1 FROM sources LIMIT 1").fetchone():
            return False

    engine = make_engine(settings.corpus_database_url)
    init_db(engine, CORPUS_TABLES)
    engine.dispose()
    conn = sqlite3.connect(corpus)
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
        have = _tables(conn, "legacy")
        copied = {}
        with conn:
            for table in CORPUS_TABLES:
                if table.name not in have:
                    continue
                cols = ", ".join(f'"{c.name}"' for c in table.columns)
                conn.execute(f'DELETE FROM main."{table.name}"')
                conn.execute(f'INSERT INTO main."{table.name}" ({cols}) '
                             f'SELECT {cols} FROM legacy."{table.name}"')
                copied[table.name] = conn.execute(
                    f'SELECT COUNT(*) FROM main."{table.name}"').fetchone()[0]
        conn.execute("DETACH DATABASE legacy")
    finally:
        conn.close()
    log.info("copied corpus tables from app.db into corpus.db", extra={"rows": copied})
    return True


def drop_legacy_tables(settings: Settings) -> list[str]:
    """Remove corpus tables from app.db (after `ensure_corpus_db` has copied them)."""
    from app.store.models import CORPUS_TABLES

    legacy = sqlite_path(settings.database_url)
    if legacy is None or not legacy.is_file():
        return []
    with sqlite_conn(legacy) as conn:
        present = _tables(conn)
        dropped = [t.name for t in reversed(CORPUS_TABLES) if t.name in present]
        for name in dropped:
            conn.execute(f'DROP TABLE "{name}"')
    return dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.corpus", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drop-legacy", action="store_true",
                    help="drop corpus tables left in app.db after the migration")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    migrated = ensure_corpus_db(settings)
    print(f"active corpus : {active_corpus_dir()}")
    print(f"corpus db     : {corpus_url(settings, active_corpus_dir())}")
    print(f"threads db    : {settings.database_url}")
    print(f"migrated now  : {migrated}")
    if args.drop_legacy:
        corpus = sqlite_path(settings.corpus_database_url)
        if corpus is None or not corpus.is_file():
            print("corpus.db doesn't exist yet: not dropping anything")
            return 1
        print(f"dropped from app.db: {', '.join(drop_legacy_tables(settings)) or 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
