"""Entity resolution: map (ticker, name, date) observations to persistent ``company_id``s.

Why this exists
---------------
Tickers are re-used (WM: Washington Mutual until 2008, Waste Management after),
companies rename (Facebook -> Meta), change tickers without leaving (FB -> META),
leave and re-enter, and spin-offs can produce a *new* company with an *old*
name (Alcoa Corp, 2016). Worse, both public sources back-fill: the Wikipedia
changes table often shows a company's *later* ticker/name on an old row
("ACT Actavis" added in 1999 when it was Watson Pharmaceuticals), and the
reference dataset shows the *last* ticker of a lineage for its whole history
(AABA for Yahoo from 1999). A ticker is therefore never used as a primary key.

Model
-----
Each company accumulates:

* **names** with first-seen dates,
* **ticker evidence**: ``ticker -> [(date, weight, kind)]`` – dated proof that
  the company traded under that ticker. Kinds: ``remove`` (ticker on the
  removal row), ``snapshot`` (row in a dated page revision), ``current``
  (today's list), ``add`` (ticker on an add row – weight 0.5 because old add
  rows are frequently back-filled), ``reference``.
* **membership claims**: ``(date, ADD|REMOVE, source)`` from named sources.

Resolution of a named observation (Wikipedia rows), in order:

1. curated alias -> canonical company key (``data/curated/company_aliases.csv``);
2. curated ticker change (old -> new on date) = same company;
3. same-row ticker change (reason says so and names agree);
4. **ticker continuity**: a company with evidence for this ticker within
   ``CONTINUITY_DAYS`` and no REMOVE claim in between;
5. name-key equality (unless listed in ``distinct_entities.csv``);
6. create a new company.

Resolution of a name-less reference interval ``[start, end]`` for ticker T:
candidates are companies with any evidence for T whose membership claims do
not exclude the interval (a final REMOVE before ``start`` or a first ADD long
after ``end`` excludes); the candidate with the most evidence weight *inside*
the interval wins. Unresolved intervals go through date-pairing heuristics in
``membership.py`` and finally become flagged ``needs_review`` companies.

``company_id`` is deterministic: ``C`` + sha1(canonical key)[:10].
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from snp500.normalize.text import normalize_name, normalize_ticker, parse_date

CONTINUITY_DAYS = 400  # annual snapshots are ~365 days apart
MEMBERSHIP_TOL_DAYS = 45

EVIDENCE_WEIGHT = {"remove": 1.0, "snapshot": 1.0, "current": 1.0, "add": 0.5, "reference": 0.8, "curated": 1.0}


@dataclass
class TickerSpan:
    ticker: str
    valid_from: Optional[date]
    valid_to: Optional[date]
    source: str


@dataclass
class Company:
    company_id: str
    canonical_name: str
    name_key: str
    names: dict[str, date] = field(default_factory=dict)
    evidence: dict[str, list[tuple[date, float, str]]] = field(default_factory=dict)
    claims: list[tuple[date, str, str]] = field(default_factory=list)  # (date, ADD|REMOVE, source)
    tickers: list[TickerSpan] = field(default_factory=list)  # derived by finalize()
    cik: Optional[str] = None
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)
    merged_into: Optional[str] = None

    # -- evidence helpers --------------------------------------------------
    def add_evidence(self, ticker: str, on: date, kind: str) -> None:
        self.evidence.setdefault(ticker, []).append((on, EVIDENCE_WEIGHT[kind], kind))

    def has_ticker(self, t: str) -> bool:
        return t in self.evidence

    def all_tickers(self) -> list[str]:
        return list(self.evidence)

    def evidence_dates(self, ticker: str) -> list[date]:
        return [d for d, _, _ in self.evidence.get(ticker, [])]

    def tickers_on(self, d: date) -> list[str]:
        """All tickers under which the company traded on ``d`` (several for multi-class listings)."""
        return [s.ticker for s in self.tickers if (s.valid_from or date.min) <= d <= (s.valid_to or date.max)]

    def ticker_on(self, d: date) -> Optional[str]:
        ts = self.tickers_on(d)
        return ts[0] if ts else None

    def first_add(self) -> Optional[date]:
        ds = [d for d, a, _ in self.claims if a == "ADD"]
        return min(ds) if ds else None

    def last_remove(self) -> Optional[date]:
        ds = [d for d, a, _ in self.claims if a == "REMOVE"]
        return max(ds) if ds else None

    def removed_between(self, a: date, b: date) -> bool:
        """A REMOVE claim in [a, b)."""
        return any(act == "REMOVE" and a <= d < b for d, act, _ in self.claims)

    def membership_excludes(self, start: date, end: Optional[date]) -> bool:
        """True if named-source claims make membership over [start, end] impossible."""
        adds = sorted(d for d, a, _ in self.claims if a == "ADD")
        removes = sorted(d for d, a, _ in self.claims if a == "REMOVE")
        # interval entirely after the final REMOVE with no later ADD
        if removes:
            last_r = removes[-1]
            if start > last_r + timedelta(days=MEMBERSHIP_TOL_DAYS) and not any(a > last_r for a in adds):
                return True
        # interval entirely before the first ADD with no earlier REMOVE
        if adds and end is not None:
            first_a = adds[0]
            if end < first_a - timedelta(days=MEMBERSHIP_TOL_DAYS) and not any(r < first_a for r in removes):
                return True
        return False

    def evidence_inside(self, ticker: str, start: date, end: Optional[date]) -> float:
        e = end or date.max
        return sum(w for d, w, _ in self.evidence.get(ticker, []) if start - timedelta(days=MEMBERSHIP_TOL_DAYS) <= d <= e + timedelta(days=MEMBERSHIP_TOL_DAYS)) if end else sum(
            w for d, w, _ in self.evidence.get(ticker, []) if d >= start - timedelta(days=MEMBERSHIP_TOL_DAYS)
        )


def make_company_id(key: str) -> str:
    return "C" + hashlib.sha1(key.encode()).hexdigest()[:10]


@dataclass
class CuratedTickerChange:
    old_ticker: str
    new_ticker: str
    effective_date: date
    company_name: str
    source_url: str


class Curated:
    """Hand-maintained resolution hints, each with a citation. See data/curated/README.md."""

    def __init__(self, aliases: dict[str, str], ticker_changes: list[CuratedTickerChange], distinct: set[str], entity_keys: Optional[dict[str, str]] = None, overrides: Optional[list[dict]] = None):
        self.aliases = aliases
        self.ticker_changes = ticker_changes
        self.distinct = distinct
        self.entity_keys = entity_keys or {}  # canonical display name -> explicit key
        self.overrides = overrides or []

    @classmethod
    def load(cls, curated_dir: Path) -> "Curated":
        aliases: dict[str, str] = {}
        entity_keys: dict[str, str] = {}
        p = curated_dir / "company_aliases.csv"
        if p.exists():
            with p.open() as fh:
                for row in csv.DictReader(fh):
                    canon = row["canonical_name"].strip()
                    # exact (case-insensitive) alias first, normalised form as fallback;
                    # "AT&T" and "AT&T Corporation" normalise identically but are different entities
                    aliases["exact:" + row["alias_name"].strip().lower()] = canon
                    aliases.setdefault("norm:" + normalize_name(row["alias_name"]), canon)
                    if (row.get("entity_key") or "").strip():
                        entity_keys[canon] = row["entity_key"].strip()
        overrides: list[dict] = []
        p = curated_dir / "event_overrides.csv"
        if p.exists():
            with p.open() as fh:
                for row in csv.DictReader(fh):
                    if (row.get("mode") or "").strip():
                        overrides.append({k: (v or "").strip() for k, v in row.items()})
        changes: list[CuratedTickerChange] = []
        p = curated_dir / "ticker_changes.csv"
        if p.exists():
            with p.open() as fh:
                for row in csv.DictReader(fh):
                    changes.append(CuratedTickerChange(normalize_ticker(row["old_ticker"]), normalize_ticker(row["new_ticker"]), parse_date(row["effective_date"]), row["company_name"].strip(), row.get("source_url", "").strip()))
        distinct: set[str] = set()
        p = curated_dir / "distinct_entities.csv"
        if p.exists():
            with p.open() as fh:
                for row in csv.DictReader(fh):
                    distinct.add(normalize_name(row["name"]))
        return cls(aliases, changes, distinct, entity_keys, overrides)

    @classmethod
    def empty(cls) -> "Curated":
        return cls({}, [], set())

    def canonical_for(self, name: str) -> Optional[str]:
        return self.aliases.get("exact:" + name.strip().lower()) or self.aliases.get("norm:" + normalize_name(name))

    def ticker_change_for(self, new_ticker: str, on: date, tol_days: int = 45) -> Optional[CuratedTickerChange]:
        for c in self.ticker_changes:
            if c.new_ticker == new_ticker and abs((c.effective_date - on).days) <= tol_days:
                return c
        return None

    def ticker_change_from(self, old_ticker: str, on: date, tol_days: int = 45) -> Optional[CuratedTickerChange]:
        for c in self.ticker_changes:
            if c.old_ticker == old_ticker and abs((c.effective_date - on).days) <= tol_days:
                return c
        return None


class CompanyRegistry:
    def __init__(self, curated: Optional[Curated] = None):
        self.curated = curated or Curated.empty()
        self.companies: dict[str, Company] = {}
        self._by_key: dict[str, str] = {}
        self.redirect: dict[str, str] = {}  # merged company_id -> surviving company_id
        self.log: list[dict] = []

    # -- basics --------------------------------------------------------------
    def canonical(self, cid: str) -> str:
        while cid in self.redirect:
            cid = self.redirect[cid]
        return cid

    def get(self, cid: str) -> Company:
        return self.companies[self.canonical(cid)]

    def live(self) -> list[Company]:
        return [c for c in self.companies.values() if c.merged_into is None]

    def _key(self, name: str) -> tuple[str, str]:
        canon = self.curated.canonical_for(name) or name
        return canon, self.curated.entity_keys.get(canon) or normalize_name(canon)

    def by_key(self, name: str) -> Optional[Company]:
        _, key = self._key(name)
        cid = self._by_key.get(key)
        return self.get(cid) if cid else None

    def create(self, name: str, ticker: str, on: date, source: str, kind: str, needs_review: bool = False) -> Company:
        if name:
            canon, key = self._key(name)
        else:
            canon, key = f"Unknown company (ticker {ticker})", f"unknown-ticker:{ticker}:{on.isoformat()}"
        cid = make_company_id(key)
        if cid in self.companies:
            c = self.get(cid)
            c.add_evidence(ticker, on, kind)
            return c
        c = Company(company_id=cid, canonical_name=canon, name_key=key, needs_review=needs_review)
        if name:
            c.names[name] = on
        c.add_evidence(ticker, on, kind)
        self.companies[cid] = c
        self._by_key[key] = cid
        self.log.append({"op": "create", "company_id": cid, "name": canon, "ticker": ticker, "date": on.isoformat(), "source": source, "kind": kind})
        return c

    def merge(self, loser: Company, winner: Company, rule: str) -> Company:
        """Fold ``loser`` into ``winner`` (names, evidence, claims). Deterministic and logged."""
        loser, winner = self.get(loser.company_id), self.get(winner.company_id)
        if loser is winner:
            return winner
        for n, d in loser.names.items():
            winner.names.setdefault(n, d)
        for t, ev in loser.evidence.items():
            winner.evidence.setdefault(t, []).extend(ev)
        winner.claims.extend(loser.claims)
        winner.notes.extend(loser.notes)
        winner.notes.append(f"absorbed {loser.company_id} ({loser.canonical_name}) by {rule}")
        winner.cik = winner.cik or loser.cik
        loser.merged_into = winner.company_id
        self.redirect[loser.company_id] = winner.company_id
        self._by_key[loser.name_key] = winner.company_id
        self.log.append({"op": "merge", "loser": loser.company_id, "loser_name": loser.canonical_name, "winner": winner.company_id, "winner_name": winner.canonical_name, "rule": rule})
        return winner

    # -- rules ---------------------------------------------------------------
    def _names_compatible(self, c: Company, name: str) -> bool:
        if not name:
            return True
        _, key = self._key(name)
        keys = {c.name_key} | {normalize_name(n) for n in c.names}
        if key in keys:
            return True
        return any(key.startswith(k) or k.startswith(key) for k in keys if k)

    def by_ticker_continuity(self, ticker: str, on: date, name: str = "", kind: str = "") -> Optional[Company]:
        """Rule 4: a company evidenced under ``ticker`` within CONTINUITY_DAYS and not removed in between."""
        hits: list[tuple[int, Company]] = []
        for c in self.live():
            best = None
            for d in c.evidence_dates(ticker):
                gap = abs((on - d).days)
                if gap <= CONTINUITY_DAYS:
                    lo, hi = (d, on) if d <= on else (on, d)
                    if not c.removed_between(lo, hi):
                        best = gap if best is None else min(best, gap)
            if best is not None:
                hits.append((best, c))
        if not hits:
            # Fallback: the most recent holder of the ticker, provided it was never
            # removed after that evidence and no other company held the ticker since.
            holders = []
            for c in self.live():
                ds = [d for d in c.evidence_dates(ticker) if d <= on]
                if ds and not c.removed_between(max(ds), on + timedelta(days=1)):
                    holders.append((max(ds), c))
            if len(holders) == 1 and name and self._names_compatible(holders[0][1], name):
                return holders[0][1]
            if len(holders) == 1 and not name:
                return holders[0][1]
            if len(holders) == 1 and kind in ("snapshot", "current") and (on - holders[0][0]).days <= 2 * CONTINUITY_DAYS:
                # A dated page lists ticker T; T's only known holder is a live member last
                # seen within two snapshot periods. Names differ (IntercontinentalExchange
                # vs Intercontinental Exchange) but the ticker trail is unbroken.
                return holders[0][1]
            if len(holders) > 1:
                holders.sort(key=lambda h: (h[0], h[1].company_id), reverse=True)
                if name and self._names_compatible(holders[0][1], name):
                    return holders[0][1]
            return None
        if len(hits) > 1 and name:
            compatible = [h for h in hits if self._names_compatible(h[1], name)]
            if len(compatible) >= 1:
                hits = compatible
        hits.sort(key=lambda h: (h[0], h[1].company_id))
        return hits[0][1]

    def resolve_named(self, ticker: str, name: str, on: date, source: str, kind: str, counterpart_ticker: str = "", reason_is_ticker_change: bool = False) -> tuple[Company, bool]:
        """Resolve a named observation. Returns (company, is_ticker_change)."""
        # Rule 2: curated ticker change
        if kind == "add":
            tc = self.curated.ticker_change_for(ticker, on)
            if tc is not None:
                old = self.by_ticker_continuity(tc.old_ticker, on) or self.by_key(tc.company_name) or (self.by_key(name) if name else None)
                if old is None:
                    old = self.create(name or tc.company_name, tc.old_ticker, on - timedelta(days=1), source, "curated")
                    old.notes.append(f"created via curated ticker change {tc.old_ticker}->{ticker} on {on}")
                if name:
                    old.names.setdefault(name, on)
                old.add_evidence(ticker, on, kind)
                self.log.append({"op": "ticker_change", "company_id": old.company_id, "old": tc.old_ticker, "new": ticker, "date": on.isoformat(), "rule": "curated", "source": tc.source_url})
                return old, True
            # Rule 3: same-row ticker change
            if reason_is_ticker_change and counterpart_ticker:
                old = self.by_ticker_continuity(counterpart_ticker, on, name)
                if old is not None and self._names_compatible(old, name):
                    old.names.setdefault(name, on)
                    old.add_evidence(ticker, on, kind)
                    self.log.append({"op": "ticker_change", "company_id": old.company_id, "old": counterpart_ticker, "new": ticker, "date": on.isoformat(), "rule": "same_row_reason"})
                    return old, True
        # Rule 1: a curated alias is authoritative for identity (it exists precisely
        # because ticker/name heuristics get this company wrong).
        if name and self.curated.canonical_for(name) is not None:
            c = self.by_key(name) or self.create(name, ticker, on, source, kind)
            c.names.setdefault(name, on)
            c.add_evidence(ticker, on, kind)
            self.log.append({"op": "resolve", "company_id": c.company_id, "ticker": ticker, "name": name, "date": on.isoformat(), "kind": kind, "rule": "curated_alias"})
            return c, False
        # Rule 5 (name key) and Rule 4 (continuity)
        c_name = self.by_key(name) if name else None
        c_cont = self.by_ticker_continuity(ticker, on, name, kind)
        chosen: Optional[Company] = None
        rule = ""
        if c_name is not None and normalize_name(name) not in self.curated.distinct:
            chosen, rule = c_name, "name_key"
        if c_cont is not None:
            if chosen is None:
                if kind == "add" and name and not self._names_compatible(c_cont, name):
                    # An ADD under a ticker a differently-named live member holds is a
                    # replacement that re-uses the ticker (AGL Resources taking GAS from
                    # Nicor), not the same company.
                    chosen = None
                else:
                    chosen, rule = c_cont, "ticker_continuity"
            elif c_cont is not chosen:
                # Name says A, ticker continuity says B.
                last_ev = max((d for ev in chosen.evidence.values() for d, _, _ in ev if d <= on), default=None)
                name_company_out = last_ev is not None and chosen.removed_between(last_ev, on + timedelta(days=1))
                if name_company_out:
                    # A left the index after its last sighting: the name was taken over
                    # by B (acquirer adopting the target's name, e.g. Avago -> Broadcom).
                    chosen, rule = c_cont, "ticker_continuity_name_reused"
                elif self._names_compatible(c_cont, name):
                    chosen, rule = c_cont, "ticker_continuity_over_name"
        if chosen is None and kind == "add" and name:
            # Re-entry: a company that left the index earlier comes back under the same
            # ticker and a compatible name ("American Airlines" / "American Airlines Group").
            cands = [c for c in self.live() if c.has_ticker(ticker) and self._names_compatible(c, name) and c.last_remove() is not None and c.last_remove() < on and not any(d > c.last_remove() for d, a, _ in c.claims if a == "ADD")]
            if len(cands) == 1:
                chosen, rule = cands[0], "ticker_reentry_compatible_name"
        if chosen is None and kind == "remove":
            # REMOVE of a ticker with an older evidence trail (gap > CONTINUITY_DAYS) but not removed since
            cands = [c for c in self.live() if c.has_ticker(ticker) and not c.membership_excludes(on, on) and self._names_compatible(c, name)]
            if len(cands) == 1:
                chosen, rule = cands[0], "ticker_any_time_compatible"
        if chosen is None:
            chosen, rule = self.create(name, ticker, on, source, kind, needs_review=not name), "create"
        else:
            if name:
                chosen.names.setdefault(name, on)
                if chosen.needs_review:
                    chosen.canonical_name, chosen.needs_review = name, False
                    chosen.notes.append(f"named from {source} observation on {on}")
            chosen.add_evidence(ticker, on, kind)
        if rule != "create":
            self.log.append({"op": "resolve", "company_id": chosen.company_id, "ticker": ticker, "name": name, "date": on.isoformat(), "kind": kind, "rule": rule})
        return chosen, False

    def resolve_reference_interval(self, ticker: str, start: date, end: Optional[date], exclude: Optional[str] = None) -> tuple[Optional[Company], str]:
        cands = [c for c in self.live() if c.has_ticker(ticker) and not c.membership_excludes(start, end) and c.company_id != exclude]
        if not cands:
            return None, "no_candidate"
        scored = sorted(((c.evidence_inside(ticker, start, end), c.company_id) for c in cands), reverse=True)
        top = scored[0]
        if top[0] == 0:
            # ticker evidence exists but none near the interval: accept only if unique
            if len(cands) == 1:
                return cands[0], "ticker_unique_no_dated_evidence"
            return None, "ambiguous_no_dated_evidence"
        if len(scored) > 1 and scored[1][0] == top[0]:
            return None, "ambiguous_tie"
        return self.get(top[1]), "ticker_evidence"

    # -- finalisation --------------------------------------------------------
    def finalize_ticker_spans(self, today: date) -> None:
        """Derive contiguous ticker spans per company from dated evidence.

        Tickers are ordered by first evidence; each span runs from its first
        evidence to the day before the next ticker's first evidence. The last
        span is open-ended if the company is a current member, else it ends at
        its last evidence.
        """
        for c in self.live():
            items = []
            ends_with_change: dict[str, bool] = {}
            for t, ev in c.evidence.items():
                # Point-in-time-reliable kinds first; the reference back-fills tickers and
                # old change-table ADD rows are frequently written with today's ticker.
                for kinds in (("snapshot", "remove", "current", "curated"), ("reference",), ("add",)):
                    ds = [d for d, _, k in ev if k in kinds]
                    if ds:
                        break
                hi = max(ds)
                # last sighting is the remove-half of a ticker change on that very day
                ends_with_change[t] = any(d == hi and k in ("remove", "curated") for d, _, k in ev)
                items.append((min(ds), hi, t))
            items.sort()
            spans: list[TickerSpan] = []
            last_r = c.last_remove()
            current_member = last_r is None or any(d > last_r for d, a, _ in c.claims if a == "ADD") or any(d >= today - timedelta(days=400) for ev in c.evidence.values() for d, _, _ in ev)
            for i, (lo, hi, t) in enumerate(items):
                lo_eff = None if i == 0 else lo  # earliest ticker: unknown start
                # A later ticker *replaces* this one only if this one was not seen after
                # the later one first appeared; otherwise they are concurrent share classes.
                successors = [n_lo for n_lo, n_hi, n_t in items[i + 1 :] if n_lo > hi or (n_lo == hi and ends_with_change[t])]
                if successors:
                    hi_eff = min(successors) - timedelta(days=1)
                elif current_member:
                    hi_eff = None
                else:
                    hi_eff = hi
                spans.append(TickerSpan(t, lo_eff, hi_eff, "evidence"))
            c.tickers = spans
