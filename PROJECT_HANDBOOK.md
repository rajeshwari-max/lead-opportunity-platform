# Lead Scanning Platform — Complete Technical Handbook

*Prepared for presentation. Covers what the system does, how every part works,
the full API, and the order in which you would build it from scratch.*

---

## Part 1 — What the system is

A **business-development lead scanner**. It automatically visits 75+ funding
websites (grant portals, tender boards, foundation pages), extracts every
funding opportunity, cleans and classifies each one, stores it in a database,
shows it in a dashboard, and emails the right opportunities to the right people
on the team every morning.

**The problem it solves:** a BD team was manually checking dozens of funder
websites for grants and tenders. That is hours a week, it is inconsistent, and
opportunities are missed because deadlines pass unnoticed.

**Current scale (live)**

| | |
|---|---|
| Opportunities stored | 70,646 |
| Currently active | 11,799 |
| Source websites | 75 |
| Scrape runs completed | 623 |

---

## Part 2 — Tech stack

### Backend — Python

| Library | Version | Why it is there |
|---|---|---|
| **FastAPI** | ≥0.111 | Web framework. Chosen for automatic OpenAPI docs and native async, which matters because scraping is I/O-bound. |
| **SQLAlchemy** | ≥2.0 | ORM. Python classes ↔ database tables, so no hand-written SQL. |
| **SQLite** | built in | Database. One file, zero server to run. Correct at this scale; the migration path to PostgreSQL is one connection string. |
| **Pydantic** | ≥2.7 | Request/response validation. A bad request is rejected before it reaches business logic. |
| **pydantic-settings** | ≥2.3 | Config from environment variables (12-factor). |
| **httpx** | ≥0.27 | Async HTTP client for fetching pages. |
| **BeautifulSoup4 + lxml** | ≥4.12 / ≥5.2 | HTML parsing. lxml is the fast C parser underneath. |
| **Playwright** | ≥1.45 | Headless browser for JavaScript-rendered sites that return an empty shell to plain HTTP. |
| **APScheduler** | ≥3.10 | In-process cron. Runs the nightly scrape and the 09:00 digest. |
| **openpyxl** | ≥3.1 | Excel export. |
| **Gunicorn + Uvicorn** | ≥22.0 | Production server. |

### Frontend — TypeScript

| Library | Version | Why |
|---|---|---|
| **React** | 18.3 | UI framework. |
| **TypeScript** | 5.4 | Types. Catches an API-shape mismatch at build time instead of as a blank screen. |
| **Vite** | 5.3 | Build tool and dev server. |
| **Tailwind CSS** | 3.4 | Utility-first styling. |
| **TanStack Table** | 8.17 | Headless table — sorting, resizing, column visibility. |
| **Recharts** | 2.12 | The donut and bar charts. |
| **lucide-react** | 0.400 | Icons. |
| **framer-motion** | 11.2 | Animation. |

### Infrastructure

- **AWS EC2** (Ubuntu 26.04) — the server
- **Nginx** — serves the built frontend, reverse-proxies `/api` to the backend
- **Supervisor** — keeps the backend running, restarts it on crash
- **Git / GitHub** — version control and deployment transport

---

## Part 3 — Architecture

```
                    ┌─────────────────────────────────┐
   75 funder        │  SCRAPERS                       │
   websites  ──────▶│  httpx (fast) or Playwright     │
                    │  (JS-rendered sites)            │
                    └───────────────┬─────────────────┘
                                    │ RawOpportunity objects
                                    ▼
                    ┌─────────────────────────────────┐
                    │  PROCESSING PIPELINE            │
                    │  normalise → filter → classify  │
                    │  → deduplicate → save           │
                    └───────────────┬─────────────────┘
                                    ▼
                    ┌─────────────────────────────────┐
                    │  SQLite  (+ FTS5 search index)  │
                    └────┬──────────────────────┬─────┘
                         │                      │
              ┌──────────▼─────────┐   ┌────────▼──────────┐
              │  FastAPI REST API  │   │  APScheduler      │
              │  38 endpoints      │   │  nightly scrape   │
              └──────────┬─────────┘   │  09:00 digest     │
                         │             └────────┬──────────┘
                         │ JSON over HTTP       │ SMTP
              ┌──────────▼─────────┐   ┌────────▼──────────┐
              │  React dashboard   │   │  Team inboxes     │
              └────────────────────┘   └───────────────────┘
```

