# Low-Level Design

## 1. Data model

### 1.1 Build artefacts (`data/build/`)
| File | Grain | Notes |
|---|---|---|
| `companies.csv` | company | `company_id`, `canonical_name`, `name_key`, `current_ticker`, `all_tickers`, `cik`, `needs_review`, `notes` |
| `company_names.csv` | company × name | `first_seen` |
| `company_tickers.csv` | company × ticker | `valid_from`/`valid_to` (derived spans), `evidence_kinds`, first/last evidence |
| `events.csv` | event | the event log (schema below) |
| `intervals.csv` | membership interval | derived; `supported`, `left_censored`, exit reason |
| `candidates.csv` | per-source claim | audit trail for clustering |
| `sector_observations.csv` | company × date | GICS sector/sub-industry as observed on a dated page |
| `issues.csv` | anomaly | state-machine decisions |
| `parse_problems.csv` | unparseable row | |
| `resolution_log.jsonl` | decision | rule, inputs, output |
| `reconciliation/{report.md,discrepancies.csv}` | disagreement range | |
| `build_manifest.json` | build | input hashes, counts |

### 1.2 Database (`snp500/db/models.py`)
```
companies(company_id PK, canonical_name, name_key, current_ticker, cik, needs_review, notes)
company_names(id, company_id FK, name, first_seen)
company_tickers(id, company_id FK, ticker, valid_from, valid_to, evidence_kinds, first_evidence, last_evidence)
constituent_events(event_id PK, effective_date, action, company_id FK, ticker_at_event, company_name,
                   reason, reason_category, counterpart_company_id FK, source_url, source_type,
                   scraped_at, raw_sha256, confidence, corroborating_sources, refs, notes, in_window)
membership_intervals(id, company_id FK, entry_date, exit_date, entry_event_id, exit_event_id,
                   entry_ticker, exit_ticker, exit_reason_category, left_censored, supported)
company_sector_observations(id, company_id FK, ticker, as_of_date, gics_sector, gics_sub_industry, source_url, source_type, scraped_at)
build_issues(id, company_id, kind, on_date, detail)
reconciliation_discrepancies(id, comparison, severity, kind, ticker, company_id, company_name, first_date, last_date, n_dates)
raw_snapshots(id, source, url, fetched_at, sha256, path, status, n_bytes, note)
build_runs(id, built_at, loaded_at, manifest_json)
```
Indexes: `effective_date`, `action`, `company_id`, `reason_category`, `source_type`; `(entry_date, exit_date)`; ticker; `supported`.

### 1.3 Event schema
`event_id` = `"E" + sha1(company_id|effective_date|action|ticker)[:14]`. `action ∈ {ADD, REMOVE, TICKER_CHANGE}`. `reason_category ∈ {acquisition, merger, bankruptcy, spinoff, delisting, going_private, market_cap_eligibility, index_rebalancing_reclassification, corporate_restructuring, ticker_or_name_change, new_listing, other_unknown}`.

## 2. Identifiers

* `company_id = "C" + sha1(entity_key)[:10]`, where `entity_key` is the curated `entity_key` if the canonical name has one, else `normalize_name(canonical_name)`, else (ticker-only identity) `unknown-ticker:<T>:<first date>`.
* Name normalisation (`normalize/text.py`): NFKD → ASCII, lower-case, drop parentheticals, `&`→`and`, strip punctuation, iteratively strip *legal-form* suffixes only (inc, corp, company, co, ltd, plc, llc, lp, holdings, sa, nv, ag, se, class a/b/c). Descriptive words (Financial, Industries, Bancorp) are kept.
* Ticker normalisation: upper-case, `-`/`/` → `.`, strip footnotes and stray `|`, validate `^[A-Z0-9]{1,6}(\.[A-Z])?$`.

## 3. Raw store and fetcher

