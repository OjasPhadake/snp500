"""Source orchestration: what to fetch, from where, and how it is stored.

Source identifiers (``source`` in the raw store and ``source_type`` on events):

* ``wikipedia_constituents``  – current constituents page
* ``wikipedia_changes``       – "Historical components" changes table
* ``wikipedia_revision``      – historical revisions of the constituents page
* ``reference_fja05680``      – independent daily membership dataset (MIT licensed)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from snp500.config import settings
from snp500.logging_setup import get_logger, log_event
from snp500.scraping.http import Fetched, Fetcher
from snp500.scraping.wikipedia import (
    CHANGES_URL,
    CONSTITUENTS_URL,
    REVISION_API,
    REVISION_URL,
    parse_revision_lookup,
)

log = get_logger(__name__)

SRC_CONSTITUENTS = "wikipedia_constituents"
SRC_CHANGES = "wikipedia_changes"
SRC_REVISION = "wikipedia_revision"
SRC_REVISION_LOOKUP = "wikipedia_revision_lookup"
SRC_REFERENCE = "reference_fja05680"

REFERENCE_FILES = {
    "membership": "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv",
    "ticker_start_end": "sp500_ticker_start_end.csv",
    "changes_since_2019": "sp500_changes_since_2019.csv",
    "README.md": "README.md",
    "LICENSE": "LICENSE",
}
REFERENCE_BASE = "https://raw.githubusercontent.com/fja05680/sp500/master/"


def fetch_current_constituents(fetcher: Fetcher, max_age: Optional[timedelta] = timedelta(days=1)) -> Fetched:
    return fetcher.fetch(SRC_CONSTITUENTS, CONSTITUENTS_URL, max_age=max_age)


def fetch_changes(fetcher: Fetcher, max_age: Optional[timedelta] = timedelta(days=1)) -> Fetched:
    return fetcher.fetch(SRC_CHANGES, CHANGES_URL, max_age=max_age)


def lookup_revision(fetcher: Fetcher, on_or_after: date) -> tuple[Optional[int], Optional[str]]:
    """First revision of the constituents page at/after ``on_or_after`` (UTC)."""
    url = REVISION_API.format(ts=f"{on_or_after.isoformat()}T00:00:00Z")
    got = fetcher.fetch(SRC_REVISION_LOOKUP, url, max_age=None, ext="json")
    return parse_revision_lookup(got.text)


def fetch_revision_snapshots(fetcher: Fetcher, snapshot_dates: list[date]) -> list[tuple[date, int, str, Fetched]]:
    """Fetch the page revision current on each requested date.

    Returns ``(requested_date, revid, revision_timestamp, fetched)`` tuples.
    Revisions are immutable, so they are cached forever (``max_age=None``).
    """
    out = []
    for d in snapshot_dates:
        revid, ts = lookup_revision(fetcher, d)
        if revid is None:
            log_event(log, "no revision found", date=d.isoformat())
            continue
        got = fetcher.fetch(SRC_REVISION, REVISION_URL.format(oldid=revid), max_age=None, note=f"snapshot for {d}")
        out.append((d, revid, ts or "", got))
    return out


def fetch_reference_dataset(fetcher: Fetcher, max_age: Optional[timedelta] = timedelta(days=30)) -> dict[str, Fetched]:
    out = {}
    for key, fname in REFERENCE_FILES.items():
        ext = "csv" if fname.endswith(".csv") else ("md" if fname.endswith(".md") else "txt")
        out[key] = fetcher.fetch(SRC_REFERENCE, REFERENCE_BASE + fname, max_age=max_age, ext=ext)
    return out


def default_snapshot_dates(start_year: int = 2008, end_year: Optional[int] = None) -> list[date]:
    """One snapshot per year (first revision on/after Jan 1). 2008 is the first
    year in which the page reliably carried a GICS sector column."""
    end_year = end_year or date.today().year
    return [date(y, 1, 1) for y in range(start_year, end_year + 1)]


def write_manifest_summary(path: Path, fetched: dict[str, list[dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fetched, indent=2, default=str))