### How frontend and backend connect

They are **completely separate programs** that only speak JSON over HTTP.

- **Development:** React on `:5173`, FastAPI on `:8000`. Vite proxies `/api/*`
  to `:8000`, so the browser sees one origin and there is no CORS problem.
- **Production:** Vite compiles React into static HTML/CSS/JS. Nginx serves
  those files on port 80 and forwards `/api/*` to Gunicorn on `127.0.0.1:8001`.
  One origin, so cookies work without CORS at all.

The contract between them is `frontend/src/lib/types.ts` — TypeScript interfaces
mirroring the Pydantic schemas. Change a field on the backend and the frontend
build fails, rather than the page silently rendering `undefined`.

---

## Part 4 — Database schema

Five tables. SQLite file at `backend/data/opportunities.db`, in WAL mode so
reads don't block writes.

### `opportunities` — the main table

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `unique_id` | VARCHAR(64) | **SHA-256 fingerprint, UNIQUE** — this is what prevents duplicates |
| `title` | TEXT | |
| `organization` | VARCHAR(512) | indexed — the funder |
| `country`, `region` | VARCHAR(128) | indexed, normalised |
| `vertical` | VARCHAR(256) | legacy free-text |
| `verticals` | VARCHAR(256) | indexed — canonical, comma-separated, **multi-label** |
| `work_type` | VARCHAR(32) | Research / Implementation |
| `study_type` | VARCHAR(32) | Baseline / Endline / Evaluation… |
| `category` | VARCHAR(10) | Grant / RFP / Tender / Proposal |
| `deadline` | DATE | indexed, nullable (NULL = "Ongoing") |
| `opportunity_url` | TEXT | link to the original listing |
| `summary`, `eligibility` | TEXT | |
| `funding_amount` | VARCHAR(256) | free text as the funder wrote it |
| `status` | VARCHAR(7) | indexed — Active / Expired |
| `source_website` | VARCHAR(128) | indexed — which board it came from |
| `date_scraped` | DATETIME | |
| `approved`, `approved_at`, `approved_by` | | the team's sign-off |

Plus a virtual **FTS5** table `opportunities_fts` giving full-text search across
title, summary and eligibility.

### `team_members`

| Column | Purpose |
|---|---|
| `name`, `email` | identity (email UNIQUE) |
| `keywords` | comma-separated; matched against title/summary/eligibility |
| `categories` | restrict to Grant / RFP / … |
| `verticals` | restrict to their verticals |
| `auto_send` | include in the automatic daily digest |
| `active` | **deactivating is how access is revoked** |

### `sent_log`

`(member_id, opportunity_id, sent_at)` — the memory that stops the same
opportunity being emailed to the same person twice.

### `reminder_log`

`(member_id, opportunity_id, days_before)` UNIQUE — stops a 10-day reminder
going out twice.

### `scrape_runs`

Per-source audit: pages scraped, found, saved, skipped, errors, status.

### `expert_counts`

Expert Pool figures per vertical from DevelopmentAid.

---

## Part 5 — How scraping works

### The scraper contract

Every scraper subclasses `BaseScraper` and must implement one method:

```python
class BaseScraper(ABC):
    name: str
    display_name: str
    start_url: str
    prefer_js: bool = False        # use a browser if Playwright is installed
    enrich_details: bool = False   # also visit each detail page

    @abstractmethod
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        ...

    def next_page(self, html, page_url, page_number) -> PageRequest | None:
        ...
```

Scrapers self-register at import time, so adding one is: write the file, and it
appears in the dashboard automatically.

### Two kinds of scraper

**1. Hand-written** (12 files) for complex sites: DevelopmentAid, FundsForNGOs,
Bond UK, NGOBOX, GrantWatch, DevNetJobs…

**2. Config-driven** (75 entries in `sources.json`) for ordinary listing pages:

```json
{
  "name": "world_bank",
  "display_name": "World Bank",
  "url": "https://projects.worldbank.org/en/projects-operations/opportunities?lang=en",
  "page_url": "https://projects.worldbank.org/...&os={offset}",
  "page_size": 10
}
```

