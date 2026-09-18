"""End-to-end build: raw snapshots -> parsed rows -> candidates -> events/intervals -> build artefacts.

The build is **offline and deterministic**: it reads only from the raw store
(``data/raw``) and curated files (``data/curated``); it never fetches. Running it
twice on the same raw snapshots produces byte-identical outputs.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from snp500.config import settings
from snp500.logging_setup import get_logger, log_event
from snp500.normalize.text import normalize_ticker
from snp500.reconcile import engine as rec
from snp500.reconstruct import membership as M
from snp500.reconstruct.entity_resolution import CompanyRegistry, Curated
from snp500.reconstruct.events import candidates_from_changes, candidates_from_constituents, candidates_from_reference
from snp500.scraping import sources as S
from snp500.scraping.raw_store import RawSnapshot, RawStore
from snp500.scraping.reference import ReferenceSnapshot, parse_membership_csv, snapshot_on
from snp500.scraping.wikipedia import parse_changes, parse_constituents

log = get_logger(__name__)


def _load_latest(store: RawStore, source: str, url_contains: Optional[str] = None) -> tuple[RawSnapshot, str]:
    best = None
    for snap in store.iter_snapshots(source):
        if url_contains and url_contains not in snap.url:
            continue
        if best is None or snap.fetched_at > best.fetched_at:
            best = snap
    if best is None:
        raise FileNotFoundError(f"no raw snapshot for {source} ({url_contains}); run `snp500 scrape` first")
    return best, store.read_text(best)


def load_revision_snapshots(store: RawStore) -> list[tuple[date, RawSnapshot, str]]:
    """All stored page revisions with the date they were requested for."""
    out = []
    for snap in store.iter_snapshots(S.SRC_REVISION):
        note = snap.note or ""
        if note.startswith("snapshot for "):
            d = date.fromisoformat(note.split()[-1])
            out.append((d, snap, store.read_text(snap)))
    # keep the latest fetch per requested date
    latest: dict[date, tuple[date, RawSnapshot, str]] = {}
    for d, snap, text in out:
        if d not in latest or snap.fetched_at > latest[d][1].fetched_at:
            latest[d] = (d, snap, text)
    return sorted(latest.values(), key=lambda x: x[0])


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def run_build(build_dir: Optional[Path] = None, raw_dir: Optional[Path] = None, curated_dir: Optional[Path] = None, use_reference: bool = True) -> M.BuildResult:
    build_dir = Path(build_dir or settings.build_dir)
    store = RawStore(raw_dir or settings.raw_dir)
    curated = Curated.load(curated_dir or settings.curated_dir)
    window_start = date.fromisoformat(settings.window_start)
    window_end = date.fromisoformat(settings.window_end)
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inputs: dict[str, str] = {}
    parse_problems: list[dict] = []

    # ---- parse Wikipedia ---------------------------------------------------
    snap_c, html_c = _load_latest(store, S.SRC_CONSTITUENTS)
    snap_ch, html_ch = _load_latest(store, S.SRC_CHANGES)
    inputs["wikipedia_constituents"] = snap_c.sha256
    inputs["wikipedia_changes"] = snap_ch.sha256
    pc = parse_constituents(html_c)
    pch = parse_changes(html_ch)
    parse_problems += [dict(source="wikipedia_constituents", **asdict(p)) for p in pc.problems]
    parse_problems += [dict(source="wikipedia_changes", **asdict(p)) for p in pch.problems]
    log_event(log, "parsed wikipedia", constituents=len(pc.rows), changes=len(pch.rows), problems=len(parse_problems))

    wiki_c = candidates_from_changes(pch.rows, snap_ch.url, snap_ch.fetched_at, snap_ch.sha256)
    wiki_c += candidates_from_constituents(pc.rows, snap_c.url, snap_c.fetched_at, snap_c.sha256)

    # ---- reference dataset -------------------------------------------------
    ref_c, ref_iv, ref_snaps = [], [], []
    if use_reference:
        snap_r, text_r = _load_latest(store, S.SRC_REFERENCE, "Historical%20Components")
        inputs["reference_fja05680"] = snap_r.sha256
        ref_snaps = parse_membership_csv(text_r)
        ref_c, ref_iv = candidates_from_reference(ref_snaps, snap_r.url, snap_r.fetched_at, snap_r.sha256)
        log_event(log, "parsed reference", snapshots=len(ref_snaps), intervals=len(ref_iv))

    # ---- dated page revisions (names, tickers, sectors, validation) ---------
    revs = load_revision_snapshots(store)
    snapshots: list[M.SnapshotObservation] = []
    sector_rows: list[tuple[date, RawSnapshot, list]] = []
    rev_sets: list[tuple[date, frozenset[str]]] = []
    for d, snap, html in revs:
        try:
            parsed = parse_constituents(html)
        except ValueError as exc:
            parse_problems.append(dict(source="wikipedia_revision", where=snap.url, row=[], error=str(exc)))
            continue
        inputs[f"wikipedia_revision:{d.isoformat()}"] = snap.sha256
        parse_problems += [dict(source=f"wikipedia_revision:{d}", **asdict(p)) for p in parsed.problems]
        snapshots.append(M.SnapshotObservation(d, parsed.rows, snap.url, snap.fetched_at, "wikipedia_revision"))
        sector_rows.append((d, snap, parsed.rows))
        rev_sets.append((d, frozenset(r.ticker for r in parsed.rows)))
    today = date.today()
    snapshots.append(M.SnapshotObservation(today, pc.rows, snap_c.url, snap_c.fetched_at, "wikipedia_constituents"))
    sector_rows.append((today, snap_c, pc.rows))

    # ---- build -------------------------------------------------------------
    reg = CompanyRegistry(curated)
    result = M.build(wiki_c, snapshots, ref_c, ref_iv, reg, window_start, window_end)
    log_event(log, "built", companies=len(reg.live()), events=len(result.events), intervals=len(result.intervals), issues=len(result.issues))

    # Name companies known only from ticker-level evidence using dated page rows.
    for d, snap, rows in sector_rows:
        for r in rows:
            for comp in reg.live():
                if comp.needs_review and any(abs((d - ed).days) <= 400 for ed in comp.evidence_dates(r.ticker)):
                    comp.canonical_name, comp.needs_review = r.name, False
                    comp.names.setdefault(r.name, d)
                    comp.notes.append(f"named from {snap.url} ({d})")
    sector_obs: list[dict] = []
    for d, snap, rows in sector_rows:
        for r in rows:
            comp = reg.by_ticker_continuity(r.ticker, d, r.name)
            if comp is not None and (r.gics_sector or r.gics_sub_industry):
                sector_obs.append(dict(company_id=comp.company_id, ticker=r.ticker, as_of_date=d.isoformat(), gics_sector=r.gics_sector, gics_sub_industry=r.gics_sub_industry, source_url=snap.url, source_type="wikipedia_revision" if d != today else "wikipedia_constituents", scraped_at=snap.fetched_at))

    # ---- reconciliation ----------------------------------------------------
    discrepancies: list[rec.Discrepancy] = []
    per_date: dict[str, list[tuple[date, int, int, int, int]]] = {}
    if ref_snaps:
        dates = sorted({s.on_date for s in ref_snaps if s.on_date >= window_start} | set(rec.quarter_ends(window_start, min(window_end, today))))
        ds = rec.compare_companies(result.intervals, reg, result.ref_assignment, dates)
        discrepancies += ds
        theirs_n = {d: sum(1 for ivs in result.ref_assignment.values() if any(iv.start <= d and (iv.end is None or d < iv.end) for iv in ivs)) for d in dates}
        per_date["reference_company"] = _per_date(result.intervals, reg, dates, theirs_n, ds)
    if rev_sets:
        ds = rec.compare_tickers(result.intervals, reg, "wikipedia_revision", rev_sets)
        discrepancies += ds
        per_date["wikipedia_revision"] = _per_date(result.intervals, reg, [d for d, _ in rev_sets], {d: len(t) for d, t in rev_sets}, ds)
    collapsed = rec.collapse(discrepancies)
    unknown = [c for c in reg.live() if c.needs_review]
    meta = {"built_at": built_at, "input_sha256": inputs, "window": [settings.window_start, settings.window_end], "n_companies": len(reg.live()), "n_events": len(result.events), "n_intervals": len(result.intervals), "n_unsupported_intervals": sum(1 for i in result.intervals if not i.supported), "n_issues": len(result.issues), "n_discrepancy_ranges": len(collapsed), "n_unknown_companies": len(unknown)}

    # ---- write artefacts ---------------------------------------------------
    write_artifacts(build_dir, result, sector_obs, parse_problems, collapsed, per_date, unknown, meta)
    return result


def _per_date(intervals, reg, dates, theirs_n, ds):
    by_date: dict[date, list[int]] = {d: [0, 0] for d in dates}
    for x in ds:
        by_date[x.on_date][0 if x.kind == "missing_in_ours" else 1] += 1
    return [(d, len(M.constituents_on(intervals, d)), theirs_n[d], by_date[d][0], by_date[d][1]) for d in dates]


def write_artifacts(build_dir: Path, result: M.BuildResult, sector_obs, parse_problems, collapsed, per_date, unknown, meta) -> None:
    reg = result.registry
    build_dir.mkdir(parents=True, exist_ok=True)
    companies = []
    names, tickers = [], []
    for c in sorted(reg.live(), key=lambda c: c.company_id):
        cur = c.ticker_on(date.today())
        companies.append(dict(company_id=c.company_id, canonical_name=c.canonical_name, name_key=c.name_key, current_ticker=cur or "", all_tickers=";".join(c.all_tickers()), cik=c.cik or "", needs_review=c.needs_review, notes=" | ".join(c.notes)))
        for n, d in sorted(c.names.items(), key=lambda x: x[1]):
            names.append(dict(company_id=c.company_id, name=n, first_seen=d.isoformat()))
        for s in c.tickers:
            ev = c.evidence.get(s.ticker, [])
            tickers.append(dict(company_id=c.company_id, ticker=s.ticker, valid_from=s.valid_from.isoformat() if s.valid_from else "", valid_to=s.valid_to.isoformat() if s.valid_to else "", evidence_kinds=";".join(sorted({k for _, _, k in ev})), first_evidence=min(d for d, _, _ in ev).isoformat(), last_evidence=max(d for d, _, _ in ev).isoformat()))
    _write_csv(build_dir / "companies.csv", companies, list(companies[0].keys()))
    _write_csv(build_dir / "company_names.csv", names, ["company_id", "name", "first_seen"])
    _write_csv(build_dir / "company_tickers.csv", tickers, ["company_id", "ticker", "valid_from", "valid_to", "evidence_kinds", "first_evidence", "last_evidence"])
    ev_rows = []
    for e in result.events:
        r = asdict(e)
        r["effective_date"] = e.effective_date.isoformat()
        r["corroborating_sources"] = ";".join(e.corroborating_sources)
        r["refs"] = ";".join(e.refs)
        ev_rows.append(r)
    _write_csv(build_dir / "events.csv", ev_rows, list(ev_rows[0].keys()))
    iv_rows = []
    for iv in result.intervals:
        r = asdict(iv)
        r["entry_date"] = iv.entry_date.isoformat() if iv.entry_date else ""
        r["exit_date"] = iv.exit_date.isoformat() if iv.exit_date else ""
        iv_rows.append(r)
    _write_csv(build_dir / "intervals.csv", iv_rows, list(iv_rows[0].keys()))
    _write_csv(build_dir / "issues.csv", [dict(company_id=i.company_id, kind=i.kind, on_date=i.on_date.isoformat() if i.on_date else "", detail=i.detail) for i in result.issues], ["company_id", "kind", "on_date", "detail"])
    _write_csv(build_dir / "sector_observations.csv", sector_obs, ["company_id", "ticker", "as_of_date", "gics_sector", "gics_sub_industry", "source_url", "source_type", "scraped_at"])
    _write_csv(build_dir / "parse_problems.csv", [dict(source=p["source"], where=p["where"], row=" | ".join(p["row"]), error=p["error"]) for p in parse_problems], ["source", "where", "row", "error"])
    with (build_dir / "resolution_log.jsonl").open("w") as fh:
        for r in reg.log:
            fh.write(json.dumps(r) + "\n")
    cand_rows = []
    for c in result.resolved:
        r = asdict(c)
        r["effective_date"] = c.effective_date.isoformat()
        r["reason_category"] = c.reason_category.value
        r["refs"] = ";".join(c.refs)
        cand_rows.append(r)
    _write_csv(build_dir / "candidates.csv", cand_rows, list(cand_rows[0].keys()))
    rdir = build_dir / "reconciliation"
    rdir.mkdir(exist_ok=True)
    _write_csv(rdir / "discrepancies.csv", collapsed, ["comparison", "severity", "kind", "ticker", "company_id", "company_name", "first_date", "last_date", "n_dates"])
    (rdir / "report.md").write_text(rec.render_report(collapsed, per_date, result.issues, unknown, meta))
    (build_dir / "build_manifest.json").write_text(json.dumps(meta, indent=2))
    log_event(log, "artifacts written", build_dir=str(build_dir), **{k: v for k, v in meta.items() if k.startswith("n_")})
