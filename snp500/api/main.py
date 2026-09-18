"""FastAPI layer. Every endpoint is a thin wrapper over ``queries`` and returns
point-in-time-correct data. Dates are ISO-8601 (YYYY-MM-DD)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from snp500 import __version__
from snp500.api import queries as q
from snp500.db.session import get_sessionmaker

app = FastAPI(
    title="S&P 500 Historical Constituent Intelligence Platform",
    version=__version__,
    description="Point-in-time, event-sourced S&P 500 membership 2001-2026 with traceable provenance. "
    "Exit dates are the first day a company is *not* in the index. Intervals from a single uncorroborated source are excluded unless include_unsupported=true.",
)

TEMPLATES = Path(__file__).parent / "templates"


def get_db():
    Session = get_sessionmaker()
    with Session() as s:
        yield s


def _date(s: str, name: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return (TEMPLATES / "dashboard.html").read_text()


@app.get("/health")
def health(db: Session = Depends(get_db)):
    return {"status": "ok", "version": __version__, "quality": q.data_quality(db)}


@app.get("/constituents", summary="get_constituents(date)")
def constituents(date_: str = Query(..., alias="date", description="YYYY-MM-DD"), include_unsupported: bool = False, db: Session = Depends(get_db)):
    d = _date(date_, "date")
    rows = q.get_constituents(db, d, include_unsupported=include_unsupported)
    return {"date": d, "count": len(rows), "constituents": rows}


@app.get("/changes", summary="get_changes(start_date, end_date)")
def changes(start_date: str, end_date: str, action: Optional[str] = Query(None, description="ADD, REMOVE or TICKER_CHANGE"), min_confidence: float = 0.0, db: Session = Depends(get_db)):
    rows = q.get_changes(db, _date(start_date, "start_date"), _date(end_date, "end_date"), [action] if action else None, min_confidence)
    return {"count": len(rows), "changes": rows}


@app.get("/companies/{company}", summary="get_company_history(company)")
def company_history(company: str, db: Session = Depends(get_db)):
    rows = q.get_company_history(db, company)
    if not rows:
        raise HTTPException(404, f"no company matches {company!r} (try a ticker, company_id or name fragment)")
    return {"matches": len(rows), "companies": rows}


@app.get("/memberships/longest", summary="get_longest_memberships()")
def longest(limit: int = 25, current_only: bool = False, db: Session = Depends(get_db)):
    return {"memberships": q.get_longest_memberships(db, limit=limit, current_only=current_only)}


@app.get("/exits", summary="get_companies_that_exited(start_date, end_date)")
def exits(start_date: str, end_date: str, reason_category: Optional[str] = None, db: Session = Depends(get_db)):
    rows = q.get_companies_that_exited(db, _date(start_date, "start_date"), _date(end_date, "end_date"), reason_category)
    return {"count": len(rows), "exits": rows}


@app.get("/analytics/turnover")
def turnover(start_year: int = 2001, end_year: Optional[int] = None, db: Session = Depends(get_db)):
    return {"turnover": q.turnover_by_year(db, start_year, end_year)}


@app.get("/analytics/exit-reasons")
def exit_reasons(start_year: int = 2001, db: Session = Depends(get_db)):
    return {"exit_reasons": q.exit_reasons_by_year(db, start_year)}


@app.get("/analytics/sectors")
def sectors(date_: str = Query(..., alias="date"), db: Session = Depends(get_db)):
    d = _date(date_, "date")
    return {"date": d, "sectors": q.sector_composition(db, d)}


@app.get("/analytics/duration")
def duration(db: Session = Depends(get_db)):
    return q.membership_duration_stats(db)


@app.get("/analytics/survival")
def survival(cohort_start: str = "2001-01-01", cohort_end: str = "2026-12-31", db: Session = Depends(get_db)):
    return {"curve": q.survival_curve(db, _date(cohort_start, "cohort_start"), _date(cohort_end, "cohort_end"))}


@app.get("/cemetery")
def cemetery(limit: int = 2000, db: Session = Depends(get_db)):
    rows = q.cemetery(db, limit)
    return {"count": len(rows), "companies": rows}


@app.get("/quality")
def quality(db: Session = Depends(get_db)):
    out = q.data_quality(db)
    if out.get("build"):
        out["build"] = json.loads(out["build"])
    return out
