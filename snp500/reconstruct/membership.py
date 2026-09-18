"""Merge per-source candidates into one event log and derive membership intervals.

Pipeline
--------
1. **Resolve named observations** (Wikipedia change rows, dated page
   revisions, today's list) chronologically through :class:`CompanyRegistry`.
   Same-row / curated ticker changes are detected here.
2. **Resolve reference intervals** (ticker-only) by dated ticker evidence and
   membership plausibility, then by *date pairing* with Wikipedia events the
   reference reports under a different ticker (H2: same removal date, H3:
   same addition date, H1: same-day swap that is really a rename).
3. **Cluster** candidates per (company, action) within ``CLUSTER_TOL_DAYS`` –
   the same real-world event reported by several sources becomes one event
   whose primary source is the highest-confidence candidate; confidence is
   bumped for each corroborating source.
4. **State machine** per company: ADD opens an interval, REMOVE closes it.
   Anomalies are recorded as *issues*; nothing is silently dropped.
5. **Support flag**: an interval with no overlap with any reference-dataset
   interval assigned to the company is ``supported=False``. Such intervals
   come from a single uncorroborated source and are excluded from
   point-in-time queries by default (they remain in the event log).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from snp500.reconstruct.classify import ReasonCategory, classify_reason
from snp500.reconstruct.entity_resolution import CONTINUITY_DAYS, Company, CompanyRegistry
from snp500.reconstruct.events import ADD, REMOVE, Candidate, deterministic_event_id
from snp500.scraping.reference import ReferenceInterval
from snp500.scraping.wikipedia import ConstituentRow

TICKER_CHANGE = "TICKER_CHANGE"
CLUSTER_TOL_DAYS = 10
PAIR_TOL_DAYS = 3
RENAME_MIN_PRIOR_DAYS = 30


@dataclass
class Event:
    event_id: str
    effective_date: date
    action: str  # ADD | REMOVE | TICKER_CHANGE
    company_id: str
    ticker_at_event: str
    company_name: str
    reason: str
    reason_category: str
    source_url: str
    source_type: str
    scraped_at: str
    confidence: float
    counterpart_company_id: Optional[str] = None
    corroborating_sources: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    raw_sha256: str = ""
    notes: str = ""
    in_window: bool = True


@dataclass
class Interval:
    company_id: str
    entry_date: Optional[date]  # None = unknown (left-censored)
    exit_date: Optional[date]  # None = current member
    entry_event_id: Optional[str]
    exit_event_id: Optional[str]
    entry_ticker: str
    exit_ticker: str
    exit_reason_category: str = ""
    left_censored: bool = False
    supported: bool = True
    entry_source: str = ""


@dataclass
class Issue:
    company_id: str
    kind: str
    on_date: Optional[date]
    detail: str


@dataclass
class SnapshotObservation:
    on_date: date
    rows: list[ConstituentRow]
    source_url: str
    scraped_at: str
    source_type: str  # wikipedia_revision | wikipedia_constituents


@dataclass
class BuildResult:
    registry: CompanyRegistry
    events: list[Event]
    intervals: list[Interval]
    issues: list[Issue]
    resolved: list[Candidate]
    ref_assignment: dict[str, list[ReferenceInterval]]  # company_id -> reference intervals


# --------------------------------------------------------------------------
# Phase 1
# --------------------------------------------------------------------------
def resolve_named(cands: list[Candidate], snapshots: list[SnapshotObservation], reg: CompanyRegistry) -> list[Candidate]:
    """Chronologically resolve every named observation; returns candidates with company ids."""
    timeline: list[tuple[date, int, object]] = []
    for c in cands:
        if c.source_type in ("wikipedia_changes", "curated"):
            timeline.append((c.effective_date, 0 if c.action == ADD else 1, c))
    for s in snapshots:
        timeline.append((s.on_date, 2, s))
    # constituents "date added" candidates are attached to the company resolved from today's row
    dated_added = [c for c in cands if c.source_type == "wikipedia_constituents"]
    timeline.sort(key=lambda x: (x[0], x[1], getattr(x[2], "ticker", "")))

    tc_remove_halves: dict[tuple[date, str], str] = {}
    out: list[Candidate] = []
    for on, _, obj in timeline:
        if isinstance(obj, Candidate):
            c = obj
            if c.action == ADD:
                comp, is_tc = reg.resolve_named(c.ticker, c.name, on, c.source_type, "add", counterpart_ticker=c.counterpart_ticker, reason_is_ticker_change=(c.reason_category == ReasonCategory.TICKER_CHANGE))
                c.company_id = comp.company_id
                if is_tc:
                    c.action = TICKER_CHANGE
                    c.reason_category = ReasonCategory.TICKER_CHANGE
                    tc_remove_halves[(on, c.counterpart_ticker)] = comp.company_id
                else:
                    comp.claims.append((on, ADD, c.source_type))
            else:
                if (on, c.ticker) in tc_remove_halves:
                    c.company_id = tc_remove_halves[(on, c.ticker)]
                    c.action = TICKER_CHANGE
                    c.reason_category = ReasonCategory.TICKER_CHANGE
                    c.notes += "; remove-half of ticker change"
                    reg.get(c.company_id).add_evidence(c.ticker, on, "remove")
                else:
                    tc = reg.curated.ticker_change_from(c.ticker, on)
                    comp, _ = reg.resolve_named(c.ticker, c.name, on, c.source_type, "remove")
                    c.company_id = comp.company_id
                    if tc is not None and reg.by_ticker_continuity(tc.new_ticker, on) is comp:
                        c.action = TICKER_CHANGE
                        c.reason_category = ReasonCategory.TICKER_CHANGE
                        c.notes += "; remove-half of curated ticker change"
                    else:
                        comp.claims.append((on, REMOVE, c.source_type))
            out.append(c)
        else:
            s: SnapshotObservation = obj
            kind = "current" if s.source_type == "wikipedia_constituents" else "snapshot"
            for r in s.rows:
                comp, _ = reg.resolve_named(r.ticker, r.name, on, s.source_type, kind)
                if kind == "current" and r.cik:
                    comp.cik = comp.cik or r.cik
    # attach "date added" claims to the companies holding the ticker today
    for c in dated_added:
        comp = reg.by_ticker_continuity(c.ticker, date.today(), c.name) or reg.by_key(c.name)
        if comp is None:
            comp, _ = reg.resolve_named(c.ticker, c.name, date.today(), c.source_type, "current")
        c.company_id = comp.company_id
        comp.claims.append((c.effective_date, ADD, c.source_type))
        out.append(c)
    # canonicalise ids (merges may have happened)
    for c in out:
        if c.company_id:
            c.company_id = reg.canonical(c.company_id)
    return out


# --------------------------------------------------------------------------
# Phase 2
# --------------------------------------------------------------------------
def union_intervals(ivs: list[ReferenceInterval], tol_days: int = CLUSTER_TOL_DAYS) -> list[tuple[date, Optional[date], str, str]]:
    """Merge overlapping/adjacent reference intervals of one company.

    The reference back-fills renamed tickers, so one company can carry two
    overlapping ticker intervals (AABA and YHOO for Yahoo). Membership is the
    union. Returns (start, end, start_ticker, end_ticker)."""
    out: list[list] = []
    for iv in sorted(ivs, key=lambda i: (i.start, i.end or date.max)):
        if out and (out[-1][1] is None or iv.start <= out[-1][1] + timedelta(days=tol_days)):
            cur = out[-1]
            if iv.end is None or (cur[1] is not None and iv.end > cur[1]):
                cur[1], cur[3] = iv.end, iv.ticker
        else:
            out.append([iv.start, iv.end, iv.ticker, iv.ticker])
    return [tuple(x) for x in out]


def resolve_reference(cands: list[Candidate], intervals: list[ReferenceInterval], reg: CompanyRegistry, wiki_resolved: list[Candidate]) -> tuple[list[Candidate], list[Candidate], dict[str, list[ReferenceInterval]]]:
    """Phase 2: assign every reference interval to a company, then emit one
    ADD/REMOVE candidate pair per *union* of a company's intervals."""
    by_key: dict[tuple[date, str, str], Candidate] = {(c.effective_date, c.action, c.ticker): c for c in cands}
    template = cands[0]
    ref_adds_by_date: dict[date, set[str]] = defaultdict(set)
    ref_removes_by_date: dict[date, set[str]] = defaultdict(set)
    for iv in intervals:
        ref_adds_by_date[iv.start].add(iv.ticker)
        if iv.end:
            ref_removes_by_date[iv.end].add(iv.ticker)
    wiki_by_date: dict[tuple[str, date], list[Candidate]] = defaultdict(list)
    for c in wiki_resolved:
        if c.action in (ADD, REMOVE) and c.source_type in ("wikipedia_changes", "curated"):
            wiki_by_date[(c.action, c.effective_date)].append(c)

    def ref_has_ticker_near(ticker: str, d: date, table: dict[date, set[str]], tol: int = CLUSTER_TOL_DAYS) -> bool:
        return any(ticker in table.get(d + timedelta(days=k), ()) for k in range(-tol, tol + 1))

    def wiki_near(action: str, d: date, tol: int = PAIR_TOL_DAYS, ticker: Optional[str] = None) -> list[Candidate]:
        out = []
        for k in range(-tol, tol + 1):
            out += [c for c in wiki_by_date.get((action, d + timedelta(days=k)), []) if ticker is None or c.ticker == ticker]
        return out

    def synth_tc(d: date, old: str, new: str, comp: Company, reason: str, note: str) -> Candidate:
        return Candidate(effective_date=d, action=TICKER_CHANGE, ticker=new, name=comp.canonical_name, source_type="reference_fja05680", source_url=template.source_url, scraped_at=template.scraped_at, confidence=0.6, reason=reason, reason_category=ReasonCategory.TICKER_CHANGE, counterpart_ticker=old, company_id=comp.company_id, raw_sha256=template.raw_sha256, notes=note)

    # A reference interval for a *new* ticker that starts long before the curated
    # change date is a back-fill over a predecessor (CB before ACE->CB in 2016 was
    # The Chubb Corporation). Split it at the change date; the earlier part cannot
    # belong to the company that adopted the ticker.
    forbid: dict[int, str] = {}  # id(interval) -> company_id excluded
    split: list[ReferenceInterval] = []
    for iv in intervals:
        tc = next((t for t in reg.curated.ticker_changes if t.new_ticker == iv.ticker and iv.start < t.effective_date - timedelta(days=45) and (iv.end is None or iv.end > t.effective_date)), None)
        if tc is None:
            split.append(iv)
            continue
        target = reg.by_ticker_continuity(tc.new_ticker, tc.effective_date + timedelta(days=CONTINUITY_DAYS)) or reg.by_key(tc.company_name)
        head = ReferenceInterval(iv.ticker, iv.start, tc.effective_date)
        tail = ReferenceInterval(iv.ticker, tc.effective_date, iv.end)
        if target is not None:
            forbid[id(head)] = target.company_id
        split += [head, tail]
        reg.log.append({"op": "split_reference_interval", "ticker": iv.ticker, "at": tc.effective_date.isoformat(), "reason": f"curated ticker change {tc.old_ticker}->{tc.new_ticker}"})
    intervals = split
    by_key.update({(iv.start, ADD, iv.ticker): by_key.get((iv.start, ADD, iv.ticker)) for iv in intervals if (iv.start, ADD, iv.ticker) in by_key})

    assignment: dict[str, list[ReferenceInterval]] = defaultdict(list)
    synthetic: list[Candidate] = []
    for iv in sorted(intervals, key=lambda i: (i.start, i.ticker)):
        comp, rule = reg.resolve_reference_interval(iv.ticker, iv.start, iv.end, exclude=forbid.get(id(iv)))
        if comp is None and iv.ticker.endswith("Q") and len(iv.ticker) >= 4 and "." not in iv.ticker:
            # Delisting convention in the reference (Norgate): a trailing "Q" marks a
            # bankrupt issuer (CITGQ = CIT Group). Try the base ticker.
            comp, rule0 = reg.resolve_reference_interval(iv.ticker[:-1], iv.start, iv.end)
            if comp is not None:
                rule = f"bankruptcy_suffix_q ({rule0})"
        if comp is None:
            # H3: Wikipedia ADD on the same date under a ticker the reference never adds near then.
            for wc in wiki_near(ADD, iv.start):
                if wc.company_id and wc.ticker != iv.ticker and not ref_has_ticker_near(wc.ticker, iv.start, ref_adds_by_date):
                    cand = reg.get(wc.company_id)
                    if not cand.membership_excludes(iv.start, iv.end) and not assignment.get(cand.company_id):
                        comp, rule = cand, "pair_add_date"
                        break
        if comp is None and iv.end is not None:
            # H2: Wikipedia REMOVE on the end date under a ticker the reference never removes near then.
            for wc in wiki_near(REMOVE, iv.end):
                if wc.company_id and wc.ticker != iv.ticker and not ref_has_ticker_near(wc.ticker, iv.end, ref_removes_by_date):
                    cand = reg.get(wc.company_id)
                    if not cand.membership_excludes(iv.start, iv.end) and not any(a.end == iv.end for a in assignment.get(cand.company_id, [])):
                        comp, rule = cand, "pair_remove_date"
                        break
        if comp is None and iv.end is not None:
            # H1: same-day swap X->Y where Y's company was demonstrably a member long before: a rename.
            for y in sorted(ref_adds_by_date.get(iv.end, ())):
                if y == iv.ticker:
                    continue
                cy, _ = reg.resolve_reference_interval(y, iv.end, None)
                if cy is None:
                    continue
                fa = cy.first_add()
                if fa is not None and fa <= iv.end - timedelta(days=RENAME_MIN_PRIOR_DAYS) and not cy.removed_between(fa, iv.end):
                    comp, rule = cy, "inferred_rename_same_day_swap"
                    synthetic.append(synth_tc(iv.end, iv.ticker, y, comp, f"Ticker changed from {iv.ticker} to {y} (inferred: same-day swap in reference data; company a member since {fa}).", "inferred ticker change (H1)"))
                    break
        if comp is None:
            comp = reg.create("", iv.ticker, iv.start, "reference_fja05680", "reference", needs_review=True)
            rule = f"new_unknown_company ({rule})"
        comp.add_evidence(iv.ticker, iv.start, "reference")
        if iv.end:
            comp.add_evidence(iv.ticker, iv.end, "reference")
        assignment[comp.company_id].append(iv)
        reg.log.append({"op": "resolve_reference", "ticker": iv.ticker, "start": iv.start.isoformat(), "end": iv.end.isoformat() if iv.end else None, "company_id": comp.company_id, "name": comp.canonical_name, "rule": rule})

    # H4: reference same-day swaps X->Y resolved to *different* companies A, B where
    # Wikipedia records neither a removal of X nor an addition of Y near that date:
    # a rename the named sources missed (BB&T -> Truist). Merge A into B.
    for d, removed in sorted(ref_removes_by_date.items()):
        added = ref_adds_by_date.get(d, set())
        if len(removed) != 1 or len(added) != 1:
            continue
        (x,), (y,) = removed, added
        a = next((reg.get(cid) for cid, ivs in assignment.items() if any(iv.ticker == x and iv.end == d for iv in ivs)), None)
        b = next((reg.get(cid) for cid, ivs in assignment.items() if any(iv.ticker == y and iv.start == d for iv in ivs)), None)
        if a is None or b is None or a is b:
            continue
        if wiki_near(REMOVE, d, CLUSTER_TOL_DAYS, ticker=x) or wiki_near(ADD, d, CLUSTER_TOL_DAYS, ticker=y):
            continue  # Wikipedia documents a real replacement involving these tickers
        fa = b.first_add()
        names_ok = reg._names_compatible(a, b.canonical_name) or reg._names_compatible(b, a.canonical_name)
        if not ((fa is not None and fa <= d - timedelta(days=RENAME_MIN_PRIOR_DAYS)) or names_ok):
            continue
        winner = reg.merge(a, b, "reference_swap_rename")
        if a.company_id in assignment:
            assignment.setdefault(winner.company_id, []).extend(assignment.pop(a.company_id))
        synthetic.append(synth_tc(d, x, y, winner, f"Ticker changed from {x} to {y} (inferred: same-day swap in reference data with no add/remove recorded by Wikipedia; treated as a rename).", "inferred ticker change (H4)"))
        reg.log.append({"op": "ticker_change", "company_id": winner.company_id, "old": x, "new": y, "date": d.isoformat(), "rule": "reference_swap_rename"})

    # H5: back-filled rename. Company A is seen in dated page snapshots under ticker X
    # but the reference never lists X; company B holds a reference interval covering
    # A's whole sighting period under ticker Y yet is never seen in a snapshot during
    # that period, and B's first sighting comes after A's last. The reference wrote
    # B's later ticker over the whole lineage (Coach -> Tapestry): merge A into B.
    reliable = ("snapshot", "remove", "current", "curated")
    ref_tickers = {iv.ticker for iv in intervals}
    seen: dict[str, list[date]] = defaultdict(list)  # ticker -> snapshot dates it was seen under, any company
    for c in reg.live():
        for t, ev in c.evidence.items():
            seen[t] += [d for d, _, k in ev if k in ("snapshot", "current")]
    for a in list(reg.live()):
        if a.company_id in assignment or a.needs_review or a.last_remove() is not None:
            continue  # a removed company cannot have been renamed into a continuing member
        a_ev = sorted((d, t) for t, ev in a.evidence.items() for d, _, k in ev if k in reliable)
        if not a_ev or any(t in ref_tickers for _, t in a_ev):
            continue
        a_first, a_last = a_ev[0][0], a_ev[-1][0]
        cands = []
        for b_id, ivs in assignment.items():
            b = reg.get(b_id)
            if b is a or b.needs_review:
                continue
            covering = [iv for iv in ivs if iv.start <= a_first and (iv.end is None or iv.end >= a_last)]
            if not covering:
                continue
            y = covering[0].ticker
            if any(a_first <= d <= a_last for d in seen.get(y, [])):
                continue  # Y was listed by someone during A's period: not a hidden rename of A
            b_ev = sorted(d for ev in b.evidence.values() for d, _, k in ev if k in reliable)
            if not b_ev or b_ev[0] <= a_last:
                continue
            gap = (b_ev[0] - a_last).days
            if gap <= CONTINUITY_DAYS + 31:
                cands.append((gap, b, y))
        if len(cands) != 1:
            if len(cands) > 1:
                reg.log.append({"op": "h5_ambiguous", "company_id": a.company_id, "name": a.canonical_name, "candidates": [b.company_id for _, b, _ in cands]})
            continue
        gap, b, y = cands[0]
        x = a_ev[-1][1]
        winner = reg.merge(a, b, "backfilled_rename")
        synthetic.append(synth_tc(a_last + timedelta(days=1), x, y, winner, f"Ticker changed from {x} to {y} between {a_last} and {a_last + timedelta(days=gap)} (inferred: reference lists {y} for the whole lineage while dated page snapshots show {x}; exact date unknown).", "inferred ticker change (H5)"))
        reg.log.append({"op": "ticker_change", "company_id": winner.company_id, "old": x, "new": y, "date": (a_last + timedelta(days=1)).isoformat(), "rule": "backfilled_rename", "gap_days": gap})

    # Emit candidates from the union of each company's intervals.
    out: list[Candidate] = []
    first_date = min(iv.start for iv in intervals)
    for cid, ivs in assignment.items():
        cid = reg.canonical(cid)
        for start, end, t0, t1 in union_intervals(ivs):
            key = (start, ADD, t0)
            base = by_key.get(key)
            if base is None:  # union changed the boundary ticker; find any ADD on that date for this company
                base = next((by_key[k] for k in by_key if k[0] == start and k[1] == ADD and k[2] in {i.ticker for i in ivs}), None)
            c = Candidate(**{**base.__dict__}) if base else Candidate(effective_date=start, action=ADD, ticker=t0, name="", source_type="reference_fja05680", source_url=template.source_url, scraped_at=template.scraped_at, confidence=template.confidence, raw_sha256=template.raw_sha256)
            c.company_id = cid
            c.notes = (c.notes + "; union of reference intervals").strip("; ")
            out.append(c)
            if end is not None:
                base = by_key.get((end, REMOVE, t1)) or next((by_key[k] for k in by_key if k[0] == end and k[1] == REMOVE and k[2] in {i.ticker for i in ivs}), None)
                c = Candidate(**{**base.__dict__}) if base else Candidate(effective_date=end, action=REMOVE, ticker=t1, name="", source_type="reference_fja05680", source_url=template.source_url, scraped_at=template.scraped_at, confidence=template.confidence, raw_sha256=template.raw_sha256)
                c.company_id = cid
                c.notes = (c.notes + "; union of reference intervals").strip("; ")
                out.append(c)
    return out, synthetic, {reg.canonical(k): v for k, v in assignment.items()}


