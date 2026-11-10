"""Phase 11: data bundles. Pack -> verify -> unpack/install round trips, and every way a bad
bundle is refused without touching the corpus already in place."""

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.corpus import active_corpus_dir, set_current, sqlite_conn
from ingest.bundle import (
    ARCHIVE_NAME,
    BundleError,
    extract_verified,
    install,
    latest_snapshots,
    pack,
    prune,
    sha256_file,
    unpack,
)
from ingest.indexer import BM25Index
from tests.corpus_fixture import corpus_settings, make_corpus

NOW = datetime(2026, 11, 10, 4, 45, tzinfo=UTC)  # 10:15 IST


@pytest.fixture
def built(tmp_path):
    data = tmp_path / "data"
    settings = corpus_settings(tmp_path, data)
    make_corpus(data, settings)
    archive, meta = pack(tmp_path / "dist", settings=settings, data_dir=data, now=NOW)
    return tmp_path, data, settings, archive, meta


def names(archive: Path) -> set[str]:
    with tarfile.open(archive) as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


def facts(db: Path) -> list[tuple]:
    with sqlite_conn(db) as conn:
        return conn.execute("SELECT scheme_id, expense_ratio, exit_load FROM scheme_facts "
                            "ORDER BY scheme_id").fetchall()


# ------------------------------------------------------------------------------ pack
def test_pack_contents_and_metadata(built):
    tmp_path, data, _, archive, meta = built
    files = names(archive)
    assert {"corpus.db", "bm25.pkl", "index_manifest.json", "bundle.json"} <= files
    assert any(f.startswith("chroma/") for f in files)
    assert "parsed/groww-kotak-multi-cap.json" in files
    # Only the newest snapshot per source.
    raw = sorted(f for f in files if f.startswith("raw/"))
    assert raw == ["raw/groww-kotak-multi-cap/2026-11-09.html",
                   "raw/groww-kotak-multi-cap/2026-11-09.meta.json"]
    assert not any("app.db" in f or "threads" in f for f in files)

    assert meta["id"] == "2026-11-10-101500" and meta["chunk_count"] == 4
    assert meta["core_nulls"] == {"hdfc-flexi-cap": ["expense_ratio"], "kotak-multi-cap": []}
    assert meta["sources"]["groww-kotak-multi-cap"]["sha256"] == "a" * 64
    assert meta["archive_sha256"] == sha256_file(archive)
    published = json.loads((tmp_path / "dist" / "bundle.json").read_text(encoding="utf-8"))
    assert published == meta and "files" not in published
    sha_line = (tmp_path / "dist" / f"{ARCHIVE_NAME}.sha256").read_text(encoding="utf-8")
    assert sha_line.split() == [meta["archive_sha256"], ARCHIVE_NAME]


def test_packed_database_is_portable(built, tmp_path):
    _, _, _, archive, _ = built
    out = tmp_path / "x"
    extract_verified(archive, out)
    with sqlite_conn(out / "corpus.db") as conn:
        path = conn.execute("SELECT snapshot_path FROM sources").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert path == "raw/groww-kotak-multi-cap/2026-11-09.html"  # no local absolute path
    assert mode == "delete"  # a single file, no -wal companion needed


def test_pack_refuses_a_database_with_conversations(tmp_path):
    data = tmp_path / "data"
    settings = corpus_settings(tmp_path, data)
    make_corpus(data, settings)
    with sqlite_conn(data / "corpus.db") as conn:
        conn.execute("CREATE TABLE messages (id TEXT, content TEXT)")
    with pytest.raises(BundleError, match="conversations"):
        pack(tmp_path / "dist", settings=settings, data_dir=data)
    assert not (tmp_path / "dist" / ARCHIVE_NAME).exists()


