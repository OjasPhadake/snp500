# High-Level Design

## 1. Purpose and non-goals

The platform reconstructs S&P 500 membership as a **dated event log** so that any historical date can be queried without survivorship bias. It is explicitly *not* a price/returns database and does not attempt to reconstruct index weights. The primary analysis window is 2001-01-01 – 2026-12-31; data back to 1996 (reference) and 1957 (Wikipedia "date added") is retained and flagged `in_window = false` where outside.

## 2. System context

```
   Wikipedia (current list, changes table, dated revisions)     fja05680/sp500 (reference, MIT)
                 │                                                        │
                 ▼                                                        ▼
   ┌──────────────────────────── Scraping / Ingestion ─────────────────────────────┐
   │ Fetcher: UA, 1 req/s, retries+backoff, JSON logs, immutable-revision caching  │
   │ RawStore: append-only files + manifest.jsonl (sha256, fetched_at, url)        │
   └─────────────────────────────────────┬─────────────────────────────────────────┘
                                         ▼
   ┌──────────────────── Parsing (deterministic, problem-reporting) ───────────────┐
   │ parse_constituents / parse_changes / parse_revision_lookup / parse_membership │
   └─────────────────────────────────────┬─────────────────────────────────────────┘
                                         ▼
   ┌──────────────── Normalisation & Entity Resolution (CompanyRegistry) ──────────┐
   │ name keys, ticker normalisation, dated ticker evidence, curated hints         │
   └─────────────────────────────────────┬─────────────────────────────────────────┘
                                         ▼
   ┌──────────────── Event Reconstruction (candidates → events → intervals) ───────┐
   │ per-source candidates, clustering, corroboration, state machine, issues       │
   └───────────────┬─────────────────────────────────────────┬─────────────────────┘
                   ▼                                         ▼
   ┌──── Reconciliation engine ────┐              ┌──── Build artefacts ────┐
   │ vs reference (company level)  │              │ data/build/*.csv, jsonl │
   │ vs page revisions (ticker)    │              │ build_manifest.json     │
   │ report.md + discrepancies.csv │              └──────────┬──────────────┘
   └───────────────────────────────┘                         ▼
                                              ┌──── PostgreSQL / SQLite (SQLAlchemy) ────┐
                                              └──────────────────┬───────────────────────┘
                                                                 ▼
                                              ┌──── FastAPI ──── Dashboard (Plotly) ─────┐
                                              └──────────────────────────────────────────┘
```

Human-in-the-loop: the reconciliation report is the work queue; verified findings go into `data/curated/*.csv` with citations, and the build is re-run.

## 3. Components

| Component | Module | Responsibility | Key properties |
|---|---|---|---|
| Fetcher | `scraping/http.py` | HTTP with throttling, retries (429/5xx, network), caching by max-age via the raw store | Never returns bytes that were not persisted first |
| RawStore | `scraping/raw_store.py` | Immutable snapshot files + JSONL manifest | Append-only; hash-verified reads |
| Sources | `scraping/sources.py` | What to fetch; revision lookup via MediaWiki API | Page revisions cached forever (immutable) |
| Parsers | `scraping/wikipedia.py`, `scraping/reference.py` | HTML/CSV → dataclasses; header matching by meaning; `problems` list | Pure, deterministic |
| Normalisation | `normalize/text.py` | Dates, tickers, name keys | Total functions; malformed input raises |
| CompanyRegistry | `reconstruct/entity_resolution.py` | Persistent identities, evidence, claims, merges, ticker spans | Deterministic IDs; every decision logged |
| Candidates & classification | `reconstruct/events.py`, `reconstruct/classify.py` | Per-source claims; reason vocabulary | Source confidence table |
| Membership builder | `reconstruct/membership.py` | Phases 1–5 (resolve, pair, cluster, derive, flag) | Issues never dropped |
| Reconciliation | `reconcile/engine.py` | Set comparisons, range collapsing, severity, report | Read-only w.r.t. the reconstruction |
| Pipeline | `pipeline.py` | Orchestrates an offline build; writes artefacts + manifest | Deterministic |
| DB | `db/models.py`, `db/load.py` | Schema; atomic idempotent load | Postgres or SQLite |
| API | `api/queries.py`, `api/main.py` | Point-in-time queries, analytics | Sector/ticker as-of date |
| Dashboard | `api/templates/dashboard.html` | Explorer, timelines, turnover, sectors, survival, cemetery | Static HTML + Plotly over the API |
| CLI | `cli.py` | `scrape / build / load / serve / all` | |

## 4. Data flow and trust model

Sources are ranked by how *point-in-time reliable* their identifiers are (see `DATA_SOURCES.md`). The pipeline never merges sources by overwriting; it collects **claims**, resolves the **entity** each claim is about, and then lets claims **corroborate** each other. A claim standing alone is kept with its low confidence and, for membership, is flagged unsupported.

Confidence per source (before corroboration): curated 0.99 · Wikipedia changes row with citation 0.95 · without 0.90 · Wikipedia "date added" 0.75 · reference 0.60 (0.50 for left-censored opening membership). Each corroborating source adds 0.05 (cap 0.99).

## 5. Point-in-time semantics

* Member on `d` ⇔ `entry_date ≤ d < exit_date` (unknown entry counts as ≤; unknown exit counts as ∞).
* Tickers on `d` are looked up in `company_tickers` validity ranges (several for multi-class listings).
* Sector on `d` is the nearest dated observation within 400 days, else *not observed*.
* Intervals with `supported = false` are excluded unless requested.

## 6. Reproducibility and provenance

* `build_manifest.json` lists the sha256 of every input snapshot.
* Every event carries `source_url`, `source_type`, `scraped_at`, `raw_sha256`, `confidence`, `corroborating_sources`, external `refs` resolved from Wikipedia citations, and `notes` describing how it was derived.
* `resolution_log.jsonl` records each identity decision (rule name, inputs, output).
* `candidates.csv` retains every per-source claim, resolved to a company, so the clustering can be audited.

## 7. Failure modes and how they surface

| Failure | Surface |
|---|---|
| Page layout change | `parse_problems.csv` (and build stops if the main tables vanish) |
| Source disagreement on a date | `conflicting_entry_date` / minor date disagreement in the report |
| Wikipedia-only membership claim | `unsupported_interval` issue; excluded from PIT queries |
| Identity ambiguity | `needs_review` company; `h5_ambiguous` log entries |
| Reference back-fill collapsing entities | `extra_in_ours` membership ranges in the report (see README numbers) |

## 8. Deployment

Single Python package. Dev: SQLite. Production: `docker compose up postgres`, set `SNP500_DATABASE_URL`, `snp500 load`, run `uvicorn snp500.api.main:app` behind a reverse proxy. Rebuilds are safe to run on a schedule: `scrape` appends snapshots, `build` is offline, `load` swaps tables in one transaction.
