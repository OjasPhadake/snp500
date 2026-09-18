# Operations: running, curating, deploying

## Commands
```bash
snp500 scrape [--force] [--no-revisions] [--start-year 2007]   # append to data/raw
snp500 build [--no-reference]                                  # offline, deterministic
snp500 load [--database-url URL]                               # atomic table swap
snp500 serve [--host] [--port]                                 # API + dashboard
snp500 all                                                     # scrape -> build -> load
pytest -q
```
Configuration: environment variables prefixed `SNP500_` or a `.env` file (see `.env.example`): `DATABASE_URL`, `RAW_DIR`, `USER_AGENT`, `REQUESTS_PER_SECOND`, `WINDOW_START/END`.

## The curation loop
1. `snp500 build` and open `data/build/reconciliation/report.md`.
2. Start with **Membership disagreements** (ranges > 10 days) and **Build issues** of kind `remove_without_add`, `unsupported_interval`, `conflicting_entry_date`.
3. For each, inspect the evidence with a one-off script or the API (`/companies/{ticker}`), find the primary source (S&P announcement, 8-K, press release), and add a row to the appropriate curated file **with the URL**:
   * same company under a new ticker → `ticker_changes.csv`
   * two names for one company, or one name for two companies → `company_aliases.csv` (use `entity_key` to force separation)
   * a wrong Wikipedia row → `event_overrides.csv` (`suppress` the row, `add` the verified event)
4. Re-run `snp500 build`; confirm the range disappears and no new ones appear (diff `discrepancies.csv`).
5. Commit the curated change together with the regenerated `data/build`.

## Adding a new source
Implement a parser returning rows + problems, a `candidates_from_<source>` function with a confidence in `SOURCE_CONFIDENCE`, and register the fetch in `scraping/sources.py`. Named sources go through phase 1 (`resolve_named`); ticker-only sources through phase 2. Add the source to the reconciliation comparisons if it is point-in-time.

## PostgreSQL
```bash
docker compose up -d postgres
export SNP500_DATABASE_URL=postgresql+psycopg2://snp500:snp500@localhost:5432/snp500
snp500 load && snp500 serve --host 0.0.0.0
```
The loader creates the schema (`Base.metadata.create_all`) and replaces derived tables inside one transaction. Alembic is listed for future migrations; the schema is currently rebuilt from artefacts, so migrations are unnecessary.

## Scheduling
Run `snp500 all` weekly. Because the raw store is append-only and page revisions are immutable, repeated runs only add the day's current pages; the build stays reproducible for any past manifest by pointing `RAW_DIR` at a checkout of that commit.

## Known limitations / future work
* Pre-2008 exits are frequently identities known only from a ticker (245 companies, mostly 1996–2007). Naming them needs older page revisions (none have a table before mid-2007) or S&P press archives.
* The reference dataset collapses some legal entities into one back-filled ticker lineage (SBC/AT&T, ACE/Chubb, Actavis/Allergan, Praxair/Linde); the reconstruction keeps them separate and reports the difference as `extra_in_ours`.
* Multi-class share lines are modelled as concurrent tickers of one company; the index's own count of *lines* (503–505) is therefore higher than the company count reported here.
* GICS sector history begins with the 2008 page revision; 2001–2007 sectors are reported as not observed rather than guessed.
* SEC EDGAR 8-K/`FORM 25` parsing for exact delisting dates and S&P DJI announcement PDFs are natural next sources; the candidate/confidence framework accepts them without changes to the core.
