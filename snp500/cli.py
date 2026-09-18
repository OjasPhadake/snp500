"""Command-line entry point: ``snp500 scrape | build | load | serve | all``."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import typer

from snp500.config import settings
from snp500.logging_setup import get_logger, log_event

app = typer.Typer(help="S&P 500 Historical Constituent Intelligence Platform")
log = get_logger("snp500.cli")


@app.command()
def scrape(force: bool = typer.Option(False, help="Re-fetch even if a fresh snapshot exists"), revisions: bool = typer.Option(True, help="Fetch dated Wikipedia page revisions (one per year from 2007)"), start_year: int = 2007):
    """Fetch all sources into the append-only raw store (data/raw)."""
    from snp500.scraping import sources as S
    from snp500.scraping.http import Fetcher

    f = Fetcher()
    max_age = None if force else timedelta(days=1)
    c = S.fetch_current_constituents(f, max_age=max_age) if not force else f.fetch(S.SRC_CONSTITUENTS, S.CONSTITUENTS_URL, force=True)
    ch = S.fetch_changes(f, max_age=max_age) if not force else f.fetch(S.SRC_CHANGES, S.CHANGES_URL, force=True)
    S.fetch_reference_dataset(f, max_age=None if force else timedelta(days=30))
    if revisions:
        dates = S.default_snapshot_dates(start_year, date.today().year)
        if start_year <= 2007:
            dates = [date(2007, 7, 1)] + [d for d in dates if d.year >= 2008]
        S.fetch_revision_snapshots(f, dates)
    log_event(log, "scrape complete", constituents=c.snapshot.path, changes=ch.snapshot.path)


@app.command()
def build(no_reference: bool = typer.Option(False, help="Build from Wikipedia sources only (no reference gap-fill)")):
    """Reconstruct companies, events and intervals from the raw store; write data/build and the reconciliation report."""
    from snp500.pipeline import run_build

    r = run_build(use_reference=not no_reference)
    typer.echo(f"companies={len(r.registry.live())} events={len(r.events)} intervals={len(r.intervals)} issues={len(r.issues)}")
    typer.echo(f"report: {settings.build_dir / 'reconciliation' / 'report.md'}")


@app.command()
def load(database_url: Optional[str] = typer.Option(None, help="Override SNP500_DATABASE_URL")):
    """Load data/build into the database (SQLite by default, PostgreSQL via SNP500_DATABASE_URL)."""
    from snp500.db.load import load_build

    counts = load_build(url=database_url)
    for k, v in counts.items():
        typer.echo(f"{k:32s} {v}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Run the FastAPI server (dashboard at /, OpenAPI docs at /docs)."""
    import uvicorn

    uvicorn.run("snp500.api.main:app", host=host, port=port, reload=reload)


@app.command()
def all(force: bool = False):  # noqa: A001
    """scrape -> build -> load."""
    scrape(force=force)
    build()
    load()


if __name__ == "__main__":
    app()
