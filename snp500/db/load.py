"""Load build artefacts (data/build/*.csv) into the database.

The load is idempotent and atomic per run: all tables derived from the build
are truncated and re-filled inside one transaction, so readers never observe a
half-loaded state. Raw snapshots are indexed from the raw store manifests.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy import delete

from snp500.config import settings
from snp500.db import models as m
from snp500.db.session import get_engine, get_sessionmaker
from snp500.logging_setup import get_logger, log_event
from snp500.scraping.raw_store import RawStore

log = get_logger(__name__)


def _d(s: str) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def _b(s: str) -> bool:
    return str(s).lower() in ("true", "1", "yes")


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def create_schema(url: Optional[str] = None) -> None:
    m.Base.metadata.create_all(get_engine(url))


def load_build(build_dir: Optional[Path] = None, url: Optional[str] = None, raw_dir: Optional[Path] = None) -> dict:
    build_dir = Path(build_dir or settings.build_dir)
    create_schema(url)
    Session = get_sessionmaker(url)
    manifest = json.loads((build_dir / "build_manifest.json").read_text())
    counts: dict[str, int] = {}
    with Session() as s, s.begin():
        for table in (m.SectorObservation, m.MembershipInterval, m.ConstituentEvent, m.CompanyTicker, m.CompanyName, m.Company, m.BuildIssue, m.Discrepancy, m.RawSnapshotRecord):
            s.execute(delete(table))
        companies = [m.Company(company_id=r["company_id"], canonical_name=r["canonical_name"], name_key=r["name_key"], current_ticker=r["current_ticker"] or None, cik=r["cik"] or None, needs_review=_b(r["needs_review"]), notes=r["notes"] or None) for r in _rows(build_dir / "companies.csv")]
        s.add_all(companies)
        counts["companies"] = len(companies)
        names = [m.CompanyName(company_id=r["company_id"], name=r["name"], first_seen=_d(r["first_seen"])) for r in _rows(build_dir / "company_names.csv")]
        s.add_all(names)
        counts["company_names"] = len(names)
        tickers = [m.CompanyTicker(company_id=r["company_id"], ticker=r["ticker"], valid_from=_d(r["valid_from"]), valid_to=_d(r["valid_to"]), evidence_kinds=r["evidence_kinds"], first_evidence=_d(r["first_evidence"]), last_evidence=_d(r["last_evidence"])) for r in _rows(build_dir / "company_tickers.csv")]
        s.add_all(tickers)
        counts["company_tickers"] = len(tickers)
        events = [
            m.ConstituentEvent(event_id=r["event_id"], effective_date=_d(r["effective_date"]), action=r["action"], company_id=r["company_id"], ticker_at_event=r["ticker_at_event"], company_name=r["company_name"], reason=r["reason"] or None, reason_category=r["reason_category"], counterpart_company_id=r["counterpart_company_id"] or None, source_url=r["source_url"], source_type=r["source_type"], scraped_at=r["scraped_at"] or None, raw_sha256=r["raw_sha256"] or None, confidence=float(r["confidence"]), corroborating_sources=r["corroborating_sources"] or None, refs=r["refs"] or None, notes=r["notes"] or None, in_window=_b(r["in_window"]))
            for r in _rows(build_dir / "events.csv")
        ]
        s.add_all(events)
        counts["constituent_events"] = len(events)
        intervals = [m.MembershipInterval(company_id=r["company_id"], entry_date=_d(r["entry_date"]), exit_date=_d(r["exit_date"]), entry_event_id=r["entry_event_id"] or None, exit_event_id=r["exit_event_id"] or None, entry_ticker=r["entry_ticker"] or None, exit_ticker=r["exit_ticker"] or None, exit_reason_category=r["exit_reason_category"] or None, left_censored=_b(r["left_censored"]), supported=_b(r["supported"])) for r in _rows(build_dir / "intervals.csv")]
        s.add_all(intervals)
        counts["membership_intervals"] = len(intervals)
        sectors = [m.SectorObservation(company_id=r["company_id"], ticker=r["ticker"], as_of_date=_d(r["as_of_date"]), gics_sector=r["gics_sector"] or None, gics_sub_industry=r["gics_sub_industry"] or None, source_url=r["source_url"], source_type=r["source_type"], scraped_at=r["scraped_at"] or None) for r in _rows(build_dir / "sector_observations.csv")]
        s.add_all(sectors)
        counts["company_sector_observations"] = len(sectors)
        issues = [m.BuildIssue(company_id=r["company_id"] or None, kind=r["kind"], on_date=_d(r["on_date"]), detail=r["detail"]) for r in _rows(build_dir / "issues.csv")]
        s.add_all(issues)
        counts["build_issues"] = len(issues)
        disc = [m.Discrepancy(comparison=r["comparison"], severity=r["severity"], kind=r["kind"], ticker=r["ticker"] or None, company_id=r["company_id"] or None, company_name=r["company_name"] or None, first_date=_d(r["first_date"]), last_date=_d(r["last_date"]), n_dates=int(r["n_dates"])) for r in _rows(build_dir / "reconciliation" / "discrepancies.csv")]
        s.add_all(disc)
        counts["reconciliation_discrepancies"] = len(disc)
        store = RawStore(raw_dir or settings.raw_dir)
        raw = []
        for src_dir in sorted(p for p in store.raw_dir.iterdir() if p.is_dir()):
            for snap in store.iter_snapshots(src_dir.name):
                raw.append(m.RawSnapshotRecord(source=snap.source, url=snap.url, fetched_at=snap.fetched_at, sha256=snap.sha256, path=snap.path, status=snap.status, n_bytes=snap.n_bytes, note=snap.note or None))
        s.add_all(raw)
        counts["raw_snapshots"] = len(raw)
        s.add(m.BuildRun(built_at=manifest["built_at"], manifest_json=json.dumps(manifest)))
    log_event(log, "loaded", **counts)
    return counts