One `GenericListingScraper` class is generated per entry at import time. Adding a
site is a JSON edit, not code.

### Fetching: two strategies

1. **httpx** — plain HTTP. Fast, cheap, the default.
2. **Playwright** — a real headless Chromium that executes JavaScript. Many
   modern funder sites return an empty shell to plain HTTP because the content
   is rendered client-side. `prefer_js=True` upgrades to a browser *if
   Playwright is installed*, and silently falls back if not.

### Pagination

Sites disagree about what the number in the URL means, so templates support two
dialects:

- `{page}` — 1-based page index (`?page=2` = second page)
- `{offset}` — 0-based row offset (`?os=10` = second page of 10)

> **Real bug this caught:** World Bank uses `os=` as an *offset* but was
> configured as `{page}`. The crawler asked for `os=2, 3, 4…`, sliding the
> window down one row at a time. Nine of every ten results were repeats, the
> stale-page counter tripped, and the source stopped after 38 rows **while
> reporting success**.

### Concurrency and safety limits

```python
max_concurrent_sources = 6     # websites scraped at once
concurrency_per_source = 4     # pages fetched at once within one site
rate_limit_delay      = 1.0    # polite pause between requests
max_pages_safety_cap  = 2000   # hard stop against infinite pagination
stale_page_streak     = 3      # stop after 3 pages with nothing new
```

`max_concurrent_sources` exists because running all 75 at once saturated the
event loop and froze the progress API.

### The processing pipeline

Every scraped row passes through this before it is saved:

```
normalise → filter → classify → deduplicate → save
```

1. **Parse the deadline** — dozens of date formats
2. **Reject sentinels** — `9999-12-31` means "no closing date", not a real date
3. **Classify category** — Grant / RFP / Tender / Proposal from title + summary
4. **Classify verticals** — 277-term keyword inventory, **multi-label** (one
   opportunity can be both Health and Livelihood)
5. **Classify work type** — Research vs Implementation (this decides routing)
6. **Classify study type** — Baseline / Endline / Evaluation
7. **Normalise geography** — country whitelist, region derived from country
8. **Extract organisation** — recovered from summary prose when not given
9. **Clean the amount** — strip page furniture, recover figures from text
10. **Reject spam** — adverts, furniture listings, phone numbers
11. **Validate the link** — clear links that resolve to a homepage
12. **Fingerprint and save**

### Deduplication — the important bit

```python
def make_unique_id(title, organization, deadline, url) -> str:
    key = "|".join([_norm(title), _norm(organization),
                    deadline.isoformat() if deadline else "", _norm(url)])
    return hashlib.sha256(key.encode()).hexdigest()
```

The same opportunity scraped twice — or found on two pages, or on two sites —
produces an identical hash. A UNIQUE constraint on `unique_id` makes the
database itself reject the duplicate. **Deduplication is enforced by the
database, not by application code**, so it cannot be bypassed by a bug.

### How scraping is automated

`APScheduler` runs inside the FastAPI process, started in the lifespan hook:

```python
CronTrigger(hour=req.hour, minute=req.minute)          # daily
CronTrigger(day_of_week="mon", hour=…, minute=…)       # weekly
CronTrigger(day=1, hour=…, minute=…)                   # monthly
CronTrigger.from_crontab(req.cron)                     # custom
```

The schedule is persisted to disk, so a restart doesn't lose it. There is also a
missed-run check: if the server was down at the scheduled time, it detects the
skipped run rather than silently doing nothing.

> **Critical constraint:** the backend must run **exactly one Gunicorn worker**.
> Each worker would start its own scheduler, so two workers = every scrape twice
> and every email twice.

---

## Part 6 — How email automation works

### Three kinds of email

**1. Daily digest (09:00)** — new matches per person
**2. Deadline reminders** — at 10, 7 and 2 days before a deadline
**3. Post-scrape** — optional, fires as soon as a scrape finds new matches

All three run from the same APScheduler instance.

### Matching — who gets what

`MatchingService.matches_for(member)` builds a query from that member's row:

```python
stmt = select(Opportunity).where(
    Opportunity.status == Status.ACTIVE,
    or_(Opportunity.deadline >= date.today(), Opportunity.deadline.is_(None)),
)
# keywords → LIKE across title, summary, vertical, eligibility
# categories → category IN (...)
# verticals → verticals LIKE any selected
# exclude anything already in sent_log
```

**Empty means "all".** A member with no keywords matches *everything* — which is
why auto-created members are created with `auto_send=False`. Switching one on
unattended would send them a digest of ~11,800 rows.

### The email itself

- Grouped into **regions**, South Asia first
- A contents strip of region chips at the top, each an in-email anchor jump
- Rendered against a **95 KB size budget**, because Gmail clips messages over
  102 KB. If the content is too large it retries with progressively smaller caps
  (25, 15, 10, 6, 4, 2 rows per region)
- Anchors use `<a name="...">`, **not** `id`, because Gmail strips `id` from
  every element but preserves `name`
- Every opportunity carries a one-click **Approve** button

### One-click approval without login

Each Approve button is a signed URL:

```python
token = hmac_sha256(secret, f"{opportunity_id}:{member_email}:{expiry}")
```

Clicking it approves the opportunity **from the inbox, with no login**, because
the HMAC is stronger proof of intent than a shared password. The signing key is
generated once and kept on disk, so links in already-sent emails keep working
across restarts. Tokens expire after 30 days. There is an Undo link too, and
`set_approved()` keeps the first attribution so an undo/redo doesn't rewrite who
originally approved it.

### Ad-hoc selection email

Tick rows in the table → "Email these" → pick recipients → send.
`POST /api/opportunities/send`. Recipients are **team members, not free-text
addresses**, because the approval buttons are signed per recipient and accepting
arbitrary addresses would make the endpoint an open relay. These rows are **not**
marked as sent, so the scheduled digest still behaves normally.

---

## Part 7 — Authentication

### Two-tier passwords

| | Environment variable | Unlocks |
|---|---|---|
| Dashboard | `LOP_DASHBOARD_PASSWORD` | Read, filter, approve, export |
| Admin | `LOP_ADMIN_PASSWORD` | The above **plus** scraper, team routing, email schedule, Expert Pool controls |

Both empty = no gate at all, which keeps local development frictionless.

### Sessions

```python
payload = {"email": …, "name": …, "admin": bool, "exp": …}
token   = base64(payload) + "." + hmac_sha256(secret, base64(payload))
```

Stored in an **HttpOnly** cookie so page JavaScript cannot read it. Verified with
`hmac.compare_digest`, not `==`, so a wrong signature cannot be discovered one
byte at a time from response timing.

### Domain-based access

Anyone at `@catalysts.org` can sign in with the dashboard password without being
added first, and is auto-recorded as a team member. Three deliberate rules:

1. **Deactivation beats the domain rule** — a leaver marked inactive is refused.
   Otherwise removing someone would do nothing.
2. **Exact-or-subdomain matching** — `india.catalysts.org` passes,
   `notcatalysts.org` and `catalysts.org.evil.com` are rejected. A plain
   `endswith` would have admitted both.
3. **`auto_send` off** — see the matching section above.

### Enforcement

A **middleware**, not a route dependency, because it must also protect the
static frontend. Exempt paths: `/api/login`, `/api/config` (the frontend needs
it to know whether to show a login form), and `/api/approve/*`.

---

## Part 8 — Frontend structure

```
src/
├── App.tsx                  state owner: filters, auth, refresh key
├── components/
│   ├── LoginScreen.tsx      email + password gate
│   ├── UserMenu.tsx         who is signed in, logout
│   ├── Header.tsx           search, CSV/Excel export, dark mode
│   ├── StatCards.tsx        5 clickable KPI cards
│   ├── ChartsRow.tsx        donut + bars + upcoming deadlines
│   ├── FiltersSidebar.tsx   deadline, category, vertical, source, country
│   ├── OpportunitiesTable.tsx  the main table
│   ├── SendSelectionBar.tsx    email a ticked selection
│   ├── ScraperPanel.tsx     admin: start/pause/stop, schedule
│   ├── TeamPanel.tsx        admin: routing rules
│   ├── AutoEmailPanel.tsx   admin: digest settings
│   └── ExpertsCard.tsx      Expert Pool
├── hooks/useApi.ts          all data fetching, debounced
└── lib/
    ├── api.ts               every backend call in one place
    ├── types.ts             the contract with the backend
    └── money.ts             INR conversion
```