# --------------------------------------------------------------------------
# Phase 3
# --------------------------------------------------------------------------
def cluster_and_finalize(cands: list[Candidate], reg: CompanyRegistry, window_start: date, window_end: date) -> list[Event]:
    groups: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for c in cands:
        if c.company_id is None:
            continue
        c.company_id = reg.canonical(c.company_id)
        groups[(c.company_id, c.action)].append(c)

    events: list[Event] = []
    for (cid, action), lst in groups.items():
        lst.sort(key=lambda c: (c.effective_date, -c.confidence))
        clusters: list[list[Candidate]] = []
        for c in lst:
            if clusters and (c.effective_date - clusters[-1][0].effective_date).days <= CLUSTER_TOL_DAYS:
                clusters[-1].append(c)
            else:
                clusters.append([c])
        comp = reg.get(cid)
        for cl in clusters:
            primary = max(cl, key=lambda c: (c.confidence, c.source_type != "reference_fja05680", bool(c.name)))
            others = sorted({c.source_type for c in cl if c.source_type != primary.source_type})
            conf = min(0.99, primary.confidence + 0.05 * len(others))
            name = primary.name or next((c.name for c in cl if c.name), "") or comp.canonical_name
            reason = primary.reason or next((c.reason for c in cl if c.reason), "")
            cat = primary.reason_category
            if cat == ReasonCategory.OTHER_UNKNOWN:
                cat = next((c.reason_category for c in cl if c.reason_category != ReasonCategory.OTHER_UNKNOWN), cat)
            counterpart = None
            if primary.counterpart_ticker and action in (ADD, REMOVE):
                other = reg.by_ticker_continuity(primary.counterpart_ticker, primary.effective_date)
                counterpart = other.company_id if other else None
            notes = primary.notes
            if others:
                notes += f"; corroborated by {', '.join(others)}"
                other_dates = sorted({c.effective_date.isoformat() for c in cl if c.effective_date != primary.effective_date})
                if other_dates:
                    notes += f"; other sources dated {', '.join(other_dates)}"
            ticker = primary.ticker
            if primary.source_type == "wikipedia_constituents":
                # today's ticker is not necessarily the ticker on the entry date
                t_then = comp.ticker_on(primary.effective_date)
                if t_then and t_then != ticker:
                    notes += f"; ticker at event inferred as {t_then} (today's ticker {ticker})"
                    ticker = t_then
            events.append(Event(event_id=deterministic_event_id(cid, primary.effective_date, action, ticker), effective_date=primary.effective_date, action=action, company_id=cid, ticker_at_event=ticker, company_name=name, reason=reason, reason_category=cat.value if isinstance(cat, ReasonCategory) else str(cat), source_url=primary.source_url, source_type=primary.source_type, scraped_at=primary.scraped_at, confidence=round(conf, 3), counterpart_company_id=counterpart, corroborating_sources=others, refs=list(primary.refs), raw_sha256=primary.raw_sha256, notes=notes.strip("; "), in_window=window_start <= primary.effective_date <= window_end))
    events.sort(key=lambda e: (e.effective_date, e.company_id, e.action))
    return events


