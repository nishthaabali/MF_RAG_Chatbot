"""Corpus data bundles: what the daily GitHub Actions run publishes and the server pulls.

    python -m ingest.bundle pack --out dist/ [--report ingest_report.json] [--gate gate.json]
    python -m ingest.bundle unpack dist/data-bundle.tar.gz --dest ../data   # CI: restore
    python -m ingest.bundle install dist/data-bundle.tar.gz                 # server: by hand
    python -m ingest.bundle verify dist/data-bundle.tar.gz

A bundle is `data-bundle.tar.gz` with one corpus: `corpus.db`, `chroma/`, `bm25.pkl`,
`index_manifest.json`, `parsed/`, `raw/` (latest snapshot per source only) and `bundle.json`.
`bundle.json` describes it and lists the SHA-256 of every file. A copy of it, with the
archive's own SHA-256 added, is published next to the archive, so the server can tell whether
a bundle is new before downloading it.

Conversations never get in: threads live in data/app.db, which is never packed, and packing
refuses a corpus database that contains `threads` or `messages` tables.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import DATA_DIR, ROOT_DIR, Settings, get_settings
from app.corpus import (
    CORPUS_DB_NAME,
    bundles_dir,
    set_current,
    sqlite_conn,
    sqlite_path,
)
from ingest.parsers.groww import KEY_FIELDS

log = logging.getLogger(__name__)

BUNDLE_FORMAT = 1
ARCHIVE_NAME = "data-bundle.tar.gz"
META_NAME = "bundle.json"
# Top-level entries of a bundle, in the order they are put in place (manifest last: it marks
# a finished corpus for the server's reload check).
ITEMS = ("raw", "parsed", CORPUS_DB_NAME, "chroma", "bm25.pkl", "index_manifest.json")
FORBIDDEN_TABLES = {"threads", "messages"}
SNAPSHOT_SUFFIXES = (".html", ".pdf", ".json")


class BundleError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _git_commit() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT_DIR, capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------------------ pack
def latest_snapshots(raw_dir: Path) -> list[Path]:
    """Per source folder, the files of the newest snapshot (`<date>.html` + `<date>.meta.json`)."""
    out: list[Path] = []
    if not raw_dir.is_dir():
        return out
    for folder in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
        stems = sorted({f.name.split(".", 1)[0] for f in folder.iterdir()
                        if f.is_file() and not f.name.endswith(".meta.json")
                        and f.suffix in SNAPSHOT_SUFFIXES})
        if stems:
            out += sorted(f for f in folder.iterdir()
                          if f.is_file() and f.name.split(".", 1)[0] == stems[-1])
    return out


def table_names(db: Path) -> set[str]:
    with sqlite_conn(db) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def copy_corpus_db(src: Path, dst: Path) -> None:
    """A consistent copy (SQLite backup API: safe under WAL), made portable."""
    if not src.is_file():
        raise BundleError(f"no corpus database at {src}")
    with sqlite_conn(src) as s, sqlite_conn(dst) as d:
        s.backup(d)
    forbidden = table_names(dst) & FORBIDDEN_TABLES
    if forbidden:
        dst.unlink()
        raise BundleError(f"corpus database contains {sorted(forbidden)}: refusing to pack "
                          "conversations (is corpus_database_url pointing at app.db?)")
    conn = sqlite3.connect(dst)
    try:
        with conn:  # local absolute paths mean nothing on another machine
            for sid, path in conn.execute("SELECT id, snapshot_path FROM sources").fetchall():
                if path:
                    conn.execute("UPDATE sources SET snapshot_path = ? WHERE id = ?",
                                 (f"raw/{sid}/{Path(path.replace(chr(92), '/')).name}", sid))
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()


def describe(corpus_dir: Path, db: Path) -> dict:
    """The facts about a corpus the quality gate compares between runs."""
    manifest = json.loads((corpus_dir / "index_manifest.json").read_text(encoding="utf-8"))
    with sqlite_conn(db) as conn:
        conn.row_factory = sqlite3.Row
        sources = {r["id"]: {"fetched_at": r["fetched_at"], "sha256": r["sha256"],
                             "last_status": r["last_status"]}
                   for r in conn.execute("SELECT id, fetched_at, sha256, last_status "
                                         "FROM sources ORDER BY id")}
        cols = ", ".join(KEY_FIELDS)
        nulls = {}
        for r in conn.execute(f"SELECT scheme_id, {cols} FROM scheme_facts ORDER BY scheme_id"):
            missing = [f for f in KEY_FIELDS if r[f] in (None, "")]
            nulls[r["scheme_id"]] = missing
    return {
        "chunk_count": manifest.get("chunk_count"),
        "index_built_at": manifest.get("built_at"),
        "index_settings": manifest.get("settings", {}),
        "schemes": len(nulls),
        "core_nulls": nulls,
        "sources": sources,
    }


def pack(out_dir: Path, *, settings: Settings | None = None, data_dir: Path = DATA_DIR,
         extra: dict | None = None, now: datetime | None = None) -> tuple[Path, dict]:
    """Write `<out_dir>/data-bundle.tar.gz` and `<out_dir>/bundle.json`; returns both."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    db = sqlite_path(settings.corpus_database_url)
    if db is None:
        raise BundleError("corpus_database_url must be a SQLite file")
    for item in ("chroma", "bm25.pkl", "index_manifest.json", "parsed"):
        if not (data_dir / item).exists():
            raise BundleError(f"{item} missing in {data_dir}: run python -m ingest first")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".pack-") as tmp:
        stage = Path(tmp)
        copy_corpus_db(db, stage / CORPUS_DB_NAME)
        shutil.copytree(data_dir / "parsed", stage / "parsed")
        shutil.copytree(data_dir / "chroma", stage / "chroma")
        for name in ("bm25.pkl", "index_manifest.json"):
            shutil.copy2(data_dir / name, stage / name)
        for f in latest_snapshots(data_dir / "raw"):
            target = stage / "raw" / f.parent.name / f.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)

        files = {p.relative_to(stage).as_posix(): sha256_file(p)
                 for p in sorted(stage.rglob("*")) if p.is_file()}
        meta = {
            "format": BUNDLE_FORMAT,
            "id": now.astimezone(ZoneInfo(settings.ops.timezone)).strftime("%Y-%m-%d-%H%M%S"),
            "created_at": now.isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "chunker_version": settings.chunking.version,
            "embedding_model": settings.retrieval.embedding_model,
            **describe(stage, stage / CORPUS_DB_NAME),
            **(extra or {}),
            "files": files,
        }
        (stage / META_NAME).write_text(json.dumps(meta, indent=1), encoding="utf-8")

        archive = out_dir / ARCHIVE_NAME
        part = archive.with_suffix(".part")
        with tarfile.open(part, "w:gz") as tar:
            for entry in sorted(stage.iterdir()):
                tar.add(entry, arcname=entry.name)
        os.replace(part, archive)
    digest = sha256_file(archive)
    published = {**meta, "archive_sha256": digest}
    published.pop("files")  # the archive's own copy lists the files
    (out_dir / META_NAME).write_text(json.dumps(published, indent=1), encoding="utf-8")
    (out_dir / f"{ARCHIVE_NAME}.sha256").write_text(f"{digest}  {ARCHIVE_NAME}\n",
                                                   encoding="utf-8")
    return archive, published