### State management

No Redux, no Zustand. One `useState` in `App.tsx` holds `FilterState`, passed
down as props. At this size a state library would be ceremony without benefit.

Filters persist to `localStorage`, and a URL query string overrides them — that
is how a region chip in the digest email opens the dashboard already filtered.

### The table

TanStack Table with:
- **Drag-to-resize** columns (`columnResizeMode: "onChange"` + `table-fixed`)
- **Click-to-expand** rows showing full title, summary, eligibility
- Checkbox selection for the email feature
- Source and Type multi-select dropdowns in the toolbar
- Optional **INR conversion** under each amount

> A styling detail worth knowing: under `table-fixed`, a cell with
> `whitespace-nowrap` cannot shrink, so long text runs straight over the next
> column. That is what made amounts overlap the Approve button.

### Dynamic facets

The filter dropdowns narrow to what the current selection can actually reach —
59 sources with no filter, 41 under Health, 4 under Type=Tender.

Two rules make this work:

1. **Each facet excludes its own filter.** Otherwise picking one source would
   collapse the dropdown to just that source, with no way to add a second.
2. **A ticked value never disappears.** Otherwise filtering to a vertical where
   a selected source has no rows would strand the selection — an empty table
   with no visible control to undo it.

---

## Part 9 — Complete API reference

Base path `/api`. 38 endpoints.

### Auth
| Method | Path | Notes |
|---|---|---|
| GET | `/config` | Capability + identity probe. Exempt from the gate. |
| POST | `/login` | email + password → session cookie |
| POST | `/logout` | clears the cookie |

### Opportunities
| Method | Path | Notes |
|---|---|---|
| GET | `/opportunities` | paginated, filtered, sorted |
| POST | `/opportunities/{id}/approve` | approve / un-approve |
| POST | `/opportunities/send` | email a chosen set |
| GET | `/filters` | facet values, narrowed to the active filters |
| GET | `/stats` | KPI cards + charts |
| GET | `/verticals`, `/keywords` | taxonomy |
| GET | `/export/csv`, `/export/xlsx` | downloads |

### Approval from email (no login)
| Method | Path |
|---|---|
| GET | `/approve/{token}` |
| GET | `/approve/{token}/undo` |

### Scraping — **admin**
| Method | Path |
|---|---|
| POST | `/scrape`, `/scrape/pause`, `/scrape/resume`, `/stop` |
| GET | `/progress` (live %, ETA), `/sources` |
| GET/PUT | `/schedule` |

### Team & email
| Method | Path | Access |
|---|---|---|
| GET | `/team` | any |
| POST/PUT/DELETE | `/team`, `/team/{id}` | **admin** |
| GET | `/team/{id}/matches` | any |
| POST | `/team/{id}/send` | any |
| GET | `/email/settings`, `/email/status` | any |
| PUT | `/email/settings` | **admin** |
| POST | `/email/run-now` | any |

### Expert Pool
| Method | Path | Access |
|---|---|---|
| GET | `/experts` | **any — this is the list users may see** |
| POST | `/experts/refresh` | **admin** |
| GET/POST | `/devaid/status`, `/devaid/connect`, `/devaid/session/*` | **admin** |

### Health
`GET /health`

---

## Part 10 — Deployment

```
Internet → Nginx :80 ─┬─ /        → /var/www/lead-opportunity-platform (built React)
                      └─ /api/*   → Gunicorn 127.0.0.1:8001 (1 worker)
                                        └── Supervisor keeps it alive
```

One command deploys everything: `bash deploy/update.sh`. It fetches, resets,
builds, publishes, restarts and **verifies** — stopping on any failure rather
than continuing against stale code.

### Three deploy bugs worth presenting as lessons

1. **`tsconfig.tsbuildinfo` was tracked in git.** Every server build rewrote it,
   so `git pull` aborted with "local changes would be overwritten" — while the
   build and restart in the same script carried on. Three deploys in a row
   *looked* successful and changed nothing.