`RawStore.put(source, url, body, ...)` → `data/raw/<source>/<YYYYMMDDTHHMMSSZ>_<sha256[:10]>.<ext>` + manifest line. Identical body for the same URL re-uses the file (still logged). `read_bytes` verifies sha256. `Fetcher.fetch(source, url, max_age, force)` returns cached text when a snapshot younger than `max_age` exists (`max_age=None` = any). Tenacity retries on 429/5xx/network with exponential backoff (2–30 s, 4 attempts). Throttle: `1 / requests_per_second`.

## 4. Parsers

* `parse_constituents(html)`: finds `table#constituents`, else the first table with ≥100 rows whose header contains a ticker and name column. Column roles are matched by header *meaning* (`symbol|ticker`, `securit*|company|name`, `sector|industry`, `sub-industry`, `headquarters|location`, `date … added|first`, `cik`, `founded`) so 2007–2026 layouts all parse; a missing name header falls back to the column after the ticker (seen in a 2025 revision with header "Securit").
* `parse_changes(html)`: `table#changes`; skips header rows; requires ≥6 cells; rejects rowspans (reported); resolves `#cite_note` anchors to external URLs.
* Both return `problems` (row text + error) instead of dropping rows.

## 5. Candidate generation (`reconstruct/events.py`)

