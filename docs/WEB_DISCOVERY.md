# Whole Web Discovery

## What it does

`Whole Web Discovery` searches a general public-web index for active grants,
RFPs, RFQs, tenders and calls for proposals. It complements the maintained
website scrapers; it does not replace them.

Every discovered result follows the existing ingestion pipeline:

1. Search across India first, South Asia second and global opportunities third.
2. Fetch the result's own page without executing JavaScript.
3. Require a readable current/future deadline or an explicit rolling deadline.
4. Reject news, jobs, projects, past awards and page furniture.
5. Classify category, vertical and brand through the existing dashboard logic.
6. Apply exact and cross-source duplicate checks.
7. Save accepted rows with source `Whole Web Discovery`.

The source uses Brave Search's supported Web Search API rather than scraping a
search-results webpage. The API key remains server-side.

## Configure locally

Create a Brave Search API key at <https://brave.com/search/api/>. Then add these
values to `backend/.env`:

```dotenv
LOP_WEB_DISCOVERY_ENABLED=true
LOP_BRAVE_SEARCH_API_KEY=replace-with-your-real-key
LOP_WEB_DISCOVERY_MAX_QUERIES=18
LOP_WEB_DISCOVERY_RESULTS_PER_QUERY=20
LOP_WEB_DISCOVERY_FETCH_CONCURRENCY=4
LOP_WEB_DISCOVERY_FRESHNESS=pm
LOP_WEB_DISCOVERY_REQUIRE_DEADLINE=true
```

Restart the backend after changing `.env`. In the scraper controls, select
`Whole Web Discovery` and start a manual run. The latest diagnostic report is
written to:

```text
backend/data/debug/web_discovery_last_run.json
```

Use `LOP_WEB_DISCOVERY_FRESHNESS=py` for a deliberate one-off initial search of
the past year. Change it back to `pm` for routine scheduled runs.

## Configure EC2

After deploying the code, edit the existing server environment file:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
nano .env
```

Add the same settings, save the file, and run the existing deployment workflow:

```bash
cd ~/Deployment/lead-opportunity-platform
./deploy/update.sh
```

The key must not be committed to Git or placed in frontend environment files.
