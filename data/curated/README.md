# Curated resolution hints

Small, hand-maintained CSVs that the entity-resolution step consults. Every row
must cite a source. These files are the *only* place where a human decision
overrides what the scraped sources say, and every override is visible in
`data/build/resolution_log.jsonl` (rule = `curated`).

| File | Purpose |
|---|---|
| `ticker_changes.csv` | `old_ticker -> new_ticker` on `effective_date` is the **same company** (rename/ticker change, not an index exit+entry). `effective_date` only needs to be within ±45 days of the date the sources show; the event date always comes from the source data. |
| `company_aliases.csv` | `alias_name` (as written by a source) refers to `canonical_name`. Used to merge renamed companies (Facebook → Meta Platforms) **and** to keep genuinely different companies apart when their names normalise identically (old Alcoa Inc. vs the 2016 spin-off Alcoa Corp). |
| `distinct_entities.csv` | Normalised names that must never be merged by name alone. |
| `event_overrides.csv` | Hand-verified ADD/REMOVE events (`mode=add`) or suppressions (`mode=suppress`) with a citation. Applied after clustering, before interval derivation. |

Workflow: run the pipeline → read `data/build/reconciliation/report.md` → for each
discrepancy find the primary source → add a row here with the URL → re-run.
