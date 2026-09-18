from __future__ import annotations

from datetime import date

import pytest

from snp500.reconstruct.classify import ReasonCategory
from snp500.reconstruct.entity_resolution import CompanyRegistry, Curated
from snp500.reconstruct.events import ADD, REMOVE, Candidate
from snp500.reconstruct.membership import SnapshotObservation, build
from snp500.scraping.reference import ReferenceSnapshot, intervals_from_snapshots
from snp500.scraping.wikipedia import ConstituentRow

WIKI = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
REF = "https://example.org/reference.csv"


def cand(d: str, action: str, ticker: str, name: str = "", source: str = "wikipedia_changes", reason: str = "", counterpart: str = "", conf: float = 0.9) -> Candidate:
    from snp500.reconstruct.classify import classify_reason

    return Candidate(effective_date=date.fromisoformat(d), action=action, ticker=ticker, name=name, source_type=source, source_url=WIKI if source != "reference_fja05680" else REF, scraped_at="2026-01-01T00:00:00Z", confidence=conf, reason=reason, reason_category=classify_reason(reason), counterpart_ticker=counterpart)


def ref_from_sets(sets: dict[str, set[str]]):
    """Build reference candidates + intervals from {date: tickers}."""
    from snp500.reconstruct.events import candidates_from_reference

    snaps = [ReferenceSnapshot(date.fromisoformat(d), frozenset(t)) for d, t in sorted(sets.items())]
    cands, ivs = candidates_from_reference(snaps, REF, "2026-01-01T00:00:00Z", "deadbeef")
    return cands, ivs


def snapshot(d: str, rows: list[tuple[str, str]], sector: str | None = None) -> SnapshotObservation:
    return SnapshotObservation(date.fromisoformat(d), [ConstituentRow(t, n, sector, None, None, None, "", None, None) for t, n in rows], "https://en.wikipedia.org/w/index.php?oldid=1", "2026-01-01T00:00:00Z", "wikipedia_revision")


def run(wiki, snaps=None, ref=None, curated=None):
    reg = CompanyRegistry(curated or Curated.empty())
    ref_c, ref_iv = ref if ref else ([], [])
    return build(wiki, snaps or [], ref_c, ref_iv, reg, date(2001, 1, 1), date(2026, 12, 31))


@pytest.fixture
def make():
    return dict(cand=cand, ref=ref_from_sets, snapshot=snapshot, run=run)
