# EC2 Runbook — Lead Opportunity Platform

Operating the scraper on `15.207.68.78`. Written for the person on call at 3am,
so every section says what to type and what the output means.

---

## 1. Where everything is

| | |
|---|---|
| Repo | `~/Deployment/lead-opportunity-platform` |
| Service | `lead-scanning-api` (Supervisor) |
| API | `http://127.0.0.1:8001` |
| Database | `backend/data/opportunities.db` (SQLite, WAL) |
| Scraper log | `backend/logs/scraper.log` |
| Error log | `backend/logs/errors.log` |
| Supervisor stderr | `logs/supervisor-err.log` |
| Schedule state | `backend/data/schedule.json` |
| Secrets | `backend/.env` — **never** printed, copied or committed |

---

## 2. Deploy

```bash
cd ~/Deployment/lead-opportunity-platform && BOOT_TIMEOUT=600 ./deploy/update.sh
```

`update.sh` resets to `origin/main`, rebuilds the dashboard, publishes it,
restarts the service and polls until the API answers.

**Raise `BOOT_TIMEOUT` whenever migrations are pending.** Startup runs the
migrations, the FTS check and the column backfills against the whole database.
On a 177 MB file that is well over the default 180s, and the script has
reported a false failure for exactly that reason before — which arrives
precisely when someone is deciding whether to roll back, and argues for rolling
back a good deploy.

Verify it is running the new code with an endpoint that did not exist before:

```bash
curl -s http://127.0.0.1:8001/api/scraper-health | head -c 200
```

A `404` means the restart did not pick up the new code.

---

## 3. Environment settings that change behaviour

Set in `backend/.env`. Defaults are the safe values.

| Setting | Default | What it does |
|---|---|---|
| `LOP_SCHEDULER_CATCHUP_ON_RESTART` | `false` | Run a missed scrape on boot. Off: a restart must never start a scrape. |
| `LOP_DEVAID_INCLUDE_ARCHIVE` | `false` | Walk DevelopmentAid's historical archive. **This is the setting that made runs find 779,856 records to save 55,013.** One-off backfills only. |
| `LOP_DEVAID_MAX_SLICES` | `600` | Search partitions per section. |
| `LOP_DEVAID_MAX_DURATION_S` | `1800` | 30 min per section. |
| `LOP_DEVAID_MAX_RECORDS` | `20000` | Rows handed off per section. |
| `LOP_SOURCE_TIMEOUT_S` | `2700` | 45 min per source. Above the largest legitimate source, not near the average. |
| `LOP_RUN_TIMEOUT_S` | `21600` | 6 h for the whole run. |
| `LOP_HEALTH_FAILURE_STREAK` | `3` | Consecutive unhealthy runs before a source is called failing. |
| `LOP_HEALTH_STALE_DAYS` | `21` | Days without a saved row before a source is stale. |
| `LOP_VERTICAL_SPAN_SCORING` | `false` | Score verticals by distinct matched text. Re-tags a large share of the database — measure with `reclassify_preview.py --compare-scoring` first. |
| `LOP_READ_ONLY` | `false` | Mirror mode: refuses every write and scraper control. |

After any change: restart the service (§6).

---

## 4. Manual-only mode

The safest state. Nothing scrapes unless a person presses Start.

```bash
curl -s -X PUT http://127.0.0.1:8001/api/schedule \
  -H 'Content-Type: application/json' -d '{"mode":"manual"}'
```

Confirm:

```bash
curl -s http://127.0.0.1:8001/api/schedule
```

`"mode":"manual"` with `"next_run":null` means nothing is queued. Catch-up is
off by default, so a restart in this state also starts nothing.

## 5. Scheduling a real run

```bash
curl -s -X PUT http://127.0.0.1:8001/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{"mode":"daily","hour":2,"minute":0}'
```

`next_run` in the response is the truth. If it is null, the job did not
register and nothing will fire.

Only one scrape can run at a time across every process, enforced by a database
lease rather than by an in-process flag — two Gunicorn workers, a redeploy
mid-run, or a manual start during a scheduled one all block on the same lease.

---

## 6. Service control

```bash
sudo supervisorctl status lead-scanning-api
sudo supervisorctl restart lead-scanning-api
sudo supervisorctl stop lead-scanning-api
```

`stopasgroup=true` and `killasgroup=true` are set on purpose: they take the
browser subprocesses down with the service. **Do not remove them** — without
them a stop leaves orphaned Chromium holding memory.

Keep Gunicorn at **one worker** while the scheduler runs in-process. Two
workers means two schedulers, which means every automatic email goes out twice.

---

## 7. Stopping a stuck run safely

**Ask it nicely first.** The run stops between pages and unwinds its browsers.

```bash
curl -s -X POST http://127.0.0.1:8001/api/stop
curl -s http://127.0.0.1:8001/api/progress | head -c 300
```

Watch `state`. It should pass through `stopping` to `finalizing` to `idle`.
`finalizing` is not stuck — it is whole-database maintenance after the sources
have finished, and it can take minutes on a large database.

