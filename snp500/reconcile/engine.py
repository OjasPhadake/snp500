"""Reconciliation engine: compare the reconstructed history with independent data.

Comparisons (each produces explicit discrepancy records; nothing is modified):

1. **Reference dataset, company level** – on every date the reference changed
   plus every quarter-end: the set of companies the reference implies (its
   ticker intervals mapped through the same entity resolution) vs. ours.
   Because the reference back-fills tickers, a ticker-level comparison would
   mostly report labelling differences; the company level isolates genuine
   membership disagreements (date differences beyond tolerance, single-source
   events, unresolved identities).
2. **Wikipedia page revisions, ticker level** – the constituents page as it
   stood on Jan 1 of each year *is* point-in-time, so tickers are compared
   directly. This is the independent check on ticker history.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from snp500.reconstruct.entity_resolution import CompanyRegistry
from snp500.reconstruct.membership import Interval, constituents_on
from snp500.scraping.reference import ReferenceInterval


@dataclass
class Discrepancy:
    on_date: date
    comparison: str
    kind: str  # missing_in_ours | extra_in_ours
    ticker: str
    company_id: str
    company_name: str


def _tkey(t: str) -> str:
    return t.replace(".", "").replace("-", "").upper()


def our_tickers_on(intervals: list[Interval], reg: CompanyRegistry, d: date) -> dict[str, str]:
    """ticker (compare key) -> company_id for every ticker line active on ``d``."""
    out = {}
    for iv in constituents_on(intervals, d):
        comp = reg.get(iv.company_id)
        for t in comp.tickers_on(d) or [iv.entry_ticker]:
            out[_tkey(t)] = iv.company_id
    return out


def compare_tickers(intervals: list[Interval], reg: CompanyRegistry, comparison: str, snapshots: Iterable[tuple[date, frozenset[str]]]) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    for d, theirs_raw in snapshots:
        theirs = {_tkey(t) for t in theirs_raw}
        ours = our_tickers_on(intervals, reg, d)
        for t in sorted(theirs - set(ours)):
            hits = [c for c in reg.live() if any(_tkey(x) == t for x in c.all_tickers())]
            comp = hits[0] if len(hits) == 1 else None
            out.append(Discrepancy(d, comparison, "missing_in_ours", t, comp.company_id if comp else "", comp.canonical_name if comp else ""))
        for t in sorted(set(ours) - theirs):
            out.append(Discrepancy(d, comparison, "extra_in_ours", t, ours[t], reg.get(ours[t]).canonical_name))
    return out


def compare_companies(intervals: list[Interval], reg: CompanyRegistry, ref_assignment: dict[str, list[ReferenceInterval]], dates: list[date]) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    for d in dates:
        ours = {iv.company_id for iv in constituents_on(intervals, d)}
        theirs: dict[str, str] = {}
        for cid, ivs in ref_assignment.items():
            for iv in ivs:
                if iv.start <= d and (iv.end is None or d < iv.end):
                    theirs[cid] = iv.ticker
        for cid in sorted(set(theirs) - ours):
            out.append(Discrepancy(d, "reference_company", "missing_in_ours", theirs[cid], cid, reg.get(cid).canonical_name))
        for cid in sorted(ours - set(theirs)):
            comp = reg.get(cid)
            out.append(Discrepancy(d, "reference_company", "extra_in_ours", comp.ticker_on(d) or "", cid, comp.canonical_name))
    return out


def collapse(discrepancies: list[Discrepancy]) -> list[dict]:
    groups: dict[tuple[str, str, str, str], list[Discrepancy]] = defaultdict(list)
    for d in discrepancies:
        groups[(d.comparison, d.kind, d.ticker, d.company_id)].append(d)
    rows = []
    for (comparison, kind, ticker, cid), lst in groups.items():
        lst.sort(key=lambda x: x.on_date)
        rows.append({"comparison": comparison, "kind": kind, "ticker": ticker, "company_id": cid, "company_name": lst[0].company_name, "first_date": lst[0].on_date.isoformat(), "last_date": lst[-1].on_date.isoformat(), "n_dates": len(lst)})
    for r in rows:
        span = (date.fromisoformat(r["last_date"]) - date.fromisoformat(r["first_date"])).days
        r["severity"] = "minor_date_disagreement" if span <= 10 else "membership"
    rows.sort(key=lambda r: (r["comparison"], r["severity"], r["first_date"], r["ticker"]))
    return rows


def quarter_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, 3
    while date(y, m, 1) <= end:
        d = (date(y, m, 1) + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        if start <= d <= end:
            out.append(d)
        m += 3
        if m > 12:
            m, y = 3, y + 1
    return out


def render_report(rows: list[dict], per_date: dict[str, list[tuple[date, int, int, int, int]]], issues: list, unknown_companies: list, build_meta: dict) -> str:
    L = ["# Reconciliation report", "", f"Built: {build_meta.get('built_at')}  ", "Inputs (sha256 prefix): " + ", ".join(f"{k}={v[:10]}" for k, v in build_meta.get("input_sha256", {}).items() if not k.startswith("wikipedia_revision")), ""]
    L += ["This report is generated by `snp500 build`. Every row is a disagreement between the reconstruction and an independent source, or an anomaly the state machine had to make a decision about. Rows are inputs to the curation workflow in `data/curated/README.md`; they are never auto-resolved.", ""]
    for comparison, series in per_date.items():
        L += [f"## {comparison}: membership by date", ""]
        exact = sum(1 for _, _, _, m, e in series if m == 0 and e == 0)
        L += [f"{exact} of {len(series)} compared dates match exactly. Showing quarter-ends and dates with disagreements.", "", "| date | ours | theirs | missing_in_ours | extra_in_ours |", "|---|---:|---:|---:|---:|"]
        for d, n, nt, m, e in series:
            if m or e or (d.month in (3, 6, 9, 12) and d.day >= 28):
                L.append(f"| {d} | {n} | {nt} | {m} | {e} |")
        L.append("")
    L += ["## Discrepancy ranges (consecutive dates collapsed)", ""]
    kinds = Counter((r["comparison"], r["severity"], r["kind"]) for r in rows)
    for (comparison, sev, kind), n in sorted(kinds.items()):
        L.append(f"- {comparison} / {sev} / {kind}: {n} ranges")
    L += ["", "### Membership disagreements (longer than the 10-day clustering tolerance)", "", "| comparison | kind | ticker | company | company_id | first | last | n |", "|---|---|---|---|---|---|---|---:|"]
    for r in rows:
        if r["severity"] == "membership":
            L.append(f"| {r['comparison']} | {r['kind']} | {r['ticker']} | {r['company_name'] or '-'} | {r['company_id'] or '-'} | {r['first_date']} | {r['last_date']} | {r['n_dates']} |")
    L += ["", "### Minor date disagreements (sources differ on the effective date by up to 10 days; the higher-confidence source's date was used)", "", "| comparison | kind | ticker | company | first | last | n |", "|---|---|---|---|---|---|---:|"]
    for r in rows:
        if r["severity"] != "membership":
            L.append(f"| {r['comparison']} | {r['kind']} | {r['ticker']} | {r['company_name'] or '-'} | {r['first_date']} | {r['last_date']} | {r['n_dates']} |")
    L += ["", "## Build issues (state-machine anomalies)", ""]
    for k, n in sorted(Counter(i.kind for i in issues).items()):
        L.append(f"- {k}: {n}")
    L += ["", "| company_id | kind | date | detail |", "|---|---|---|---|"]
    for i in issues:
        L.append(f"| {i.company_id} | {i.kind} | {i.on_date or ''} | {i.detail} |")
    L += ["", "## Companies needing review (identity known only from ticker-level evidence)", "", f"{len(unknown_companies)} companies. Ticker(s):", "", ", ".join(sorted(", ".join(c.all_tickers()) for c in unknown_companies)), ""]
    return "\n".join(L)
