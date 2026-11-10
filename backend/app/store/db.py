"""Engine and session setup. SQLite runs in WAL mode so concurrent reads don't block writes."""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.store.models import Base


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


def init_db(engine: Engine, tables=None) -> None:
    """Create missing tables: `CORPUS_TABLES` or `THREAD_TABLES` (models.py), default all.

    Tables that already exist get any nullable columns added since they were created (e.g.
    `messages.comparison`), so an existing threads database keeps working after an upgrade.
    """
    chosen = list(tables) if tables is not None else list(Base.metadata.sorted_tables)
    Base.metadata.create_all(engine, tables=chosen)
    _add_missing_columns(engine, chosen)


def _add_missing_columns(engine: Engine, tables) -> None:
    inspector = inspect(engine)
    for table in tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing or not column.nullable or column.primary_key:
                continue
            kind = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}'))


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
