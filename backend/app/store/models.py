"""SQLAlchemy models for ingestion: fetched sources and structured scheme data.

Data policy: everything a source publishes is extracted and retained, including returns, NAV,
AUM, category averages, ranks, risk ratios, holdings and peer data. These are stored as facts
with their basis and as-of date. Restrictions apply only to interpretation: answers never judge,
rank or recommend. Groww's own evaluative text (`scheme_analysis`) is stored but flagged.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Source(Base):
    """One allowlisted URL and the state of its last fetch.

    `fetched_at` is the time of the last *successful* fetch and only moves forward on success,
    so a failed fetch leaves the old date in place and the answer footer shows the true age.
    """

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String)
    scheme_id: Mapped[str | None] = mapped_column(String)
    amc: Mapped[str | None] = mapped_column(String)
    sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ok | unchanged | failed | skipped_robots | from_disk
    last_status: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(Text)


class SchemeFact(Base):
    """Structured facts for one scheme, as published on its Groww page.

    Text fields keep the value as shown on the page ("0.70%", "₹100"); `*_value` columns hold the
    parsed number. NULL means "not found on the page", never "none".
    """

    __tablename__ = "scheme_facts"

    scheme_id: Mapped[str] = mapped_column(String, primary_key=True)
    scheme_name: Mapped[str | None] = mapped_column(Text)
    amc: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    expense_ratio: Mapped[str | None] = mapped_column(String)
    expense_ratio_value: Mapped[float | None] = mapped_column(Float)
    exit_load: Mapped[str | None] = mapped_column(Text)
    min_sip: Mapped[str | None] = mapped_column(String)
    min_sip_value: Mapped[float | None] = mapped_column(Float)
    min_lumpsum: Mapped[str | None] = mapped_column(String)
    min_lumpsum_value: Mapped[float | None] = mapped_column(Float)
    lock_in: Mapped[str | None] = mapped_column(String)
    riskometer: Mapped[str | None] = mapped_column(String)
    benchmark: Mapped[str | None] = mapped_column(Text)
    fund_managers: Mapped[list | None] = mapped_column(JSON)
    launch_date: Mapped[date | None] = mapped_column(Date)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    source_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # json | dom | mixed: which extraction path produced the facts
    extracted_from: Mapped[str | None] = mapped_column(String)
    review_note: Mapped[str | None] = mapped_column(Text)


class SchemeSnapshot(Base):
    """The complete data object embedded in a scheme page, stored verbatim (nothing dropped)."""

    __tablename__ = "scheme_snapshots"

    scheme_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    source_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class SchemeMetric(Base):
    """One published number: NAV, AUM, a return for a period, a category average, a rank, a ratio.

    `basis` says exactly what the number is, so answers can label it correctly:
      annualised      Groww's "Annualised returns" (CAGR) for periods over 1 year and since launch
      absolute        Groww's "Absolute returns" (total change over the period); for periods up
                      to 1 year this is also what the annualised table shows
      sip_annualised  annualised return of a monthly SIP over the period
      point           a single value (NAV, AUM, ratio, rating)
    `displayed` is True only for values visible on the page as it loads (checked against the
    page's own returns table); False for values in the page data that are not visible, e.g.
    absolute returns over 1 year (shown only after clicking a tab), SIP returns, risk ratios.
    """

    __tablename__ = "scheme_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    metric: Mapped[str] = mapped_column(String)
    period: Mapped[str | None] = mapped_column(String)
    basis: Mapped[str] = mapped_column(String)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    rank_of: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(Text)
    as_of: Mapped[date | None] = mapped_column(Date)
    displayed: Mapped[bool] = mapped_column(Boolean, default=True)
    source_id: Mapped[str] = mapped_column(String)
    source_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemeHolding(Base):
    __tablename__ = "scheme_holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    position: Mapped[int] = mapped_column(Integer)
    company_name: Mapped[str] = mapped_column(Text)
    sector: Mapped[str | None] = mapped_column(String)
    instrument: Mapped[str | None] = mapped_column(String)
    asset_type: Mapped[str | None] = mapped_column(String)
    credit_rating: Mapped[str | None] = mapped_column(String)
    market_value_cr: Mapped[float | None] = mapped_column(Float)
    percent_of_assets: Mapped[float | None] = mapped_column(Float)
    portfolio_date: Mapped[date | None] = mapped_column(Date)
    source_id: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemePeer(Base):
    """A "similar fund" listed on the scheme's Groww page, with the figures Groww shows for it."""

    __tablename__ = "scheme_peers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    position: Mapped[int] = mapped_column(Integer)
    peer_name: Mapped[str] = mapped_column(Text)
    peer_groww_id: Mapped[str | None] = mapped_column(String)
    fund_house: Mapped[str | None] = mapped_column(String)
    category: Mapped[str | None] = mapped_column(String)
    aum_cr: Mapped[float | None] = mapped_column(Float)
    expense_ratio: Mapped[float | None] = mapped_column(Float)
    return_1m: Mapped[float | None] = mapped_column(Float)
    return_3m: Mapped[float | None] = mapped_column(Float)
    return_6m: Mapped[float | None] = mapped_column(Float)
    return_1y: Mapped[float | None] = mapped_column(Float)
    return_3y: Mapped[float | None] = mapped_column(Float)
    return_5y: Mapped[float | None] = mapped_column(Float)
    return_since_launch: Mapped[float | None] = mapped_column(Float)
    min_investment: Mapped[float | None] = mapped_column(Float)
    min_sip: Mapped[float | None] = mapped_column(Float)
    riskometer: Mapped[str | None] = mapped_column(String)
    groww_rating: Mapped[float | None] = mapped_column(Float)
    launch_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict] = mapped_column(JSON)
    source_id: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundManager(Base):
    __tablename__ = "fund_managers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    managing_since: Mapped[date | None] = mapped_column(Date)
    education: Mapped[str | None] = mapped_column(Text)
    experience: Mapped[str | None] = mapped_column(Text)
    other_funds: Mapped[list | None] = mapped_column(JSON)
    source_id: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemeHistory(Base):
    """Dated history published on the page: expense-ratio series and exit-load changes."""

    __tablename__ = "scheme_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)  # expense_ratio | exit_load
    as_on_date: Mapped[date | None] = mapped_column(Date)
    value: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSON)
    source_id: Mapped[str] = mapped_column(String)


