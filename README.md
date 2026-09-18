# S&P 500 Historical Constituent Intelligence Platform

*A point-in-time, event-sourced database reconstructing 25+ years of S&P 500 membership (2001–2026), corporate exits, entries and index turnover, with traceable data provenance and historical analytics.*

The central goal is **historical analysis without survivorship bias**: `get_constituents("2008-09-15")` returns the 499 companies that were actually in the index that morning (Lehman Brothers, Washington Mutual, Fannie Mae included), with the tickers and GICS sectors that were in force *then*, not today's list projected backwards.

## What is in the box

| Layer | Where | What it does |
|---|---|---|
| Scraping / ingestion | `snp500/scraping/` | Polite fetcher (rate limit, retries, structured logs) writing every response into an **append-only raw store** with sha256 manifests. Deterministic parsers for the Wikipedia constituents page, the historical-changes table and **dated page revisions** (one per year, 2007–2026). Loader for the MIT-licensed [fja05680/sp500](https://github.com/fja05680/sp500) reference dataset. |
| Normalisation & entity resolution | `snp500/normalize/`, `snp500/reconstruct/entity_resolution.py` | Persistent `company_id` separated from ticker identity. Dated ticker evidence, name keys, curated aliases/ticker changes/overrides, and ranked resolution rules for renames, ticker re-use, re-entries and acquirers that adopt the target's name. |
| Event reconstruction | `snp500/reconstruct/` | Candidate ADD/REMOVE claims from every source → clustered into one event per real-world change (with corroboration and confidence) → membership intervals via a state machine that records every anomaly as an explicit *issue*. |
| Reconciliation | `snp500/reconcile/` | Compares the reconstruction against the reference dataset (company level) and against Wikipedia page revisions (ticker level); writes `data/build/reconciliation/report.md` and `discrepancies.csv`. Conflicts are reported, never auto-resolved. |
| Storage | `snp500/db/` | SQLAlchemy models for PostgreSQL (SQLite for dev/tests). `constituent_events` is the source of truth; `membership_intervals` is a materialised derivation. |
| API | `snp500/api/` | FastAPI: `/constituents`, `/changes`, `/companies/{q}`, `/memberships/longest`, `/exits`, analytics, `/cemetery`, `/quality`. |
| Dashboard | `snp500/api/templates/dashboard.html` | Date-based constituent explorer, company timeline (membership + ticker Gantt), turnover, exit reasons, sector composition as observed on the date, duration histogram, Kaplan–Meier survival, and the S&P 500 Cemetery. |
| Tests | `tests/` | Ticker changes, re-entry, ticker re-use, same-day add/remove, acquisitions, spin-offs, duplicate identities, missing events, conflicting sources, unsupported single-source claims, malformed HTML, raw-store integrity, end-to-end DB + API. |

Documentation of every design decision lives in [`docs/`](docs/):

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — high-level design (HLD): pipeline, data flow, components, trust model.
* [`docs/LLD.md`](docs/LLD.md) — low-level design: data model, algorithms, resolution rules, state machine, IDs, invariants.
* [`docs/DECISIONS.md`](docs/DECISIONS.md) — architecture decision records (ADRs) with the reasoning and the alternatives rejected.
* [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) — every source, what it is trusted for, its known defects, and licensing.
* [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — how to run, rebuild, curate discrepancies, and deploy with PostgreSQL.
* [`data/curated/README.md`](data/curated/README.md) — the human-in-the-loop override files.

## Quick start

```bash
pip install -r requirements.txt
pip install -e .   # installs the `snp500` command (into ~/.local/bin for user installs; `python -m snp500.cli` works regardless)
snp500 scrape          # fetch sources into data/raw (cached; ~1 req/s, polite user agent)
snp500 build           # deterministic offline rebuild -> data/build/*.csv + reconciliation report
snp500 load            # load into SQLite (data/build/snp500.sqlite3) or PostgreSQL via SNP500_DATABASE_URL
snp500 serve           # dashboard at http://127.0.0.1:8000/  API docs at /docs
pytest                 # 29 scenario, parser, store and API tests
```

PostgreSQL: `docker compose up -d postgres`, then `export SNP500_DATABASE_URL=postgresql+psycopg2://snp500:snp500@localhost:5432/snp500` before `snp500 load`/`serve`.

The repository ships with the raw snapshots and build artefacts used for the numbers below, so `snp500 build && snp500 load && snp500 serve` works offline and reproduces byte-identical events.

## Core API

| Function | Endpoint | Notes |
|---|---|---|
| `get_constituents(date)` | `GET /constituents?date=YYYY-MM-DD` | Members on that date with tickers and sector *as observed then*. Exit dates are the first day out. |
| `get_changes(start, end)` | `GET /changes?start_date=&end_date=` | ADD / REMOVE / TICKER_CHANGE events with reason, category, source URL, snapshot hash, confidence and corroborating sources. |
| `get_company_history(company)` | `GET /companies/{ticker|id|name}` | Names, ticker spans, membership intervals, events, sector observations. |
| `get_longest_memberships()` | `GET /memberships/longest` | Left-censored intervals are flagged (entry before evidence). |
| `get_companies_that_exited(start, end)` | `GET /exits?start_date=&end_date=` | With classified exit reason and cited source. |

## Current build (2026-09-18)

| Metric | Value |
|---|---|
| Companies (persistent identities) | 1,182 |
| Events (ADD / REMOVE / TICKER_CHANGE) | 1,982 |
| Membership intervals | 1,194 (14 flagged *unsupported*: single uncorroborated source, excluded from point-in-time queries by default) |
| Reference reconciliation, company level | 0 companies missing on every compared date 2001–2026; 1–3 extra on some dates, all traced to the reference collapsing two legal entities into one back-filled ticker lineage (documented in the report) |
| Identities known only from ticker evidence | 245 (mostly pre-2008 exits; named where a dated page revision covers them) |

Everything above is regenerated by `snp500 build`; see `data/build/reconciliation/report.md` for the full disagreement list.

## Positioning

This is not a web-scraping project. The scraping is the smallest part. The substance is **point-in-time correctness**, **provenance** (every event cites the URL, fetch time and sha256 of the bytes it came from), **reproducibility** (offline deterministic builds, deterministic IDs), **entity resolution** (companies ≠ tickers ≠ names), **testing** of the hard cases, and **explicit handling of disagreement** between sources instead of silent overwrites.

## License

Code: MIT. Reference data: fja05680/sp500 (MIT, see `data/raw/reference_fja05680/`). Wikipedia content: CC BY-SA 4.0; raw snapshots are retained for provenance.