# --------------------------------------------------------------------------
# Phase 4
# --------------------------------------------------------------------------
def derive_intervals(events: list[Event], reg: CompanyRegistry, ref_assignment: dict[str, list[ReferenceInterval]]) -> tuple[list[Interval], list[Issue], list[Event]]:
    by_company: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_company[e.company_id].append(e)
    intervals: list[Interval] = []
    issues: list[Issue] = []
    kept: list[Event] = []
    for cid, evs in by_company.items():
        evs.sort(key=lambda e: (e.effective_date, {REMOVE: 0, TICKER_CHANGE: 1, ADD: 2}[e.action]))
        open_iv: Optional[Interval] = None
        for e in evs:
            if e.action == TICKER_CHANGE:
                kept.append(e)
                continue
            if e.action == ADD:
                if open_iv is not None:
                    earliest_ref = min((r.start for r in ref_assignment.get(cid, [])), default=None)
                    if open_iv.entry_date is not None and e.source_type == "wikipedia_changes" and open_iv.entry_source == "wikipedia_constituents" and (earliest_ref is None or earliest_ref >= e.effective_date - timedelta(days=CLUSTER_TOL_DAYS)):
                        # The dated, sourced changes-table ADD supersedes the "Date added"
                        # column, which editors often leave at a predecessor's date.
                        issues.append(Issue(cid, "entry_date_superseded", e.effective_date, f"'Date added' column said {open_iv.entry_date}; changes table records an ADD on {e.effective_date} with no removal in between; using the changes-table date"))
                        kept[:] = [k for k in kept if k.event_id != open_iv.entry_event_id]
                        open_iv.entry_date, open_iv.entry_event_id, open_iv.entry_ticker, open_iv.entry_source = e.effective_date, e.event_id, e.ticker_at_event, e.source_type
                        kept.append(e)
                        continue
                    if open_iv.entry_date is None:
                        open_iv.entry_date, open_iv.entry_event_id, open_iv.entry_ticker, open_iv.left_censored = e.effective_date, e.event_id, e.ticker_at_event, False
                        kept.append(e)
                    elif open_iv.left_censored:
                        issues.append(Issue(cid, "entry_after_opening", e.effective_date, f"{e.source_type} says added {e.effective_date}; reference shows membership from its start ({open_iv.entry_date}); kept the earlier, left-censored start"))
                    else:
                        issues.append(Issue(cid, "conflicting_entry_date" if e.source_type != "reference_fja05680" else "duplicate_add", e.effective_date, f"already a member since {open_iv.entry_date}; ADD from {e.source_type} ignored"))
                    continue
                open_iv = Interval(cid, e.effective_date, None, e.event_id, None, e.ticker_at_event, e.ticker_at_event, left_censored="left-censored" in e.notes, entry_source=e.source_type)
                kept.append(e)
            elif e.action == REMOVE:
                if open_iv is None:
                    prev_exit = max((iv.exit_date for iv in intervals if iv.company_id == cid and iv.exit_date), default=None)
                    issues.append(Issue(cid, "remove_without_add", e.effective_date, f"REMOVE from {e.source_type} with no prior ADD; interval left-censored" + (f" (bounded below by previous exit {prev_exit})" if prev_exit else "")))
                    open_iv = Interval(cid, prev_exit, None, None, None, e.ticker_at_event, e.ticker_at_event, left_censored=True)
                if open_iv.entry_date == e.effective_date:
                    issues.append(Issue(cid, "same_day_add_remove", e.effective_date, "ADD and REMOVE on the same date; zero-length membership kept for transparency"))
                open_iv.exit_date, open_iv.exit_event_id, open_iv.exit_ticker, open_iv.exit_reason_category = e.effective_date, e.event_id, e.ticker_at_event, e.reason_category
                intervals.append(open_iv)
                kept.append(e)
                open_iv = None
        if open_iv is not None:
            intervals.append(open_iv)
    # support flag
    for iv in intervals:
        refs = ref_assignment.get(iv.company_id, [])
        tol = timedelta(days=CLUSTER_TOL_DAYS)
        a = (iv.entry_date - tol) if iv.entry_date else date.min
        b = (iv.exit_date + tol) if iv.exit_date else date.max
        iv.supported = any(r.start <= b and (r.end is None or r.end >= a) for r in refs)
        if not iv.supported:
            issues.append(Issue(iv.company_id, "unsupported_interval", iv.entry_date, f"membership {iv.entry_date}..{iv.exit_date or 'present'} ({iv.entry_ticker}) has no overlap with any reference-dataset interval; excluded from point-in-time queries by default"))
    intervals.sort(key=lambda i: (i.entry_date or date.min, i.company_id))
    kept.sort(key=lambda e: (e.effective_date, e.company_id, e.action))
    return intervals, issues, kept


