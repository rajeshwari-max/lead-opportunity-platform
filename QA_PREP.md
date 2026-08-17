# Presentation Q&A — Anticipated Questions with Answers

---

# Part 1 — The crawling question (answer this one carefully)

> *"If you use crawling, you don't need to write code for every website."*

**Your instinct is correct, and your senior is also partly right. The honest
answer is that both are true of different halves of the problem.**

## The distinction that resolves it

| | What it means | Can it be generic? |
|---|---|---|
| **Crawling** | *Finding* pages — following links, walking pagination | **Yes — completely** |
| **Scraping / parsing** | *Understanding* a page — which text is the title, which is the deadline | **Partly** |

Crawling is generic because "follow the next link" is the same instruction
everywhere. Parsing is not, because there is no standard that says a funding
deadline must live in `<span class="deadline">`. One site uses a table, another
uses cards, a third loads everything from a JSON API after page load.

**So: one crawler, many parsers.** That is exactly how this project is built.
`BaseScraper.crawl()` is written once and inherited by all 86 sources. Only
`parse_listing()` differs.

## Where your senior is right — and this project already does it

You do **not** need bespoke code per site. 75 of the 86 sources have **no Python
file at all**. They are JSON entries:

```json
{
  "name": "world_bank",
  "display_name": "World Bank",
  "url": "https://projects.worldbank.org/...",
  "page_url": "https://projects.worldbank.org/...&os={offset}",
  "page_size": 10
}
```

`GenericListingScraper` handles them with heuristics rather than site-specific
selectors:

- find anchors that *look like* opportunities (length between limits, not
  navigation words, href not `/about` or `/privacy`)
- if the anchor text is useless ("Read more"), fall back to the nearest heading
- find a date near the text with a deadline-shaped regex
- reject site chrome by phrase list ("Skip to main content", "Procurement Policy")

**So the split is: 75 sites configured, 11 sites coded.** 87% needed no
code — which is your senior's point, proven. The 12 that needed code needed it
for concrete reasons.

## Why 11 sites still needed real code

| Site | Why generic parsing failed |
|---|---|
| **DevelopmentAid** | Login required; content behind a session; Cloudflare bot protection |
| **FundsForNGOs** | Detail pages must be visited for amount and eligibility |
| **DevNetJobs** | Pagination is an HTTP **POST** with form state, not a URL |
| **NGOBOX** | Serves stale cached HTML unless the request looks like a real browser |
| **Bond UK** | Opportunities live inside a JS-rendered widget |
| **GrantWatch** | Different DOM per category |

## The strongest way to say it in the room

> "Crawling is generic and we treat it that way — one `crawl()` method serves all
> 86 sources. Parsing is generic *until it isn't*: 75 sites work from a JSON
> config with no code, and the 11 that needed code needed it because of login
> walls, POST-based pagination or JavaScript rendering, not because of styling
> differences. The rule we use is: try the generic path first, and only write a
> parser when the site does something structurally different."

That answer shows you understand the trade-off rather than defending a position.

## What a fully generic scraper would cost

It is possible — an LLM can read any page and return structured fields. The
trade-offs:

| | Config + heuristics (current) | LLM extraction |
|---|---|---|
| Cost | ~0 | Per-page API cost × 70,000+ pages |
| Speed | ~1s/page | 2–10s/page |
| Determinism | Same input → same output | Can vary between runs |
| Debuggable | You can point at the selector | "The model read it that way" |
| New site setup | Minutes (a JSON entry) | Zero |

A sensible hybrid: heuristics first, LLM only for pages that parse to nothing.

---

# Part 2 — How DevelopmentAid connects

This is the most technically interesting part of the project and a likely
question, because it is the only source requiring authentication.

## Why it is special

DevelopmentAid is a **paid membership site**. Without a signed-in session:
- deadlines are hidden or replaced with "members only"
- pagination stops after roughly one page
- the Expert Pool counts are not visible at all

So the scraper must be logged in, and it must stay logged in across restarts.

## The mechanism: a persistent browser profile

The app does **not** store the DevelopmentAid username and password and type
them in. It reuses a browser profile that a human logged into once.

```python
PROFILE_DIR = backend/data/devaid_profile      # a real Chrome user-data dir

def open_persistent(pw, headless: bool = True):
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
    )
```

`launch_persistent_context` is the key call. A normal Playwright browser starts
with an empty profile every time. A *persistent* context points at a real
directory on disk holding cookies, localStorage and session tokens — so the
browser starts already signed in, exactly like reopening Chrome on your laptop.

### The flow

```
1. Admin clicks "Connect account" in the dashboard
2. POST /api/devaid/connect
3. Playwright opens a VISIBLE Chrome window at DevelopmentAid
4. The human types the email, password and solves the CAPTCHA
5. The human closes the window
6. Cookies are now written into data/devaid_profile/
7. Every later scrape opens that same profile headlessly and is already signed in
```

**The password is never seen by the application.** It is typed by a person, into
a real browser, into DevelopmentAid's own form. That is a deliberate design
choice, not an oversight — storing credentials would put a paid account's
password in the codebase or database.

### Three details worth mentioning