2. **`rm` without its `cp`.** The web root was emptied and never refilled;
   every visitor got a bare `403 Forbidden`.
3. **Wrong service name.** `supervisorctl restart lead-api` returned "no such
   process" — the program is `lead-scanning-api`. Nothing ever restarted.

The lesson generalises: **a deploy step that cannot fail loudly will eventually
fail silently.** The script now verifies `/api/config` returns the new shape
before it claims success.

---

## Part 11 — How to build this yourself, in order

This is the sequence that works. Each step produces something you can see
running before you move on.

### Step 1 — Database model first
Nothing else can be designed until you know what an "opportunity" is.

```
backend/app/database/models.py   # SQLAlchemy classes
backend/app/database/db.py       # engine, session, init_db()
```

Run `init_db()`, confirm the `.db` file appears with the right columns.

### Step 2 — One scraper, by hand
Pick the *simplest* site. Write a plain script that fetches one page, parses it
with BeautifulSoup, and prints the results. No framework, no database.

**Why first:** if you cannot extract data reliably, nothing downstream matters.
This is where the real difficulty lives.

### Step 3 — Save to the database
Connect the scraper to the model. Introduce `unique_id` immediately — you will
create duplicates on your second run and you want the database to reject them.

### Step 4 — The scraper base class
Once you have two or three scrapers you will see the repetition: fetch,
paginate, retry, rate-limit. Extract `BaseScraper`.

**Do not do this before the third scraper.** Abstracting from one example gives
you the wrong abstraction.

### Step 5 — FastAPI, one endpoint
`GET /api/opportunities` returning JSON. Visit `/docs` — FastAPI generates
interactive documentation for free. Confirm data flows DB → API → browser.

### Step 6 — React, one table
`npm create vite@latest`. Fetch that one endpoint, render rows. Ugly is fine.
This is the moment the two halves meet, and it is worth reaching quickly.

### Step 7 — Filters, both sides
Query parameters on the backend, controls on the frontend. Define
`FilterState` once in TypeScript and pass it everywhere.

### Step 8 — The processing pipeline
Now that you can *see* the data, its problems become obvious: junk titles,
wrong countries, unparseable dates. Add one cleaning step at a time, each in its
own module, each idempotent so it can be re-run over existing rows.

### Step 9 — Classification
Keyword-based, not machine learning. It is explainable, debuggable, and takes an
afternoon rather than a month. Multi-label from the start — real opportunities
genuinely belong to two verticals.

### Step 10 — Scheduling
APScheduler in the FastAPI lifespan. Persist the schedule to disk.

### Step 11 — Email
Matching rules → HTML rendering → SMTP. Send to yourself many times. Test in
**Gmail specifically**; it strips `id` attributes and clips at 102 KB, and you
will not discover either in a local preview.

### Step 12 — Auth
HMAC-signed cookies. Add it before anything is public, not after.

### Step 13 — Deploy
EC2, Nginx, Supervisor, Gunicorn. Write the deploy script on day one and make it
verify itself.

### The order that does *not* work

Do not build the pretty dashboard first. You will design it around imagined data,
and real scraped data is always messier than you imagine. **Data first, then the
API, then the UI.**

---

## Part 12 — Talking points for the presentation

**The single most important design decision:** deduplication by content hash
enforced with a database UNIQUE constraint. It means the scraper can be re-run
any number of times, safely, and duplicates are impossible rather than unlikely.

**The most instructive bug:** the World Bank pagination. It didn't crash, didn't
log an error, and reported success — while collecting 38 rows instead of
thousands. Silent partial failure is the dangerous kind, and it argues for
checking *outputs* rather than exit codes.

**The most valuable safety rail:** one Gunicorn worker. Two schedulers would
have doubled every email to the whole team, and nobody would have known why.

**What's honestly incomplete:**
- The four newest sources yield no deadline or country — each needs a per-source
  CSS selector
- DevelopmentAid cannot be scraped from EC2; Cloudflare blocks datacentre IPs.
  The legitimate paths are API access from the vendor, or scraping from a desktop
  and merging
- INR conversion uses fixed rates, so it is indicative only