def apply_overrides(cands: list[Candidate], reg: CompanyRegistry, source_url_default: str = "data/curated/event_overrides.csv") -> list[Candidate]:
    """Apply curated event overrides (mode=suppress drops a scraped row; mode=add injects a verified event)."""
    out = list(cands)
    for ov in reg.curated.overrides:
        d = date.fromisoformat(ov["effective_date"])
        act = ov["action"].upper()
        t = ov["ticker"].upper()
        if ov["mode"] == "suppress":
            before = len(out)
            out = [c for c in out if not (c.effective_date == d and c.action == act and c.ticker == t and c.source_type == "wikipedia_changes")]
            reg.log.append({"op": "override_suppress", "date": ov["effective_date"], "action": act, "ticker": t, "n_removed": before - len(out), "source": ov.get("source_url", "")})
        elif ov["mode"] == "add":
            out.append(Candidate(effective_date=d, action=act, ticker=t, name=ov["company_name"], source_type="curated", source_url=ov.get("source_url") or source_url_default, scraped_at="", confidence=float(ov.get("confidence") or 0.99), reason=ov.get("reason", ""), reason_category=classify_reason(ov.get("reason", "")), notes="curated override: " + ov.get("note", "")))
            reg.log.append({"op": "override_add", "date": ov["effective_date"], "action": act, "ticker": t, "name": ov["company_name"], "source": ov.get("source_url", "")})
    return out


