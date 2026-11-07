# Public Web Opportunity Discovery

## What the platform can promise

The discovery source searches Brave's **publicly indexed web** for grants,
RFPs, RFQs, tenders, calls for proposals, expressions of interest, fellowships
and challenges relevant to the Catalysts brands and CMS Devsol verticals.

It cannot honestly promise every active opportunity on the entire internet.
No search index contains private portals, unindexed pages, login-only content,
blocked pages, or every newly published page immediately. The safe promise is:

> The platform regularly discovers relevant, publicly available and publicly
> indexed opportunities, then validates each candidate against its own source
> page before placing it in the dashboard.

Maintained first-party website scrapers remain the authoritative route for
known portals. Public Web Discovery complements them; it does not replace them.

## How a result reaches the dashboard

1. Run balanced searches across India, South Asia and global opportunities.
2. Search broad opportunity terms and narrower grant/procurement/call families.
3. Paginate each Brave query, subject to per-run and monthly request limits.
4. Fetch the candidate's own public page.
5. If the page is only a JavaScript shell, render a small bounded number with
   Playwright/Chromium and inspect the rendered page.
6. Require a readable current/future deadline or an explicit rolling deadline.
7. Reject news, jobs, projects, past awards and non-opportunity pages.
8. Apply the existing category, vertical, brand, spam and duplicate rules.
9. Save accepted records under source `Whole Web Discovery`.

The latest detailed run report is written to
`backend/data/debug/web_discovery_last_run.json`. The persistent monthly API
request ledger is `backend/data/web_discovery_usage.json`.

## Configuration

Keep the Brave key only in `backend/.env` (never Git or the frontend):

```dotenv
LOP_WEB_DISCOVERY_ENABLED=true
LOP_BRAVE_SEARCH_API_KEY=replace-with-your-real-key
LOP_WEB_DISCOVERY_MAX_QUERIES=72
LOP_WEB_DISCOVERY_RESULTS_PER_QUERY=20
LOP_WEB_DISCOVERY_MAX_PAGES_PER_QUERY=10
LOP_WEB_DISCOVERY_REQUEST_BUDGET_PER_RUN=720
LOP_WEB_DISCOVERY_MONTHLY_REQUEST_LIMIT=10000
LOP_WEB_DISCOVERY_FETCH_CONCURRENCY=4
LOP_WEB_DISCOVERY_INTERVAL_DAYS=3
LOP_WEB_DISCOVERY_FRESHNESS=
LOP_WEB_DISCOVERY_REQUIRE_DEADLINE=true
LOP_WEB_DISCOVERY_JS_RENDER_ENABLED=true
LOP_WEB_DISCOVERY_JS_RENDER_LIMIT_PER_RUN=30
```

Blank freshness searches Brave's full public index. This is now the default
because an opportunity can be published more than 31 days before its deadline.
The source-page deadline gate still admits only active or rolling calls. Set
`pm` only when deliberately choosing recent-month coverage over completeness.

## Usage and cost guardrails

Brave Web Search is metered by **API requests, not LLM tokens**. One query page
is one request. A retry also counts, which is why the platform records each
attempt before sending it.

The high-coverage profile runs the complete 72-query matrix and allows all ten
Brave result pages, for a hard ceiling of 720 requests in one run. Actual usage
is normally lower because each query stops as soon as Brave reports no next
page. The monthly ledger still prevents accidental unlimited use; its default
ceiling is 10,000 requests. Change the per-run or monthly limits in `.env` if
your Brave subscription has a lower allowance. Manual daytime tests consume
the same ledger, and the JSON report records exact request usage and the stop
reason.

## Run a daytime manual test

No night window applies to a manual admin run:

1. Deploy/restart the backend after adding the key and settings.
2. Open the admin dashboard and the scraper controls.
3. Select only `Whole Web Discovery`.
4. Press **Start**.
5. Watch its live source row, then inspect the run report for query/page counts,
   accepted candidates, JavaScript recoveries and budget usage.
6. Confirm new rows using the source filter `Whole Web Discovery`.

Ordinary registered source scrapers continue daily. Only public-web discovery
uses the separate every-three-days automatic cadence.

## JavaScript pages

The platform uses Playwright with headless Chromium for a limited fallback.
This executes page JavaScript and reads the rendered DOM, which covers many
React, Angular, Vue and other client-rendered listings. It is limited to thirty
candidate pages per run and runs one browser at a time. The shared teardown
closes both context and Chromium so processes do not accumulate on EC2.

This is not a universal bypass. It will not defeat CAPTCHAs, authentication,
paywalls, robots restrictions or anti-bot controls. Where available, prefer a
publisher's documented API, RSS/Atom feed, sitemap, structured JSON endpoint or
dedicated first-party scraper. Useful technologies are Playwright, browser
network inspection for public JSON/XHR endpoints, JSON-LD, sitemaps and feeds.

The existing `deploy/update.sh` installs the matching Chromium runtime as part
of the normal one-command deployment. For diagnosis, the equivalent command is:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
./.venv/bin/python -m playwright install chromium
```

## PostgreSQL migration path

The current production database is SQLite. PostgreSQL is the right next step
when write concurrency, history volume, analytics or multi-instance deployment
outgrow one EC2-hosted database file. It is a controlled migration, not simply
changing one environment variable, because the current database setup includes
SQLite PRAGMAs and FTS5 search.

Recommended steps:

1. Take and verify a SQLite snapshot; keep the application on SQLite.
2. Provision PostgreSQL (preferably AWS RDS), a private database/user, backups,
   encryption and an EC2-to-database security-group rule.
3. Add the PostgreSQL driver and versioned schema migrations.
4. Replace SQLite-only PRAGMAs and FTS5 with PostgreSQL equivalents, including
   full-text/trigram indexes used by opportunity search.
5. Load a copy while preserving opportunity IDs and all related user, lead,
   history, classification and source-health relationships.
6. Validate row counts, foreign keys, duplicate groups, expired-row rules,
   search results and dashboard totals against SQLite.
7. Rehearse the cutover and rollback on a staging copy.
8. Stop scraper/API writes briefly, copy the final delta, change
   `LOP_DATABASE_URL`, restart the service and run the same audits again.
9. Retain the final SQLite snapshot until production validation is signed off.

Do not import only the opportunities table: that would break related dashboard
and user-workspace data. PostgreSQL migration should be its own deployment round.

## EC2 deployment

After the code is published, edit the existing server environment file and add
the settings above:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
nano .env
cd ~/Deployment/lead-opportunity-platform
./deploy/update.sh
```

Then use the admin manual test flow. Never paste the real Brave key into logs,
screenshots, source files or frontend variables.
