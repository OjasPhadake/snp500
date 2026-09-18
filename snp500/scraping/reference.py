"""Loader for the fja05680/sp500 reference dataset (MIT license).

The dataset is a list of ``(date, "T1,T2,...")`` rows: the full ticker set on
each date the compiler observed a change. It is used for *validation and
gap-filling*, never copied wholesale (see docs/DECISIONS.md, ADR-004).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from snp500.normalize.text import normalize_ticker


@dataclass
class ReferenceSnapshot:
    on_date: date
    tickers: frozenset[str]


def parse_membership_csv(text: str) -> list[ReferenceSnapshot]:
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        d = date.fromisoformat(row["date"].strip())
        raw = [t.strip() for t in row["tickers"].split(",") if t.strip()]
        tickers = set()
        for t in raw:
            try:
                tickers.add(normalize_ticker(t))
            except ValueError:
                tickers.add(t.upper())  # keep odd legacy symbols verbatim rather than drop them
        out.append(ReferenceSnapshot(d, frozenset(tickers)))
    out.sort(key=lambda s: s.on_date)
    return out


def snapshot_on(snapshots: list[ReferenceSnapshot], d: date) -> frozenset[str] | None:
    """Ticker set applicable on ``d`` (the latest snapshot dated <= d)."""
    best = None
    for s in snapshots:
        if s.on_date <= d:
            best = s
        else:
            break
    return best.tickers if best else None


@dataclass
class ReferenceInterval:
    ticker: str
    start: date
    end: date | None  # None = still a member at end of data


def intervals_from_snapshots(snapshots: list[ReferenceSnapshot]) -> list[ReferenceInterval]:
    """Derive per-ticker membership intervals from consecutive snapshots.

    ``start`` is the first snapshot date containing the ticker; ``end`` is the
    first snapshot date on which it is absent (i.e. the exit *effective* date).
    """
    active: dict[str, date] = {}
    out: list[ReferenceInterval] = []
    prev: frozenset[str] = frozenset()
    for s in snapshots:
        for t in s.tickers - prev:
            active[t] = s.on_date
        for t in prev - s.tickers:
            out.append(ReferenceInterval(t, active.pop(t), s.on_date))
        prev = s.tickers
    for t, st in active.items():
        out.append(ReferenceInterval(t, st, None))
    out.sort(key=lambda r: (r.ticker, r.start))
    return out