def seed_curated_ticker_changes(reg: CompanyRegistry, template: Optional[Candidate]) -> list[Candidate]:
    """After named resolution, make curated ticker changes visible as evidence and events
    even when no Wikipedia row mentions them (SBC -> T in 2005)."""
    synthetic: list[Candidate] = []
    for tc in reg.curated.ticker_changes:
        comp = reg.by_ticker_continuity(tc.new_ticker, tc.effective_date + timedelta(days=CONTINUITY_DAYS)) or reg.by_key(tc.company_name)
        if comp is None:
            continue
        already = any(l.get("op") == "ticker_change" and l.get("old") == tc.old_ticker and l.get("new") == tc.new_ticker for l in reg.log)
        holder = reg.by_ticker_continuity(tc.old_ticker, tc.effective_date - timedelta(days=1))
        if holder is not None and holder is not comp:
            # The old ticker's company (built from page snapshots) is the same legal
            # entity as the curated target: fold it in.
            comp = reg.merge(holder, comp, f"curated_ticker_change {tc.old_ticker}->{tc.new_ticker}")
        comp.add_evidence(tc.old_ticker, tc.effective_date - timedelta(days=1), "curated")
        comp.add_evidence(tc.new_ticker, tc.effective_date, "curated")
        if not already:
            reg.log.append({"op": "ticker_change", "company_id": comp.company_id, "old": tc.old_ticker, "new": tc.new_ticker, "date": tc.effective_date.isoformat(), "rule": "curated_seed", "source": tc.source_url})
            synthetic.append(Candidate(effective_date=tc.effective_date, action=TICKER_CHANGE, ticker=tc.new_ticker, name=tc.company_name, source_type="curated", source_url=tc.source_url, scraped_at="", confidence=0.99, reason=f"Ticker changed from {tc.old_ticker} to {tc.new_ticker} (curated).", reason_category=ReasonCategory.TICKER_CHANGE, counterpart_ticker=tc.old_ticker, company_id=comp.company_id, notes="curated ticker change"))
    return synthetic


