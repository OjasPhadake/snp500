"""Point-in-time query layer over the loaded database.

Every function takes a SQLAlchemy ``Session`` and returns plain dicts so the
same code serves the FastAPI layer, the dashboard and notebooks.

Semantics
---------
* A company is a constituent on date ``d`` iff it has a membership interval
  with ``entry_date <= d`` (or unknown entry) and ``d < exit_date`` (or no
  exit). Exit dates are the first day the company is *not* in the index.
* Intervals flagged ``supported=False`` (single uncorroborated source) are
  excluded unless ``include_unsupported`` is set.
* Ticker and sector are resolved *as of* the requested date, never projected
  from today.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from snp500.db import models as m


def _active(d: date, include_unsupported: bool):
    conds = [or_(m.MembershipInterval.entry_date.is_(None), m.MembershipInterval.entry_date <= d), or_(m.MembershipInterval.exit_date.is_(None), m.MembershipInterval.exit_date > d)]
    if not include_unsupported:
        conds.append(m.MembershipInterval.supported.is_(True))
    return and_(*conds)


def tickers_as_of(s: Session, company_ids: list[str], d: date) -> dict[str, list[str]]:
    rows = s.execute(select(m.CompanyTicker.company_id, m.CompanyTicker.ticker).where(m.CompanyTicker.company_id.in_(company_ids), or_(m.CompanyTicker.valid_from.is_(None), m.CompanyTicker.valid_from <= d), or_(m.CompanyTicker.valid_to.is_(None), m.CompanyTicker.valid_to >= d))).all()
    out: dict[str, list[str]] = defaultdict(list)
    for cid, t in rows:
        out[cid].append(t)
    return out


def sector_as_of(s: Session, company_ids: list[str], d: date, max_lookback_days: int = 400) -> dict[str, dict]:
    """Latest sector observation at or before ``d`` (within lookback), else the
    earliest observation *after* d within lookback, else None. Observations are
    never taken from a different era than requested."""
    rows = s.execute(select(m.SectorObservation).where(m.SectorObservation.company_id.in_(company_ids))).scalars().all()
    by_c: dict[str, list[m.SectorObservation]] = defaultdict(list)
    for r in rows:
        by_c[r.company_id].append(r)
    out: dict[str, dict] = {}
    for cid, obs in by_c.items():
        before = [o for o in obs if o.as_of_date <= d and (d - o.as_of_date).days <= max_lookback_days]
        after = [o for o in obs if o.as_of_date > d and (o.as_of_date - d).days <= max_lookback_days]
        pick = max(before, key=lambda o: o.as_of_date) if before else (min(after, key=lambda o: o.as_of_date) if after else None)
        if pick is not None:
            out[cid] = {"gics_sector": pick.gics_sector, "gics_sub_industry": pick.gics_sub_industry, "sector_as_of": pick.as_of_date.isoformat(), "sector_source": pick.source_url}
    return out


def get_constituents(s: Session, d: date, include_unsupported: bool = False, with_sector: bool = True) -> list[dict]:
    q = select(m.MembershipInterval, m.Company).join(m.Company).where(_active(d, include_unsupported))
    rows = s.execute(q).all()
    ids = [c.company_id for _, c in rows]
    tk = tickers_as_of(s, ids, d)
    sec = sector_as_of(s, ids, d) if with_sector else {}
    out = []
    for iv, c in rows:
        out.append({"company_id": c.company_id, "company_name": c.canonical_name, "tickers": tk.get(c.company_id) or [iv.entry_ticker], "entry_date": iv.entry_date, "exit_date": iv.exit_date, "left_censored": iv.left_censored, "supported": iv.supported, **sec.get(c.company_id, {"gics_sector": None, "gics_sub_industry": None, "sector_as_of": None, "sector_source": None})})
    out.sort(key=lambda r: (r["tickers"][0] if r["tickers"] else ""))
    return out


def get_changes(s: Session, start: date, end: date, actions: Optional[list[str]] = None, min_confidence: float = 0.0) -> list[dict]:
    q = select(m.ConstituentEvent).where(m.ConstituentEvent.effective_date >= start, m.ConstituentEvent.effective_date <= end, m.ConstituentEvent.confidence >= min_confidence)
    if actions:
        q = q.where(m.ConstituentEvent.action.in_(actions))
    q = q.order_by(m.ConstituentEvent.effective_date, m.ConstituentEvent.action, m.ConstituentEvent.ticker_at_event)
    return [event_dict(e) for e in s.execute(q).scalars().all()]


def event_dict(e: m.ConstituentEvent) -> dict:
    return {"event_id": e.event_id, "effective_date": e.effective_date, "action": e.action, "company_id": e.company_id, "ticker_at_event": e.ticker_at_event, "company_name": e.company_name, "reason": e.reason, "reason_category": e.reason_category, "counterpart_company_id": e.counterpart_company_id, "source_url": e.source_url, "source_type": e.source_type, "scraped_at": e.scraped_at, "raw_sha256": e.raw_sha256, "confidence": e.confidence, "corroborating_sources": (e.corroborating_sources or "").split(";") if e.corroborating_sources else [], "refs": (e.refs or "").split(";") if e.refs else [], "notes": e.notes}


def find_company(s: Session, query: str) -> list[m.Company]:
    """Match by company_id, any historical ticker, or name substring."""
    q = query.strip()
    hits: list[m.Company] = []
    c = s.get(m.Company, q)
    if c:
        return [c]
    tq = q.upper().replace("-", ".")
    for cid in s.execute(select(m.CompanyTicker.company_id).where(m.CompanyTicker.ticker == tq)).scalars().all():
        hits.append(s.get(m.Company, cid))
    if hits:
        return hits
    like = f"%{q.lower()}%"
    ids = set(s.execute(select(m.CompanyName.company_id).where(func.lower(m.CompanyName.name).like(like))).scalars().all())
    ids |= set(s.execute(select(m.Company.company_id).where(func.lower(m.Company.canonical_name).like(like))).scalars().all())
    return [s.get(m.Company, cid) for cid in sorted(ids)]


def get_company_history(s: Session, company: str) -> list[dict]:
    out = []
    for c in find_company(s, company):
        intervals = sorted(c.intervals, key=lambda i: (i.entry_date or date.min))
        out.append({
            "company_id": c.company_id, "canonical_name": c.canonical_name, "cik": c.cik, "current_ticker": c.current_ticker, "needs_review": c.needs_review,
            "names": [{"name": n.name, "first_seen": n.first_seen} for n in sorted(c.names, key=lambda n: n.first_seen)],
            "tickers": [{"ticker": t.ticker, "valid_from": t.valid_from, "valid_to": t.valid_to, "evidence_kinds": t.evidence_kinds} for t in sorted(c.tickers, key=lambda t: (t.valid_from or date.min))],
            "memberships": [{"entry_date": i.entry_date, "exit_date": i.exit_date, "entry_ticker": i.entry_ticker, "exit_ticker": i.exit_ticker, "exit_reason_category": i.exit_reason_category, "left_censored": i.left_censored, "supported": i.supported, "days": ((i.exit_date or date.today()) - (i.entry_date or date(1996, 1, 2))).days} for i in intervals],
            "events": [event_dict(e) for e in sorted(c.events, key=lambda e: (e.effective_date, e.action))],
            "sectors": [{"as_of_date": o.as_of_date, "gics_sector": o.gics_sector, "gics_sub_industry": o.gics_sub_industry, "source_url": o.source_url} for o in sorted(c.sectors, key=lambda o: o.as_of_date)],
            "notes": c.notes,
        })
    return out


def get_longest_memberships(s: Session, limit: int = 25, as_of: Optional[date] = None, current_only: bool = False) -> list[dict]:
    as_of = as_of or date.today()
    q = select(m.MembershipInterval, m.Company).join(m.Company).where(m.MembershipInterval.supported.is_(True))
    if current_only:
        q = q.where(m.MembershipInterval.exit_date.is_(None))
    rows = []
    for iv, c in s.execute(q).all():
        start = iv.entry_date or date(1996, 1, 2)
        end = iv.exit_date or as_of
        rows.append({"company_id": c.company_id, "company_name": c.canonical_name, "entry_date": iv.entry_date, "exit_date": iv.exit_date, "left_censored": iv.left_censored or iv.entry_date is None, "days": (end - start).days, "years": round((end - start).days / 365.25, 2)})
    rows.sort(key=lambda r: -r["days"])
    return rows[:limit]


def get_companies_that_exited(s: Session, start: date, end: date, reason_category: Optional[str] = None) -> list[dict]:
    q = select(m.MembershipInterval, m.Company).join(m.Company).where(m.MembershipInterval.exit_date >= start, m.MembershipInterval.exit_date <= end, m.MembershipInterval.supported.is_(True))
    if reason_category:
        q = q.where(m.MembershipInterval.exit_reason_category == reason_category)
    ev_by_id = {}
    rows = s.execute(q.order_by(m.MembershipInterval.exit_date)).all()
    ids = [iv.exit_event_id for iv, _ in rows if iv.exit_event_id]
    if ids:
        for e in s.execute(select(m.ConstituentEvent).where(m.ConstituentEvent.event_id.in_(ids))).scalars().all():
            ev_by_id[e.event_id] = e
    out = []
    for iv, c in rows:
        e = ev_by_id.get(iv.exit_event_id)
        out.append({"company_id": c.company_id, "company_name": c.canonical_name, "exit_ticker": iv.exit_ticker, "entry_date": iv.entry_date, "exit_date": iv.exit_date, "exit_reason_category": iv.exit_reason_category, "reason": e.reason if e else None, "source_url": e.source_url if e else None, "confidence": e.confidence if e else None, "days_in_index": (iv.exit_date - (iv.entry_date or date(1996, 1, 2))).days})
    return out


# ---------------------------------------------------------------- analytics
def turnover_by_year(s: Session, start_year: int = 2001, end_year: Optional[int] = None) -> list[dict]:
    end_year = end_year or date.today().year
    rows = s.execute(select(m.ConstituentEvent.effective_date, m.ConstituentEvent.action).where(m.ConstituentEvent.action.in_(["ADD", "REMOVE"]))).all()
    adds, removes = Counter(), Counter()
    for d, a in rows:
        (adds if a == "ADD" else removes)[d.year] += 1
    out = []
    for y in range(start_year, end_year + 1):
        n = len(get_constituents(s, date(y, 12, 31) if y < date.today().year else date.today(), with_sector=False))
        out.append({"year": y, "additions": adds.get(y, 0), "removals": removes.get(y, 0), "members_at_year_end": n, "turnover_pct": round(100.0 * removes.get(y, 0) / n, 2) if n else None})
    return out


def exit_reasons_by_year(s: Session, start_year: int = 2001) -> list[dict]:
    rows = s.execute(select(m.ConstituentEvent.effective_date, m.ConstituentEvent.reason_category).where(m.ConstituentEvent.action == "REMOVE")).all()
    table: dict[int, Counter] = defaultdict(Counter)
    for d, cat in rows:
        if d.year >= start_year:
            table[d.year][cat] += 1
    return [{"year": y, **dict(c)} for y, c in sorted(table.items())]


def sector_composition(s: Session, d: date) -> list[dict]:
    cons = get_constituents(s, d)
    c = Counter((r["gics_sector"] or "Unknown / not observed") for r in cons)
    total = sum(c.values())
    return [{"gics_sector": k, "count": v, "pct": round(100.0 * v / total, 2)} for k, v in c.most_common()]


def membership_duration_stats(s: Session) -> dict:
    ivs = s.execute(select(m.MembershipInterval).where(m.MembershipInterval.supported.is_(True))).scalars().all()
    today = date.today()
    completed = [((i.exit_date - i.entry_date).days / 365.25) for i in ivs if i.exit_date and i.entry_date and not i.left_censored]
    ongoing = [((today - i.entry_date).days / 365.25) for i in ivs if not i.exit_date and i.entry_date and not i.left_censored]
    censored = sum(1 for i in ivs if i.left_censored or i.entry_date is None)
    hist = Counter()
    for y in completed:
        hist[f"{int(y // 5) * 5}-{int(y // 5) * 5 + 5}y"] += 1
    completed.sort()
    med = completed[len(completed) // 2] if completed else None
    return {"n_intervals": len(ivs), "n_completed": len(completed), "n_ongoing": len(ongoing), "n_left_censored": censored, "median_completed_years": round(med, 2) if med else None, "mean_completed_years": round(sum(completed) / len(completed), 2) if completed else None, "histogram_completed_5y_bins": dict(sorted(hist.items(), key=lambda kv: int(kv[0].split("-")[0])))}


def survival_curve(s: Session, cohort_start: date, cohort_end: date, horizon_years: int = 25) -> list[dict]:
    """Kaplan-Meier survival of companies that *entered* between cohort_start and cohort_end."""
    ivs = s.execute(select(m.MembershipInterval).where(m.MembershipInterval.supported.is_(True), m.MembershipInterval.entry_date >= cohort_start, m.MembershipInterval.entry_date <= cohort_end)).scalars().all()
    today = date.today()
    obs = []
    for i in ivs:
        t = ((i.exit_date or today) - i.entry_date).days / 365.25
        obs.append((t, i.exit_date is not None))
    obs.sort()
    n_at_risk = len(obs)
    surv = 1.0
    out = [{"years": 0.0, "survival": 1.0, "at_risk": n_at_risk}]
    i = 0
    while i < len(obs):
        t = obs[i][0]
        d = sum(1 for tt, ev in obs[i:] if tt == t and ev)
        c = sum(1 for tt, ev in obs[i:] if tt == t)
        if d:
            surv *= 1 - d / n_at_risk
            out.append({"years": round(t, 3), "survival": round(surv, 4), "at_risk": n_at_risk})
        n_at_risk -= c
        i += c
    return [o for o in out if o["years"] <= horizon_years]


def cemetery(s: Session, limit: int = 2000) -> list[dict]:
    """Companies whose latest membership has ended (with no later re-entry)."""
    rows = s.execute(select(m.MembershipInterval, m.Company).join(m.Company).where(m.MembershipInterval.supported.is_(True))).all()
    latest: dict[str, tuple] = {}
    for iv, c in rows:
        k = iv.entry_date or date.min
        if c.company_id not in latest or k > latest[c.company_id][0]:
            latest[c.company_id] = (k, iv, c)
    out = []
    for _, iv, c in latest.values():
        if iv.exit_date:
            out.append({"company_id": c.company_id, "company_name": c.canonical_name, "exit_ticker": iv.exit_ticker, "entry_date": iv.entry_date, "exit_date": iv.exit_date, "exit_reason_category": iv.exit_reason_category, "years_in_index": round((iv.exit_date - (iv.entry_date or date(1996, 1, 2))).days / 365.25, 2), "left_censored": iv.left_censored})
    out.sort(key=lambda r: r["exit_date"], reverse=True)
    return out[:limit]


def data_quality(s: Session) -> dict:
    issues = Counter(k for (k,) in s.execute(select(m.BuildIssue.kind)).all())
    disc = Counter((c, sev, k) for c, sev, k in s.execute(select(m.Discrepancy.comparison, m.Discrepancy.severity, m.Discrepancy.kind)).all())
    n_review = s.execute(select(func.count()).select_from(m.Company).where(m.Company.needs_review.is_(True))).scalar()
    run = s.execute(select(m.BuildRun).order_by(m.BuildRun.id.desc())).scalars().first()
    return {"build": run.manifest_json if run else None, "issues": dict(issues), "discrepancies": {f"{c}/{sev}/{k}": v for (c, sev, k), v in disc.items()}, "companies_needing_review": n_review}