**Real Chrome, not bundled Chromium.**
```python
channel="chrome"   # falls back to bundled Chromium if unavailable
```
DevelopmentAid returns **403 to Playwright's bundled Chromium** — its build
fingerprint is a known automation signal. The comment in the code also notes
that the user agent is deliberately *not* overridden when using real Chrome, on
the grounds that a UA string mismatched to the actual browser is itself a bot
signal.

**Stale lock recovery.** A crashed run leaves Chromium's singleton lock files in
the profile directory, and every later launch then refuses with "another
instance is already in use" even though nothing is running. The code detects
that specific error, clears the stale locks and retries — otherwise a killed
scrape would leave a dead end that only a human could clear.

**Verification, not assumption.** After connecting, the app loads a real search
page and checks:
```python
def is_signed_in(page) -> bool:
    # true when no VISIBLE "Sign in" control exists on the page
```
It checks visibility, not mere presence, because the element often exists in the
DOM while hidden. Without this the app would report "connected" for an expired
session and every scrape would quietly return one page.

## Session transfer — moving the login to the server

The server has no screen, so nobody can log in on it. Hence:

```
GET  /api/devaid/session/export   -> downloads devaid_session.json
POST /api/devaid/session/import   -> installs it on the server
```

`storage_state` (cookies + localStorage as JSON) is exported from the machine
that has a real login and imported on the server. The import **verifies** the
session before reporting success, because an expired session is still valid JSON.

Priority rule, from the code comment: an uploaded session wins **only when the
machine has no interactive login of its own**. On a desktop the local profile
stays authoritative; on a server the uploaded session is all there is.

## The unresolved problem — be honest about this

**DevelopmentAid cannot be scraped from EC2.** Cloudflare blocks the request
based on the datacentre IP address before authentication is even considered. A
valid session does not help, because the block happens at the network layer.

If asked what you would do:
1. **Request API access from DevelopmentAid** — the correct, supported answer
2. **Keep DevelopmentAid scraping on a desktop** and merge periodically with
   `scripts/merge_db.py` (this is what happens today)
3. A residential proxy would technically evade it, but that means deliberately
   circumventing a site's bot protection — not something to propose.

Saying "we hit a limit and chose the legitimate workaround" is a stronger answer
than pretending it works.

---

# Part 3 — Q&A bank

## Architecture

**Q: Why FastAPI and not Django or Flask?**
Scraping is I/O-bound — mostly waiting on network responses. FastAPI is async
natively, so one process can wait on many requests at once. It also generates
OpenAPI docs from type hints, so `/docs` is a live, executable API reference
that cannot drift from the code. Django would bring an ORM, admin and templating
we don't need; Flask would need async retrofitted.

**Q: Why SQLite and not PostgreSQL?**
70,000 rows and one writer. SQLite handles that comfortably in WAL mode, and it
is a single file — no server, no connection pool, no separate backup story.
Migrating is one connection string in `config.py` because SQLAlchemy abstracts
the dialect. I would switch when there are concurrent writers or the dataset
outgrows one machine.

**Q: Why React and not server-rendered HTML?**
The dashboard is a single interactive view — filters, sorting, resizing, charts
— where every interaction would otherwise be a page reload. A SPA also makes the
frontend deployable as static files, which is why Nginx can serve it directly.

**Q: How do frontend and backend communicate?**
JSON over HTTP, nothing else. In development Vite proxies `/api` to port 8000;
in production Nginx serves the built files and proxies `/api` to Gunicorn. Same
origin in production, so cookies work without CORS.

## Data quality

**Q: How do you prevent duplicates?**
A SHA-256 fingerprint of title + organisation + deadline + URL, stored in a
column with a UNIQUE constraint. The same opportunity scraped twice hashes
identically and the database rejects it. Enforced by the database, not
application code, so no bug can bypass it.

**Q: How do you know the scraped data is correct?**
Several ways. `scrape_runs` records found/saved/errors per source. Page 1 of
every source is saved to `data/debug/` so a parser can be checked against the
real HTML. A startup audit re-checks deadlines and Active/Expired status. And I
audited it directly — that is how I found 1,674 rows marked Active with a
deadline already past, and 148 with a `9999-12-31` sentinel being read as a real
date.

**Q: What is the biggest data-quality problem left?**
47% of active rows carry no vertical, so they are hidden from the dashboard. Of
those, 56% have no text beyond a title like "Souter Charitable Trust — Funding
Opportunity". That is a scraping gap, not a classification gap — the fix is
visiting detail pages, not a better model.

## Classification

**Q: How does classification work?**
436 weighted regexes across 6 verticals. A title hit scores 3, a body hit 1,
threshold 2 — so one title match qualifies alone, one body match does not.
Multi-label, because a solar-irrigation-for-farmers grant is genuinely both
Climate and Livelihood.

**Q: Why not machine learning?**
I measured it. ML would help on about 2,264 rows where the text is rich but the
vocabulary is paraphrased — "seriously or terminally ill" instead of "health".
That lifts coverage from 53% to roughly 72%. But 3,075 rows have no text at all,
and no model can classify what isn't there. So the scraping fix is worth more
than the model, and it comes first. The code already defines a `Classifier`
Protocol so an ML implementation drops in without other changes.