class SchemeAttribute(Base):
    """Every other scalar field on the page (codes, ISIN, limits, AMC and RTA details, ...).

    `reliable` is False for fields known to be wrong on Groww (e.g. `category_info.*`).
    """

    __tablename__ = "scheme_attributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str | None] = mapped_column(Text)
    reliable: Mapped[bool] = mapped_column(Boolean, default=True)
    source_id: Mapped[str] = mapped_column(String)


class SchemeAnalysis(Base):
    """Groww's own PROS/CONS statements. Stored for completeness; these are Groww's judgements,
    not facts, so they are never used as answer evidence."""

    __tablename__ = "scheme_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme_id: Mapped[str] = mapped_column(String, index=True)
    analysis_type: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    data: Mapped[str | None] = mapped_column(Text)
    is_judgement: Mapped[bool] = mapped_column(Boolean, default=True)
    source_id: Mapped[str] = mapped_column(String)


# ------------------------------------------------------------------------------ conversations
# Threads and messages (architecture §7.2). Message content is PII-free by construction: a
# message the PII guard blocked is stored only as the placeholder.


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # UUID4
    title: Mapped[str | None] = mapped_column(Text)  # first stored user message, truncated
    active_scheme_id: Mapped[str | None] = mapped_column(String)  # "its" / "this fund"
    pending_candidates: Mapped[list | None] = mapped_column(JSON)  # after a clarifying question
    last_intent: Mapped[dict | None] = mapped_column(JSON)  # "and for X?" (Phase 7)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # retention job


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # UUID4
    thread_id: Mapped[str] = mapped_column(
        String, ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)  # order within the thread, from 1
    role: Mapped[str] = mapped_column(String)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    response_type: Mapped[str | None] = mapped_column(String)  # assistant: Reply.type
    route: Mapped[str | None] = mapped_column(String)  # assistant: how the reply was made
    citation_url: Mapped[str | None] = mapped_column(Text)
    last_updated: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime)


# Which tables live in which database (Phase 11): the corpus database is published in the data
# bundle, the threads database never is.
THREAD_TABLES = (Thread.__table__, Message.__table__)
CORPUS_TABLES = tuple(t for t in Base.metadata.sorted_tables if t not in THREAD_TABLES)