**If it will not stop**, the lease is what to clear — restarting the service
alone leaves the lease held and the next run refuses to start:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
source .venv/bin/activate
python -c "from app.services import run_lock; print(run_lock.force_release())"
sudo supervisorctl restart lead-scanning-api
```

On the next boot, startup recovery reconciles whatever was left `running`.

---

## 8. Verifying no orphan Chrome remains

Take the baseline **before** a run:

```bash
pgrep -fc "chrome|chromium" || echo 0
```

After the run finishes, fails, is stopped or times out, the count must return
to that number. Check on every one of those paths, not just the happy one.

```bash
pgrep -af "chrome|chromium" | head
```

If any remain with no scrape running:

```bash
pkill -f "chromium.*--headless" ; sleep 2 ; pgrep -fc "chrome|chromium" || echo 0
```

Then record it — a leak that needs manual cleanup is a bug, not a chore.
`site_auth.close_owned()` closes the context *and* its owning browser, which is
what the leak was; a recurrence means a new code path is not using it.

---

## 9. How stale runs are recovered

A run holds a lease and beats a heartbeat every 30s. On startup,
`run_startup_recovery()` reconciles anything left behind:

* `running` **with** a `finished_at` → the appropriate terminal state
* `running` **without** a recent heartbeat (>15 min) → `STALE_RUN_RECOVERED`

A recovered run gets `finished_at = heartbeat_at or started_at` — never `now`,
which would claim it ran until the restart.

Check what recovery did:

```bash
grep -i "recover\|stale" backend/logs/scraper.log | tail -20
```

---

## 10. Health and memory checks

```bash
curl -s http://127.0.0.1:8001/api/scraper-health | python3 -m json.tool | head -40
free -m
ps -o pid,rss,cmd -C python3 --sort=-rss | head -5
du -h backend/data/opportunities.db*
```

Source states and what to do about each:

| State | Meaning | Action |
|---|---|---|
| `ok` | Producing recently | none |
| `stale` | No row saved in 21+ days | check the source by hand; the site may have moved |
| `never_produced` | Runs recorded, no row ever | parser or access problem — read `last_error_message` |
| `failing` | 3+ consecutive unhealthy runs | read the outcome; `NO_FETCH` is access, `PARSE_ZERO` is the parser |
| `unknown` | No run has recorded an outcome | it has not run since the outcome columns landed |

`CONFIRMED_EMPTY` is **healthy**. The source proved it has nothing to list.
It keeps getting a cheap first-page check so the next opportunity is picked up
automatically.

---

## 11. Reading a zero-result run

Never guess why a run produced nothing — the outcome says.

| Outcome | Means | First thing to check |
|---|---|---|
| `NO_FETCH` | No usable page or API response | HTTP status, final URL — 403 is access, timeout is network |
| `PARSE_ZERO` | Page loaded, parser found nothing | `backend/data/debug/` capture; compare against `tests/fixtures/` |
| `STRUCTURE_CHANGED` | Positive evidence of drift | the saved fixture, and add a failing test before fixing |
| `CONFIRMED_EMPTY` | The source proved it is empty | nothing |
| `AUTH_REQUIRED` / `SESSION_EXPIRED` | Login needed or lapsed | re-connect the account; never script the login |
| `BLOCKED` | Cloudflare or a bot wall | do not work around it — prefer an official API |
| `TIMED_OUT` | Hit the per-source or run cap | was it slow, or wedged? |
| `CANCELLED` | Somebody pressed stop | nothing |

```bash
grep -E "NO_FETCH|PARSE_ZERO|STRUCTURE_CHANGED|CONFIRMED_EMPTY" \
  backend/logs/scraper.log | tail -20
```

---

## 12. A controlled single-source scrape

Never start an all-source run to test one thing.

```bash
cd ~/Deployment/lead-opportunity-platform/backend
source .venv/bin/activate
pgrep -fc "chrome|chromium" || echo 0          # baseline
python scripts/check_scraper.py worldbank --pages 2
pgrep -fc "chrome|chromium" || echo 0          # must match
```

---

## 13. Read-only diagnostics

All safe to run against production. Each reports before it writes, and the
write needs an explicit flag.

```bash
python scripts/db_baseline.py                        # the numbers everything else is measured against
python scripts/routing_audit.py                      # what each member is set up to receive
python scripts/classifier_precision.py               # what drives each vertical tag
python scripts/reclassify_preview.py --compare-scoring
python scripts/iso_inversion_audit.py                # deadlines that disagree with the source text
python scripts/project_rows_audit.py                 # stored rows that are projects, not notices
python scripts/listing_link_audit.py                 # rows linking to their own index page
python scripts/deadline_convention_audit.py          # day/month inversion by source
python scripts/gold_dataset.py --status              # is there enough labelled data to compare models
```

Add `--apply` (or `--archive`) only after reading the dry run.

---

## 14. Safety rules that are not negotiable

* **Never** print, copy or commit `.env`, cookies, session files or browser
  profiles. `.gitignore` covers `.env.*`; check `git status --short | grep env`
  returns nothing before every commit.
* **Never** delete opportunities. Expired and invalid rows are archived. A
  wrongly archived row comes back; a deleted one does not.
* **Never** work around a CAPTCHA, Cloudflare or an access control. If a source
  refuses automated access, the answer is an official API, a licensed feed, or
  dropping the source.
* **Never** script a login for a site whose terms forbid it. Sessions are
  connected by a person in a real browser.