**Q: Isn't keyword matching fragile?**
It is explainable, which matters more here than raw accuracy. When a row is
labelled wrongly I can point at the exact matched word and fix it in one line.
With a model I would have to retrain and revalidate. For a tool whose output
routes work to human teams, being able to explain a decision is worth a few
points of recall.

## Email

**Q: What is SMTP?**
Simple Mail Transfer Protocol — the standard for *sending* mail. IMAP and POP3
are for reading; this app only sends, so it needs SMTP alone. It connects to
`smtp.gmail.com` on port 587, issues STARTTLS to upgrade the connection to
encrypted, authenticates with an App Password, and transmits a MIME message.

**Q: Why an App Password rather than the real one?**
Google blocks plain password login for applications. An App Password is scoped
to one app, revocable independently, and does not defeat 2FA on the account.

**Q: How do you stop someone being emailed the same thing twice?**
`sent_log` — a row per (member, opportunity). The matching query excludes
anything already in it. Reminders use `reminder_log` with a UNIQUE constraint on
(member, opportunity, days_before), so a 10-day reminder physically cannot fire
twice. And rows are marked sent **only after** SMTP returns successfully — if
you mark first and the send fails, that opportunity is lost forever.

**Q: How does approving from an email work without logging in?**
Each Approve button is an HMAC-SHA256 signed URL containing the opportunity id,
the recipient's email and an expiry. The server recomputes the signature with
`hmac.compare_digest`. A valid signature is stronger proof of intent than a
shared password, so those links are exempt from the auth gate. They expire after
30 days.

## Scheduling

**Q: How is scraping automated?**
APScheduler inside the FastAPI process, started in the lifespan hook, with cron
triggers for daily/weekly/monthly/custom. The schedule persists to disk so a
restart doesn't lose it, and there is a missed-run check for when the server was
down at the scheduled time.

**Q: Why only one Gunicorn worker?**
Each worker would start its own scheduler. Two workers means every scrape runs
twice and every team member receives every email twice. It is the single most
important operational constraint in the project.

**Q: Why not Celery or a separate cron?**
Both are correct at larger scale. For one server and two scheduled jobs,
APScheduler adds no infrastructure — no broker, no worker process. I would move
to Celery when jobs need to survive an app restart mid-run, or run on separate
machines.

## Security

**Q: How is authentication done?**
HMAC-signed session tokens in HttpOnly cookies, so page JavaScript cannot read
them. Two tiers: a dashboard password for reading and approving, an admin
password for the scraper, routing and email settings. Signature comparison uses
`compare_digest` rather than `==` so a wrong signature can't be discovered a
byte at a time from response timing.

**Q: Anyone with a company address can log in — isn't that a risk?**
They still need the shared dashboard password, so it is "anyone at the company
who also knows the password", not "anyone who types a company address". Three
guards: deactivation beats the domain rule so leavers are refused; domain
matching is exact-or-subdomain so `notcatalysts.org` fails; and auto-created
members have auto_send off, because a member with no keywords matches
everything and would otherwise receive an 11,800-row digest.

**Q: Is scraping these sites legal?**
We only read publicly listed funding opportunities, rate-limited to one request
per second, identifying with a normal user agent — the same pages a person could
open. The one paid site is accessed with a legitimate paid membership. Where a
site actively blocks us, as Cloudflare does on EC2, we stopped rather than
circumvented, and the recommendation is to request API access.

## Deployment

**Q: How do you deploy?**
Push to GitHub, run one script on EC2. It fetches, resets, builds the frontend,
publishes to the Nginx root, restarts the backend and verifies `/api/config`
returns the new shape before claiming success.

**Q: What was the hardest bug?**
A deploy that succeeded three times and changed nothing.
`tsconfig.tsbuildinfo` was tracked in git; every server build rewrote it, so
`git pull` aborted with "local changes would be overwritten" — but the build and
restart in the same pasted block carried on regardless. The output looked like a
successful deploy. The fix was untracking the file, but the lesson is the
general one: **a step that cannot fail loudly will eventually fail silently.**
The deploy script now verifies its own result.

## Hard questions to be ready for

**Q: What would you do differently?**
Visit detail pages from the start. Half the classification problem is missing
text, and that was a collection decision made early for speed. I would also have
written the deploy script on day one instead of running commands by hand.

**Q: How would this scale to 1,000 sources?**
The crawler already runs 6 sources concurrently and streams results per page, so
memory is flat. The limits I'd hit first are SQLite write contention — move to
PostgreSQL — and the scheduler being in-process, which would need Celery so
scraping can run on separate machines from the API.

**Q: What is the weakest part?**
Classification coverage, and I can quantify it: 53%. The path to fixing it is
detail-page scraping first, then an LLM pass over what remains unclassified.

**Q: How do you know the emails are actually useful?**
Honestly — I don't have that data yet. Only 2 opportunities have been approved
so far. The approval mechanism exists precisely to measure it: approvals per
digest is the metric that would tell us whether routing rules are well tuned,
and it is the first thing I would instrument next.