def test_pack_needs_a_built_index(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(BundleError, match="run python -m ingest"):
        pack(tmp_path / "dist", settings=corpus_settings(tmp_path, data), data_dir=data)


def test_latest_snapshots_picks_newest_per_source(tmp_path):
    for sid, days in (("a", ["2026-11-01", "2026-11-09"]), ("b", ["2026-10-30"])):
        for d in days:
            (tmp_path / sid).mkdir(exist_ok=True)
            (tmp_path / sid / f"{d}.pdf").write_bytes(b"x")
            (tmp_path / sid / f"{d}.meta.json").write_text("{}")
    got = [p.relative_to(tmp_path).as_posix() for p in latest_snapshots(tmp_path)]
    assert got == ["a/2026-11-09.meta.json", "a/2026-11-09.pdf",
                   "b/2026-10-30.meta.json", "b/2026-10-30.pdf"]


# ------------------------------------------------------------------------------ unpack
def test_unpack_round_trip(built, tmp_path):
    _, data, _, archive, meta = built
    dest = tmp_path / "restored"
    got = unpack(archive, dest, expected_sha256=meta["archive_sha256"])
    assert got["id"] == meta["id"]
    assert facts(dest / "corpus.db") == facts(data / "corpus.db")
    assert BM25Index.load(dest / "bm25.pkl").ids == BM25Index.load(data / "bm25.pkl").ids
    assert json.loads((dest / "bundle.json").read_text(encoding="utf-8"))["id"] == meta["id"]
    # Unpacking again over an existing corpus replaces it and leaves no staging behind.
    (dest / "corpus.db-wal").write_bytes(b"stale")
    unpack(archive, dest)
    assert facts(dest / "corpus.db") == facts(data / "corpus.db")
    assert not (dest / "corpus.db-wal").exists()
    assert not [p for p in dest.iterdir() if p.name.startswith(".staging") or
                p.name.endswith(".old")]


def test_checksum_mismatch_is_refused_and_dest_untouched(built, tmp_path):
    _, _, _, archive, _ = built
    dest = tmp_path / "live"
    dest.mkdir()
    (dest / "bm25.pkl").write_bytes(b"current")
    with pytest.raises(BundleError, match="archive checksum"):
        unpack(archive, dest, expected_sha256="0" * 64)
    assert (dest / "bm25.pkl").read_bytes() == b"current"
    assert sorted(p.name for p in dest.iterdir()) == ["bm25.pkl"]


def _rewrite(archive: Path, out: Path, change) -> Path:
    """Copy a bundle, letting `change(name, data)` alter or add entries."""
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            data = change(m.name, data)
            if data is None and m.isfile():
                continue
            if m.isfile():
                m.size = len(data)
                dst.addfile(m, io.BytesIO(data))
            else:
                dst.addfile(m)
        extra = change(None, None)
        if extra:
            name, data = extra
            info = tarfile.TarInfo(name)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    return out


def test_a_modified_file_is_refused(built, tmp_path):
    _, _, _, archive, _ = built
    bad = _rewrite(archive, tmp_path / "bad.tar.gz",
                   lambda n, d: d + b"x" if n == "bm25.pkl" else (None if n is None else d))
    with pytest.raises(BundleError, match="checksum mismatch: bm25.pkl"):
        unpack(bad, tmp_path / "dest")
    assert not (tmp_path / "dest" / "bm25.pkl").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "app.db", "parsed/../../evil.txt"])
def test_unexpected_or_escaping_entries_are_refused(built, tmp_path, name):
    _, _, _, archive, _ = built
    bad = _rewrite(archive, tmp_path / "bad.tar.gz",
                   lambda n, d: (name, b"x") if n is None else d)
    with pytest.raises((BundleError, tarfile.FilterError)):
        extract_verified(bad, tmp_path / "out")
    assert not (tmp_path / "out").exists() and not (tmp_path.parent / "evil.txt").exists()


# ------------------------------------------------------------------------------ install
def test_install_activate_and_prune(built, tmp_path):
    _, _, _, archive, meta = built
    data = tmp_path / "server-data"
    first, _ = install(archive, data, meta["archive_sha256"])
    assert first.parent.name == "bundles" and (first / "corpus.db").is_file()
    assert active_corpus_dir(data) == data  # nothing active until CURRENT is set
    set_current(data, first.name)
    assert active_corpus_dir(data) == first

    root = data / "bundles"
    for n in ("2026-11-01-101500-aaaaaaaa", "2026-11-02-101500-bbbbbbbb"):
        (root / n).mkdir()
    removed = prune(data, keep=2)
    assert sorted(removed) == ["2026-11-01-101500-aaaaaaaa"]
    assert first.exists() and (root / "2026-11-02-101500-bbbbbbbb").exists()
    # The active bundle is never pruned, even when it is not among the newest.
    set_current(data, "2026-11-02-101500-bbbbbbbb")
    (root / "2026-11-30-101500-cccccccc").mkdir()
    (root / "2026-12-01-101500-dddddddd").mkdir()
    prune(data, keep=2)
    assert (root / "2026-11-02-101500-bbbbbbbb").exists() and not first.exists()


def test_bad_current_pointer_falls_back_to_data(tmp_path):
    (tmp_path / "bundles").mkdir()
    set_current(tmp_path, "../../elsewhere")
    assert active_corpus_dir(tmp_path) == tmp_path
    set_current(tmp_path, "missing-bundle")
    assert active_corpus_dir(tmp_path) == tmp_path