| Source | Candidates | Confidence |
|---|---|---|
| changes table row | ADD (added ticker) and/or REMOVE (removed ticker), reason text classified, counterpart ticker recorded | 0.95 with refs, 0.90 without |
| current list "Date added" | ADD at that date, ticker = today's ticker (later re-labelled to the ticker valid on that date when known) | 0.75 |
| reference intervals | ADD at interval start (left-censored opening at the dataset's first date), REMOVE at interval end | 0.60 / 0.50 |
| curated overrides | ADD/REMOVE as specified | 0.99 |

## 6. Entity resolution (`reconstruct/entity_resolution.py`)

Company state: `names{name → first_seen}`, `evidence{ticker → [(date, weight, kind)]}`, `claims[(date, ADD|REMOVE, source)]`, derived `tickers[TickerSpan]`.

Evidence kinds/weights: `snapshot`, `remove`, `current`, `curated` = 1.0; `reference` = 0.8; `add` = 0.5.

`resolve_named(ticker, name, on, source, kind)` applies, in order:
1. **Curated alias** → canonical company (authoritative; create if absent).
2. **Curated ticker change** (`new_ticker` near `on`, kind=add): fold the old ticker's holder in; mark ticker change.
3. **Same-row ticker change** (reason classified as ticker change and names compatible).
4. **Ticker continuity**: evidence for the ticker within 400 days with no REMOVE claim in between; fallback to the unique last holder not removed since (for page rows, names may differ).
5. **Name key** match (unless in `distinct_entities`). If name and ticker disagree: ticker wins when the name-key company left the index after its last sighting (acquirer adopted the target's name), or when names are compatible; an ADD under a ticker held by a differently-named live member is a *replacement*, not the same company.
6. **Re-entry**: same ticker, compatible name, company previously removed.
7. **REMOVE fallback**: unique company that ever held the ticker, membership-plausible, name-compatible.
8. **Create**.

`resolve_reference_interval(ticker, start, end, exclude)`: candidates = companies with any evidence for the ticker whose claims do not exclude the interval (`membership_excludes`: interval entirely after a final REMOVE, or entirely before a first ADD, with 45-day tolerance); winner = highest evidence weight inside the interval (±45 days); ties or zero-evidence ambiguity → unresolved.

`finalize_ticker_spans(today)`: per ticker take first/last date from the most reliable evidence kind available; order by first date; a later ticker *replaces* an earlier one only if the earlier one was not seen after the later one first appeared (or its last sighting is the remove-half of the change that day); otherwise they are concurrent share classes. Earliest span starts `NULL` (unknown), last span is open if the company is a current member.

## 7. Membership build (`reconstruct/membership.py`)

**Phase 1 – named observations** in chronological order (changes rows at their effective date, page revisions at their date, today's list at today). ADD before REMOVE within a date so same-row ticker changes see the still-active counterpart. Share-class retirements (a REMOVE while another ticker of the same company has evidence within 30 days after) become `TICKER_CHANGE`. `date added` claims are attached to the company holding the ticker today. Curated ticker changes then **seed** evidence and `TICKER_CHANGE` candidates even when no source row mentions them (SBC → T).

**Phase 2 – reference intervals.** (a) Split intervals for a curated *new* ticker at the change date (the head cannot belong to the adopting company). (b) Resolve each interval by evidence; if unresolved try the bankruptcy-suffix convention (`CITGQ` → `CIT`), then date pairing: **H3** same-date Wikipedia ADD under a ticker the reference never adds near then; **H2** same-date Wikipedia REMOVE likewise; **H1** same-day reference swap X→Y where Y's company is known to have been a member ≥30 days earlier (rename). (c) **H4** same-day swaps resolved to two different companies with no Wikipedia add/remove of those tickers near the date → merge (rename). (d) **H5** a page-snapshot company whose ticker the reference never lists, wholly covered by one reference interval of a company first seen right after it (≤ 431 days), where that ticker was seen by nobody during the period, and the snapshot company was never removed → merge (back-filled rename); ambiguous cases are logged, not merged. (e) Emit ADD/REMOVE candidates from the **union** of each company's assigned intervals (adjacent within 10 days), so back-filled overlaps (AABA + YHOO) do not create spurious exits.

**Phase 3 – clustering.** Per (company, action), candidates within 10 days form one event: primary = highest confidence (ties: non-reference, named); confidence += 0.05 per other source type; notes list the other sources and their dates; counterpart company resolved from the counterpart ticker.

**Phase 4 – state machine** per company, ordered by date then ADD < TICKER_CHANGE < REMOVE:
* ADD with no open interval → open.
* ADD while open: fill an unknown start; a changes-table ADD supersedes a "date added" start when the reference does not show the company earlier (`entry_date_superseded`); otherwise record `entry_after_opening` (left-censored reference start kept) or `conflicting_entry_date` / `duplicate_add`.
* REMOVE while open → close; same date as entry → zero-length interval + `same_day_add_remove`.
* REMOVE with no open interval → `remove_without_add`; interval left-censored, bounded below by the previous exit.
* TICKER_CHANGE → kept in the log, no membership effect.

**Phase 5 – support flag.** Interval `supported` iff it overlaps (±10 days) any reference interval assigned to the company (when the reference is used).

## 8. Reconciliation (`reconcile/engine.py`)

* Dates: every reference change date ≥ 2001-01-01 plus quarter-ends.
* Company level vs reference: reference tickers → companies via the phase-2 assignment; compare sets; report `missing_in_ours` / `extra_in_ours`.
* Ticker level vs page revisions: compare our `tickers_on(d)` (dots stripped) with the page's tickers.
* Collapse consecutive dates per (comparison, kind, ticker, company) into ranges; `severity = minor_date_disagreement` if the range spans ≤ 10 days, else `membership`.

## 9. Query layer (`api/queries.py`)

`get_constituents(d)`: active intervals (see ADR-008) joined to companies; tickers via validity ranges; sector via nearest observation (≤ 400 days before, else ≤ 400 days after). `get_changes`, `get_company_history` (lookup by id, any historical ticker, or name substring), `get_longest_memberships`, `get_companies_that_exited`, `turnover_by_year`, `exit_reasons_by_year`, `sector_composition`, `membership_duration_stats`, `survival_curve` (Kaplan–Meier over entrants, still-members censored), `cemetery`, `data_quality`.

## 10. Invariants (checked by tests)

1. A ticker change never produces an ADD/REMOVE pair for the same company.
2. Re-entry produces two disjoint intervals for one `company_id`.
3. A REMOVE dated `d` means the company is not a member on `d`.
4. No claim is dropped without an entry in `problems`, `issues` or the log.
5. Same inputs → identical `events.csv` (IDs included).
6. `supported = false` intervals never appear in default point-in-time answers.