# ------------------------------------------------------------------------------ unpack
def extract_verified(archive: Path, target: Path, expected_sha256: str | None = None) -> dict:
    """Extract into an empty `target` and check every file against bundle.json."""
    if expected_sha256 and sha256_file(archive) != expected_sha256.lower():
        raise BundleError("archive checksum does not match")
    target.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for m in tar.getmembers():
                top = m.name.split("/", 1)[0]
                if top not in (*ITEMS, META_NAME):
                    raise BundleError(f"unexpected entry {m.name!r} in bundle")
                if not (m.isfile() or m.isdir()):
                    raise BundleError(f"unsupported entry type for {m.name!r}")
            tar.extractall(target, filter="data")
        meta_path = target / META_NAME
        if not meta_path.is_file():
            raise BundleError("bundle.json missing")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("format") != BUNDLE_FORMAT:
            raise BundleError(f"unsupported bundle format {meta.get('format')!r}")
        listed = meta.get("files") or {}
        present = {p.relative_to(target).as_posix() for p in target.rglob("*")
                   if p.is_file() and p != meta_path}
        if present != set(listed):
            raise BundleError(f"file list mismatch ({len(present ^ set(listed))} differ)")
        for rel, digest in listed.items():
            if sha256_file(target / rel) != digest:
                raise BundleError(f"checksum mismatch: {rel}")
        if table_names(target / CORPUS_DB_NAME) & FORBIDDEN_TABLES:
            raise BundleError("bundle contains conversation tables")
        return meta
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def unpack(archive: Path, dest: Path, *, settings: Settings | None = None,
           expected_sha256: str | None = None) -> dict:
    """Put a bundle's corpus into `dest` (a data/ folder nothing is running from: the CI
    runner restoring yesterday's state). The corpus database goes to corpus_database_url when
    `dest` is data/, else to dest/corpus.db. A bad bundle leaves `dest` untouched."""
    settings = settings or get_settings()
    dest.mkdir(parents=True, exist_ok=True)
    stage = dest / f".staging-{uuid.uuid4().hex[:8]}"
    meta = extract_verified(archive, stage, expected_sha256)
    db_target = (sqlite_path(settings.corpus_database_url)
                 if dest.resolve() == DATA_DIR.resolve() else dest / CORPUS_DB_NAME)
    try:
        for item in ITEMS:
            src = stage / item
            target = db_target if item == CORPUS_DB_NAME else dest / item
            old = target.with_name(target.name + ".old")
            if old.exists():
                _remove(old)
            if target.exists():
                os.replace(target, old)
            for suffix in ("-wal", "-shm"):  # stale WAL files would corrupt the new database
                side = target.with_name(target.name + suffix)
                if item == CORPUS_DB_NAME and side.exists():
                    side.unlink()
            os.replace(src, target)
            if old.exists():
                _remove(old)
        shutil.copy2(stage / META_NAME, dest / META_NAME)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return meta


