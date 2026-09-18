"""Candidate constituent events, built from each source independently.

A *candidate* is a claim by one source that a ticker was added to / removed
from the index on a date. Candidates from different sources are later merged
(see ``membership.py``) into the final ``constituent_events`` log. Keeping the
per-source candidates makes every final event traceable to the raw snapshot
(``source_url`` + ``scraped_at`` + ``raw_sha256``) it came from.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from snp500.reconstruct.classify import ReasonCategory, classify_reason
from snp500.scraping.reference import ReferenceInterval, ReferenceSnapshot, intervals_from_snapshots
from snp500.scraping.wikipedia import ChangeRow, ConstituentRow

ADD = "ADD"
REMOVE = "REMOVE"

# Source priority when the same event is reported by several sources.
SOURCE_CONFIDENCE = {
    "wikipedia_changes": 0.90,
    "wikipedia_changes_with_ref": 0.95,
    "wikipedia_constituents": 0.75,  # "Date added" column of the current list
    "wikipedia_revision": 0.70,  # membership diff between two historical page revisions
    "reference_fja05680": 0.60,  # independent dataset (Clenow/Norgate 1996-2019, Wikipedia-derived after)
    "curated": 0.99,  # hand-verified override with a citation
}


@dataclass
class Candidate:
    effective_date: date
    action: str  # ADD | REMOVE
    ticker: str
    name: str  # '' when the source has no names (reference dataset)
    source_type: str
    source_url: str
    scraped_at: str
    confidence: float
    reason: str = ""
    reason_category: ReasonCategory = ReasonCategory.OTHER_UNKNOWN
    counterpart_ticker: str = ""  # the other side of the same change row, if any
    raw_sha256: str = ""
    notes: str = ""
    refs: list[str] = field(default_factory=list)
    company_id: Optional[str] = None  # filled by entity resolution

    @property
    def key(self) -> tuple:
        return (self.effective_date, self.action, self.ticker)


def deterministic_event_id(company_id: str, effective_date: date, action: str, ticker: str) -> str:
    h = hashlib.sha1(f"{company_id}|{effective_date.isoformat()}|{action}|{ticker}".encode()).hexdigest()
    return f"E{h[:14]}"


# --------------------------------------------------------------------------
# Wikipedia "selected changes" table
# --------------------------------------------------------------------------
def candidates_from_changes(rows: list[ChangeRow], source_url: str, scraped_at: str, raw_sha256: str) -> list[Candidate]:
    out: list[Candidate] = []
    for r in rows:
        cat = classify_reason(r.reason)
        conf = SOURCE_CONFIDENCE["wikipedia_changes_with_ref" if r.refs else "wikipedia_changes"]
        common = dict(
            effective_date=r.effective_date,
            source_type="wikipedia_changes",
            source_url=source_url,
            scraped_at=scraped_at,
            confidence=conf,
            reason=r.reason,
            reason_category=cat,
            raw_sha256=raw_sha256,
            refs=list(r.refs),
            notes=f"changes table row {r.row_index}",
        )
        if r.added_ticker:
            out.append(Candidate(action=ADD, ticker=r.added_ticker, name=r.added_name, counterpart_ticker=r.removed_ticker, **common))
        if r.removed_ticker:
            out.append(Candidate(action=REMOVE, ticker=r.removed_ticker, name=r.removed_name, counterpart_ticker=r.added_ticker, **common))
    return out


# --------------------------------------------------------------------------
# Wikipedia current constituents ("Date added" column)
# --------------------------------------------------------------------------
def candidates_from_constituents(rows: list[ConstituentRow], source_url: str, scraped_at: str, raw_sha256: str) -> list[Candidate]:
    out = []
    for r in rows:
        if r.date_added is None:
            continue
        out.append(
            Candidate(
                effective_date=r.date_added,
                action=ADD,
                ticker=r.ticker,
                name=r.name,
                source_type="wikipedia_constituents",
                source_url=source_url,
                scraped_at=scraped_at,
                confidence=SOURCE_CONFIDENCE["wikipedia_constituents"],
                reason="",
                reason_category=ReasonCategory.OTHER_UNKNOWN,
                raw_sha256=raw_sha256,
                notes="'Date added' column of current constituents list",
            )
        )
    return out


# --------------------------------------------------------------------------
# Reference dataset (ticker-level intervals)
# --------------------------------------------------------------------------
def candidates_from_reference(
    snapshots: list[ReferenceSnapshot], source_url: str, scraped_at: str, raw_sha256: str
) -> tuple[list[Candidate], list[ReferenceInterval]]:
    intervals = intervals_from_snapshots(snapshots)
    first = snapshots[0].on_date
    out = []
    for iv in intervals:
        if iv.start > first:
            out.append(
                Candidate(
                    effective_date=iv.start,
                    action=ADD,
                    ticker=iv.ticker,
                    name="",
                    source_type="reference_fja05680",
                    source_url=source_url,
                    scraped_at=scraped_at,
                    confidence=SOURCE_CONFIDENCE["reference_fja05680"],
                    raw_sha256=raw_sha256,
                    notes="ticker first appears in reference snapshot",
                )
            )
        else:
            out.append(
                Candidate(
                    effective_date=first,
                    action=ADD,
                    ticker=iv.ticker,
                    name="",
                    source_type="reference_fja05680",
                    source_url=source_url,
                    scraped_at=scraped_at,
                    confidence=SOURCE_CONFIDENCE["reference_fja05680"] - 0.1,
                    raw_sha256=raw_sha256,
                    reason="Member at start of reference data; true entry date is earlier and unknown here.",
                    notes="opening membership (left-censored)",
                )
            )
        if iv.end is not None:
            out.append(
                Candidate(
                    effective_date=iv.end,
                    action=REMOVE,
                    ticker=iv.ticker,
                    name="",
                    source_type="reference_fja05680",
                    source_url=source_url,
                    scraped_at=scraped_at,
                    confidence=SOURCE_CONFIDENCE["reference_fja05680"],
                    raw_sha256=raw_sha256,
                    notes="ticker disappears from reference snapshot",
                )
            )
    return out, intervals
