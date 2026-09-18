"""SQLAlchemy models: the normalised, queryable form of the build artefacts.

Design notes
------------
* ``constituent_events`` is the event-sourced core (ADD / REMOVE / TICKER_CHANGE).
* ``membership_intervals`` is a *materialised derivation* of the events (kept
  for query speed; rebuilt on every load, never edited by hand).
* Company identity (``companies``) is separate from ticker identity
  (``company_tickers``) and name history (``company_names``).
* Sector classifications are stored as dated *observations*
  (``company_sector_observations``), never as a single current value, so
  historical analyses can use the classification that was in force.
* Every event cites its raw snapshot (``source_url``, ``scraped_at``,
  ``raw_sha256``) and the ``raw_snapshots`` table indexes the raw store.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    company_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    current_ticker: Mapped[str | None] = mapped_column(String(12))
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    names: Mapped[list["CompanyName"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    tickers: Mapped[list["CompanyTicker"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    events: Mapped[list["ConstituentEvent"]] = relationship(back_populates="company", cascade="all, delete-orphan", foreign_keys="ConstituentEvent.company_id")
    intervals: Mapped[list["MembershipInterval"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    sectors: Mapped[list["SectorObservation"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class CompanyName(Base):
    __tablename__ = "company_names"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    first_seen: Mapped[date] = mapped_column(Date, nullable=False)
    company: Mapped[Company] = relationship(back_populates="names")


class CompanyTicker(Base):
    __tablename__ = "company_tickers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    valid_from: Mapped[date | None] = mapped_column(Date)  # NULL = unknown / before evidence
    valid_to: Mapped[date | None] = mapped_column(Date)  # NULL = current
    evidence_kinds: Mapped[str | None] = mapped_column(String(100))
    first_evidence: Mapped[date | None] = mapped_column(Date)
    last_evidence: Mapped[date | None] = mapped_column(Date)
    company: Mapped[Company] = relationship(back_populates="tickers")


class ConstituentEvent(Base):
    __tablename__ = "constituent_events"
    event_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(14), nullable=False, index=True)  # ADD | REMOVE | TICKER_CHANGE
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"), index=True)
    ticker_at_event: Mapped[str] = mapped_column(String(12), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    reason_category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    counterpart_company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.company_id"))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    scraped_at: Mapped[str | None] = mapped_column(String(25))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    corroborating_sources: Mapped[str | None] = mapped_column(String(200))
    refs: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    in_window: Mapped[bool] = mapped_column(Boolean, default=True)
    company: Mapped[Company] = relationship(back_populates="events", foreign_keys=[company_id])


class MembershipInterval(Base):
    __tablename__ = "membership_intervals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"), index=True)
    entry_date: Mapped[date | None] = mapped_column(Date, index=True)  # NULL = unknown (left-censored)
    exit_date: Mapped[date | None] = mapped_column(Date, index=True)  # NULL = current member
    entry_event_id: Mapped[str | None] = mapped_column(String(16))
    exit_event_id: Mapped[str | None] = mapped_column(String(16))
    entry_ticker: Mapped[str | None] = mapped_column(String(12))
    exit_ticker: Mapped[str | None] = mapped_column(String(12))
    exit_reason_category: Mapped[str | None] = mapped_column(String(40))
    left_censored: Mapped[bool] = mapped_column(Boolean, default=False)
    supported: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    company: Mapped[Company] = relationship(back_populates="intervals")


Index("ix_intervals_range", MembershipInterval.entry_date, MembershipInterval.exit_date)


class SectorObservation(Base):
    __tablename__ = "company_sector_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    gics_sector: Mapped[str | None] = mapped_column(String(60))
    gics_sub_industry: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    scraped_at: Mapped[str | None] = mapped_column(String(25))
    company: Mapped[Company] = relationship(back_populates="sectors")


class BuildIssue(Base):
    __tablename__ = "build_issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str | None] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    on_date: Mapped[date | None] = mapped_column(Date)
    detail: Mapped[str] = mapped_column(Text)


class Discrepancy(Base):
    __tablename__ = "reconciliation_discrepancies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comparison: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    ticker: Mapped[str | None] = mapped_column(String(12))
    company_id: Mapped[str | None] = mapped_column(String(16))
    company_name: Mapped[str | None] = mapped_column(String(200))
    first_date: Mapped[date] = mapped_column(Date)
    last_date: Mapped[date] = mapped_column(Date)
    n_dates: Mapped[int] = mapped_column(Integer)


class RawSnapshotRecord(Base):
    __tablename__ = "raw_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[str] = mapped_column(String(25))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer)
    n_bytes: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class BuildRun(Base):
    __tablename__ = "build_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    built_at: Mapped[str] = mapped_column(String(25))
    loaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    manifest_json: Mapped[str] = mapped_column(Text)
