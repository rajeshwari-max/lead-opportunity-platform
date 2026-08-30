# Before / after

**Status: the "before" column is measured. The "after" column cannot be filled
until the fixes are deployed and the diagnostics are run.**

Every "after" figure below is marked either as a *test-verified* result (proven
in the suite, not yet on production data) or as *pending a run*. Nothing is
estimated. A report that guessed at these numbers would be the same class of
error as a scraper reporting `completed` for a run that fetched nothing.

---

## Baseline, measured 2026-08-29 on the live 177 MB database

| Metric | Before |
|---|---|
| Opportunities | 106,854 |
| Active | 16,303 |
| Expired | 90,551 (85%) |
| **Active with a past deadline** | **1,481** |
| Active with no deadline | 3,021 |
| Active with no vertical | 7,002 |
| Scrape runs recorded | 916 |
| — completed | 792 |
| — **stuck in `running`** | **106** |
| — stopped | 18 |
| — **failed, ever** | **0** |
| Sources that never produced a row | 16 (across 127 runs, all "completed") |
| Producing sources stale 21+ days | 47 of 75 |
| Sources fresh | 9 |
| Rows from the top 4 sources | 98.1% |
| DevelopmentAid: found → saved | 779,856 → 55,013 (93% discarded) |
| Distinct source names for 85 sources | 91 (renames fragmented run history) |

Reproduce with `python scripts/db_baseline.py` (read-only, `PRAGMA query_only`).

---

## After — test-verified, not yet measured in production

| Metric | Before | After | Evidence |
|---|---|---|---|
| Active past-deadline rows visible in any working view | 1,481 | **0 by construction** | One `actionable_clause()` used by the table, approved view, stats, search, exports and email. 18 tests in `test_actionable.py`, including that a `rolling` state cannot resurrect a passed date. |
| Runs that can be stuck in `running` | 106 | **0 reachable** | Lease + 30s heartbeat; `run_startup_recovery()` reconciles on boot. Recovered runs get `finished_at = heartbeat_at`, never `now`. 16 tests. |
| Runs that can end without a terminal state | any | **0** | Per-source 45 min and whole-run 6 h timeouts. A timed-out run records `TIMED_OUT`, not `completed`. |
| Zero-result runs reported as success | 792 | **0** | 13-outcome taxonomy; `CONFIRMED_EMPTY` requires positive proof, never a bare parser zero. 22 tests. |
| Concurrent scrapes across processes | possible | **0** | Cross-process SQLite lease; manual and scheduled starts share it. |
| Orphaned Chromium after stop/timeout/failure | leaked | **0** | `close_owned()` closes the context and its owning browser on every exit path. 9 tests with a fake process table. |
| DevelopmentAid archive walked per scheduled run | every run | **never by default** | `LOP_DEVAID_INCLUDE_ARCHIVE=false`, plus three execution caps. 29 tests. |
| ISO deadlines inverted by `dayfirst=True` | all with day ≤ 12 | **0** | `dateutil` applies `dayfirst` to ISO; the parser now reads `YYYY-MM-DD` as itself. |
| Contract awards and projects accepted from World Bank | all | **rejected** | The scrapers pass the source's own notice type; the contract excludes `contract_award` and `project`. |
| Automated tests | 0 | **456** | `python -m pytest tests -q` |

---

## Pending a production run

These need the deploy plus a script run. Each script reports before it writes.

| Question | Script | State |
|---|---|---|
| How many stored deadlines disagree with the source's own text? | `iso_inversion_audit.py` | not run |
| How many stored rows are projects or awards? | `project_rows_audit.py` | not run |
| What would the classifier pruning re-tag? | `reclassify_preview.py --compare-scoring` | not run |
| How many rows link to their own index page? | `listing_link_audit.py` | not run |
| Which sources show day/month inversion? | `deadline_convention_audit.py` | not run |
| How many duplicate groups would the re-key merge? | `rekey_opportunities.py` | dry run done (3,335 groups); **not applied** |
| Browser count before/after a controlled run | `pgrep -fc "chrome\|chromium"` | not measured on EC2 |
| Runtime and memory | `free -m`, `ps -o rss` | not measured on EC2 |
| Classifier quality metrics | `classifier_eval.py` | **blocked — no labelled data** |

---

## How to complete this report

```bash
# 1. deploy
cd ~/Deployment/lead-opportunity-platform && BOOT_TIMEOUT=600 ./deploy/update.sh

# 2. re-measure the baseline on the migrated database
cd backend && source .venv/bin/activate
python scripts/db_baseline.py > /tmp/after.txt

# 3. the repairs, each dry-run first
python scripts/iso_inversion_audit.py
python scripts/project_rows_audit.py
python scripts/listing_link_audit.py
python scripts/reclassify_preview.py --compare-scoring

# 4. a controlled single-source run with a browser baseline
pgrep -fc "chrome|chromium" || echo 0
python scripts/check_scraper.py worldbank --pages 2
pgrep -fc "chrome|chromium" || echo 0

# 5. classifier — only after labelling
python scripts/gold_dataset.py --status
```

Paste each output into the table above. The point of the report is that the
after column is measured on the same database the before column came from —
an after column filled from tests is a statement about the code, and this
document already says which rows are which.