def build(wiki_candidates: list[Candidate], snapshots: list[SnapshotObservation], ref_candidates: list[Candidate], ref_intervals: list[ReferenceInterval], reg: CompanyRegistry, window_start: date, window_end: date) -> BuildResult:
    wiki_candidates = apply_overrides(wiki_candidates, reg)
    wiki_resolved = resolve_named(wiki_candidates, snapshots, reg)
    wiki_resolved += seed_curated_ticker_changes(reg, wiki_candidates[0] if wiki_candidates else None)
    if ref_candidates:
        ref_resolved, synthetic, assignment = resolve_reference(ref_candidates, ref_intervals, reg, wiki_resolved)
    else:
        ref_resolved, synthetic, assignment = [], [], {}
    reg.finalize_ticker_spans(date.today())
    all_c = wiki_resolved + ref_resolved + synthetic
    tc_dates = {(reg.canonical(c.company_id), c.effective_date) for c in synthetic}
    for c in ref_resolved:
        if (reg.canonical(c.company_id), c.effective_date) in tc_dates and c.action in (ADD, REMOVE):
            c.action = TICKER_CHANGE
            c.reason_category = ReasonCategory.TICKER_CHANGE
    events = cluster_and_finalize(all_c, reg, window_start, window_end)
    assignment = {reg.canonical(k): v for k, v in assignment.items()}
    intervals, issues, kept = derive_intervals(events, reg, assignment if ref_candidates else defaultdict(list))
    if not ref_candidates:
        for iv in intervals:
            iv.supported = True
        issues = [i for i in issues if i.kind != "unsupported_interval"]
    return BuildResult(reg, kept, intervals, issues, all_c, assignment)


def constituents_on(intervals: list[Interval], d: date, include_unsupported: bool = False) -> list[Interval]:
    """Point-in-time membership: intervals active on ``d`` (entry <= d < exit)."""
    return [iv for iv in intervals if (include_unsupported or iv.supported) and (iv.entry_date is None or iv.entry_date <= d) and (iv.exit_date is None or d < iv.exit_date)]
