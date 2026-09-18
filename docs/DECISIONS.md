# Architecture Decision Records

Each record states the context, the decision, the alternatives considered and the consequences. Numbers refer to the order the decisions were taken during the build.

## ADR-001 Event sourcing instead of daily snapshots
**Context.** A snapshot table (500 rows × ~6,500 trading days ≈ 3.3M rows) answers `get_constituents(date)` trivially but cannot say *why* anything changed, cannot be corrected without rewriting history, and hides the provenance of each row.
**Decision.** Store dated `ADD` / `REMOVE` (and `TICKER_CHANGE`) events; derive membership intervals and point-in-time sets from them. Intervals are materialised (`membership_intervals`) for query speed but are always rebuilt from events.
**Consequences.** Every membership fact is traceable to one event with a source URL, fetch time and content hash. Corrections are new events/overrides, not edits.

## ADR-002 Company identity is separate from ticker identity
**Context.** Tickers are re-used (WM: Washington Mutual → Waste Management), renamed without an index exit (FB → META), and back-filled by sources (see ADR-005). Names are re-used too (Alcoa Inc. → Arconic while a *new* Alcoa Corp appears).
**Decision.** Persistent `company_id = "C" + sha1(entity key)[:10]`; ticker history and name history are attributes with validity ranges. A ticker change generates a `TICKER_CHANGE` event, never an exit/entry pair.
**Alternatives rejected.** Ticker as primary key (breaks on re-use); CIK as key (unavailable for most pre-2015 exits).

## ADR-003 Wikipedia is a source, not the truth
**Context.** The Wikipedia "selected changes" table is incomplete before ~2010 (7 rows for 2000, 1 for 2003), rows are frequently written with the company's *later* name and ticker ("ACT Actavis" added 1999 when it was Watson Pharmaceuticals), and some rows are simply wrong (the 2011 MPC spin-off row names Marathon Oil; the 2016 Chubb row lists the surviving acquirer's ticker as removed).
**Decision.** Treat each Wikipedia table as one *claim source* with its own confidence; require corroboration for membership; retain every raw page; and use **dated page revisions** (Jan 1 of each year 2008–2026 plus July 2007) as an independent point-in-time witness that also supplies historical GICS sectors.

## ADR-004 The reference dataset validates and gap-fills; it is not copied
**Context.** fja05680/sp500 (Clenow/Norgate 1996–2019, then Wikipedia-derived) is a daily ticker-set list. It is the only public source covering 2001–2010 densely.
**Decision.** Reference intervals become low-confidence candidates (0.5–0.6) that (a) corroborate Wikipedia events within a 10-day cluster window (confidence +0.05 per corroborating source), and (b) fill gaps with events whose `source_type = reference_fja05680`. A membership interval that overlaps **no** reference interval for its company is flagged `supported = False` and excluded from point-in-time queries by default (`include_unsupported=true` reveals it). Reconciliation reports every disagreement.
**Consequences.** Users can filter by `confidence` and `source_type`. The reference's own defects (ADR-005) are surfaced, not inherited silently.

## ADR-005 Back-filled tickers are a first-class problem
**Context.** Both public sources write history with later identifiers: the reference lists AABA (Altaba) for Yahoo from 1999, USB for the Firstar/U.S. Bancorp lineage from 1996, T for AT&T through the 2005 SBC reverse-takeover; Wikipedia's "Date added" column keeps a predecessor's date after a merger-rename.
**Decision.** Ticker evidence is *dated and weighted by kind*: `snapshot`/`remove`/`current`/`curated` = 1.0 (point-in-time reliable), `reference` = 0.8, changes-table `add` = 0.5. Ticker spans are derived from reliable evidence first. Reference intervals are assigned by evidence *inside* the interval, membership plausibility (a company removed before the interval cannot own it), and cross-source **date pairing** (H1–H5 in `LLD.md`). Curated ticker changes split back-filled reference intervals at the change date.

## ADR-006 Curated overrides are allowed, but only with a citation and only in four small files
**Context.** Some identities cannot be inferred from the sources (SBC → T, ACE → CB, Alcoa/Arconic). Hard-coding them in Python would hide judgment calls.
**Decision.** `data/curated/{company_aliases,ticker_changes,distinct_entities,event_overrides}.csv`, each row with a `source_url`. Every application is logged in `resolution_log.jsonl` with `rule = curated*`. The reconciliation report is the work queue for adding rows.

## ADR-007 Never silently resolve conflicts
**Decision.** The interval state machine records `duplicate_add`, `conflicting_entry_date`, `entry_after_opening`, `entry_date_superseded`, `remove_without_add`, `same_day_add_remove`, `unsupported_interval` as issues in `issues.csv` and the `build_issues` table. Parsers return `problems` instead of dropping rows. Reconciliation writes discrepancy ranges with a severity (`membership` vs `minor_date_disagreement` ≤ 10 days).

## ADR-008 Exit dates are the first day *out*
**Decision.** `exit_date` is the effective date of the REMOVE event (S&P's "prior to the open" convention); membership on date `d` requires `entry_date <= d < exit_date`. Same-day add+remove produces a zero-length interval that is kept and flagged, never dropped.

## ADR-009 Historical sectors are observations, not attributes
**Context.** GICS sectors change (2018 Communication Services reshuffle; 2023 reshuffles) and the current page only has today's classification.
**Decision.** `company_sector_observations(company_id, as_of_date, sector, source)` populated from dated page revisions. Queries take the observation nearest the requested date within 400 days, otherwise report *unknown / not observed* (as for 2001–2007). Today's sector is never projected backwards.

## ADR-010 Companies, not ticker lines, are the unit of membership
**Context.** Multi-class listings (GOOG/GOOGL, NWS/NWSA, FOX/FOXA) make the ticker count 503–505 while the company count is ~500.
**Decision.** One membership per company; all concurrent tickers are returned. Reconciliation against the (ticker-based) reference is done at the company level after mapping reference tickers through the same entity resolution; against page revisions at the ticker level.

## ADR-011 Deterministic, offline builds
**Decision.** `snp500 build` reads only `data/raw` and `data/curated`; it never fetches. Event IDs are `sha1(company_id|date|action|ticker)`; company IDs are hashes of the entity key. Re-running on the same raw snapshots yields identical CSVs. `build_manifest.json` records the sha256 of every input snapshot.

## ADR-012 Append-only raw store
**Decision.** Every HTTP body is written to `data/raw/<source>/<utc-ts>_<sha256[:10]>.<ext>` before parsing; a JSONL manifest records URL, time, hash, status and size. Identical bodies re-use the file but still get a manifest line. Nothing is ever overwritten or deleted by the pipeline. Reads verify the hash.

## ADR-013 PostgreSQL target with SQLite fallback
**Decision.** SQLAlchemy 2.0 models; default `sqlite:///data/build/snp500.sqlite3` so the whole pipeline and tests run with zero infrastructure; production via `SNP500_DATABASE_URL` (docker-compose provided). The load is atomic (one transaction) and idempotent.

## ADR-014 Wikipedia-only membership claims with no reference support are excluded by default
**Context.** A few changes-table ADD rows (e.g. FSR Firstar 1998) have no counterpart at all in the reference and no exit row, which would make the company an eternal member.
**Decision.** See ADR-004 `supported` flag. These 14 intervals remain in the event log and in `/companies/{q}` output, marked `supported=false`.

## ADR-015 Reason classification is rule-based and never guesses
**Decision.** An ordered regex rule list maps the cited reason text to a 12-value vocabulary; empty or unmatched text is `other_unknown`. Reference-only events carry no reason. Model-based classification was rejected for reproducibility.