def install(archive: Path, data_dir: Path = DATA_DIR, expected_sha256: str | None = None,
            ) -> tuple[Path, dict]:
    """Server side: extract into data/bundles/<id>/ (CURRENT is not changed here: the caller
    switches it after loading and checking the corpus)."""
    root = bundles_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    stage = root / f".staging-{uuid.uuid4().hex[:8]}"
    meta = extract_verified(archive, stage, expected_sha256)
    name = f"{meta['id']}-{(expected_sha256 or sha256_file(archive))[:8]}"
    target = root / name
    if target.exists():
        shutil.rmtree(stage, ignore_errors=True)
        return target, meta
    os.replace(stage, target)
    return target, meta


def prune(data_dir: Path = DATA_DIR, keep: int = 2) -> list[str]:
    """Delete older installed bundles, keeping the newest `keep` (always the active one).
    Folders a running server still has open can't be deleted on Windows: they are skipped
    and tried again next time."""
    root = bundles_dir(data_dir)
    if not root.is_dir():
        return []
    try:
        active = (root / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        active = ""
    dirs = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.name, reverse=True)
    removed = []
    for d in dirs[keep:]:
        if d.name == active:
            continue
        try:
            shutil.rmtree(d)
            removed.append(d.name)
        except OSError:
            log.info("bundle folder still in use; will retry", extra={"bundle": d.name})
    for d in root.glob(".staging-*"):
        shutil.rmtree(d, ignore_errors=True)
    return removed


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


# ------------------------------------------------------------------------------ CLI
def _sha_arg(value: str | None) -> str | None:
    if value and Path(value).is_file():
        return Path(value).read_text(encoding="utf-8").split()[0]
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.bundle", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="bundle the corpus in data/")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--report", type=Path, help="ingest_report.json to embed a summary of")
    p.add_argument("--gate", type=Path, help="gate_report.json to embed the verdict of")
    for name, help_ in (("unpack", "restore a bundle into a data/ folder"),
                        ("install", "install a bundle into data/bundles/ and activate it"),
                        ("verify", "check a bundle without installing it")):
        q = sub.add_parser(name, help=help_)
        q.add_argument("archive", type=Path)
        q.add_argument("--sha256", help="expected archive SHA-256, or a .sha256 file")
        if name == "unpack":
            q.add_argument("--dest", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.cmd == "pack":
            extra = {}
            if args.report and args.report.is_file():
                r = json.loads(args.report.read_text(encoding="utf-8"))
                ing = r.get("ingest") or {}
                extra["ingest"] = {k: ing.get(k) for k in
                                   ("processed", "fetched", "unchanged", "succeeded")}
                extra["ingest"]["failures"] = sorted(ing.get("failures") or {})
            if args.gate and args.gate.is_file():
                extra["gate"] = json.loads(args.gate.read_text(encoding="utf-8")).get("passed")
            archive, meta = pack(args.out, extra=extra)
            size = archive.stat().st_size / 1e6
            print(f"packed {archive} ({size:.1f} MB): bundle {meta['id']}, "
                  f"{meta['chunk_count']} chunks, {len(meta['sources'])} sources")
        elif args.cmd == "unpack":
            meta = unpack(args.archive, args.dest, expected_sha256=_sha_arg(args.sha256))
            print(f"restored bundle {meta['id']} into {args.dest}")
        elif args.cmd == "verify":
            with tempfile.TemporaryDirectory() as tmp:
                meta = extract_verified(args.archive, Path(tmp) / "b", _sha_arg(args.sha256))
            print(f"ok: bundle {meta['id']}, {len(meta['files'])} files")
        else:
            target, meta = install(args.archive, expected_sha256=_sha_arg(args.sha256))
            set_current(DATA_DIR, target.name)
            print(f"installed bundle {meta['id']} at {target}; a running server switches to "
                  "it within ops.reload_check_seconds after checking it")
    except BundleError as exc:
        print(f"bundle error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
