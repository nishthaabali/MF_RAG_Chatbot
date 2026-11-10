"""The covered schemes, AMCs and citable links, loaded once from config/ (and the sources table).

Read-only after loading, so one instance is shared by every request.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import Engine, select

from app.config import ROOT_DIR

CONFIG_DIR = ROOT_DIR / "config"
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Scheme:
    id: str
    name: str  # canonical name as shown on the Groww page, e.g. "Kotak Multi Cap Fund"
    amc: str
    category: str
    source_id: str
    url: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class AMC:
    id: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Link:
    """A citable URL plus the date its content was last fetched (None if never fetched)."""

    url: str
    last_updated: date | None


def to_ist_date(value: datetime | date | None) -> date | None:
    if value is None or not isinstance(value, datetime):
        return value
    if value.tzinfo is None:  # stored as UTC by the ingestion pipeline
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).date()


@dataclass
class Catalog:
    schemes: dict[str, Scheme]
    amcs: dict[str, AMC]
    source_urls: dict[str, str]
    source_types: dict[str, str]
    education_topics: dict[str, str]  # topic -> source id
    fetched_at: dict[str, date] = field(default_factory=dict)  # source id -> IST date
    # Fund manager name (as published) -> covered schemes they manage, in catalog order. Lets
    # "What is Rajeev Thakkar's experience?" resolve without a scheme name.
    managers: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR, engine: Engine | None = None) -> "Catalog":
        def read(name: str) -> dict:
            with (config_dir / name).open(encoding="utf-8") as f:
                return yaml.safe_load(f) or {}

        sources = read("sources.yaml").get("sources") or []
        aliases = read("aliases.yaml")
        topics = (read("education_links.yaml").get("topics")) or {}

        scheme_aliases = aliases.get("schemes") or {}
        schemes = {
            s["scheme_id"]: Scheme(
                id=s["scheme_id"],
                name=s["name"],
                amc=s["amc"],
                category=s.get("category", ""),
                source_id=s["id"],
                url=s["url"],
                aliases=tuple(scheme_aliases.get(s["scheme_id"]) or ()),
            )
            for s in sources
            if s.get("type") == "scheme_page"
        }
        amcs = {
            amc_id: AMC(amc_id, entry.get("name", amc_id), tuple(entry.get("aliases") or ()))
            for amc_id, entry in (aliases.get("amcs") or {}).items()
        }
        catalog = cls(
            schemes=schemes,
            amcs=amcs,
            source_urls={s["id"]: s["url"] for s in sources},
            source_types={s["id"]: s["type"] for s in sources},
            education_topics=dict(topics),
        )
        if engine is not None:
            catalog.fetched_at = load_fetch_dates(engine)
            catalog.managers = load_managers(engine, list(schemes))
        return catalog

    # ------------------------------------------------------------------ lookups
    def schemes_of(self, amc_id: str) -> list[Scheme]:
        return [s for s in self.schemes.values() if s.amc == amc_id]

    def link(self, source_id: str) -> Link:
        return Link(self.source_urls[source_id], self.fetched_at.get(source_id))

    def scheme_link(self, scheme_id: str) -> Link:
        return self.link(self.schemes[scheme_id].source_id)

    def education_link(self, topic: str = "default") -> Link:
        source_id = self.education_topics.get(topic) or self.education_topics["default"]
        return self.link(source_id)


def load_fetch_dates(engine: Engine) -> dict[str, date]:
    """Last successful fetch date (IST) of every source, from the sources table."""
    from app.store.models import Source

    with engine.connect() as conn:
        rows = conn.execute(select(Source.id, Source.fetched_at)).all()
    return {sid: to_ist_date(at) for sid, at in rows if at is not None}


def load_managers(engine: Engine, scheme_order: list[str]) -> dict[str, tuple[str, ...]]:
    """Each fund manager's covered schemes, from the fund_managers table (empty if not built)."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.store.models import FundManager

    try:
        with engine.connect() as conn:
            rows = conn.execute(select(FundManager.name, FundManager.scheme_id)).all()
    except SQLAlchemyError:
        return {}
    rank = {sid: i for i, sid in enumerate(scheme_order)}
    by_name: dict[str, set[str]] = {}
    for name, scheme_id in rows:
        if name and scheme_id in rank:
            by_name.setdefault(name.strip(), set()).add(scheme_id)
    return {name: tuple(sorted(ids, key=rank.__getitem__)) for name, ids in by_name.items()}
