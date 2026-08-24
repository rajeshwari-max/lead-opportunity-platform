# DevelopmentAid — How It Works, and Why It Stopped

---

# PART 1 — Why it is returning nothing

## The current failure, from today's log

```
13:06:46  grants:  HTTP 403 on arrival — waiting for a possible interstitial to clear
13:06:59  grants page 1 HTTP status: 403
13:07:50  grants:  no cards after first load (28329 bytes) — reloading once
13:09:00  grants:  BLOCKED BY CLOUDFLARE (page title 'Just a moment...')
13:09:08  tenders: HTTP 403 on arrival
13:11:14  tenders: BLOCKED BY CLOUDFLARE (page title 'Just a moment...')
13:11:22  completed — 0 found = 0 new saved + 0 already in database
```

**HTTP 403 on arrival.** Before any content loads, before any parsing, before
the session is even relevant. This is a network-layer refusal, so nothing in the
parser, the selectors or the URL construction can affect it.

## What has been ruled out — each by evidence, not reasoning

| Suspected cause | Verdict | Evidence |
|---|---|---|
| Wrong URL / query string | **Ruled out** | Reverted to the plain `/grants/search` and `/tenders/search`. Log line 6636 shows the bare URL. Still 403. |
| `LOP_DEVAID_SECTIONS=tenders` skipping grants | **Fixed** | `.env` now `grants,tenders`; both sections are attempted in the 13:06 run. |
| Bundled Chromium instead of real Chrome | **Ruled out** | The "real Chrome not available" fallback warning last appeared on **10 Aug**. Real Chrome is in use. |
| Expired or missing session | **Ruled out** | `data/devaid_profile/Default/Network/Cookies` exists and is populated. |
| Stale session file taking priority | **Ruled out** | No "using uploaded session" log line — it is on the persistent-profile path. |
| Selectors broken by a redesign | **Ruled out** | `da-search-card` still correct; the 30 Jul capture found **2,463** of them. |
| Parsing logic | **Ruled out** | Parsing is never reached. The 403 happens on `page.goto()`. |

Every code-side factor checks out. That is the uncomfortable finding.

## The one thing left, and it is a real one

```python
browser = open_persistent(pw, headless=True)   # <- was hardcoded
```

And inside `open_persistent`:

```python
return pw.chromium.launch_persistent_context(
    str(PROFILE_DIR), channel="chrome", **common
)
# note: user_agent is deliberately NOT set
```

Not overriding the user agent is the right call — a UA that disagrees with the
actual browser is a worse signal than an honest one. **But in headless mode,
real Chrome's own user agent contains the string `HeadlessChrome`.**

So on every single request the scraper has been truthfully announcing that it is
a headless browser. That is among the most reliable bot signals in existence, and
it is a very plausible reason Cloudflare challenges it now when it did not in
July — the site did not need to change its rules, only its sensitivity.

**The change:** `headless` is now configurable, and the verification uses the
same setting as the scrape (verifying headless while scraping headed would test a
different client than the one doing the work).

```
LOP_DEVAID_HEADLESS=false     # desktop: a visible browser window
LOP_DEVAID_HEADLESS=true      # server: no screen, no choice
```

Running a visible window is **not** a disguise. It is an ordinary browser doing
ordinary browsing with your own logged-in session. That is a meaningfully
different thing from spoofing a fingerprint, and it is the last honest option
available.

## What I am not going to do

Patching `navigator.webdriver`, injecting stealth scripts, solving the Turnstile
challenge, or routing through residential proxies. All of those are defeating a
security control the site chose to apply, and each breaks the next time
Cloudflare updates. If a visible browser with a valid paid session is still
refused, the site is declining automated access and the answer is commercial,
not technical.

## Try in this order

1. **Set `LOP_DEVAID_HEADLESS=false`** in `backend/.env`, restart, scrape
   DevelopmentAid alone. A Chrome window will open — leave it alone and watch
   the log. `HTTP status: 200` instead of `403` is the signal.
2. If that works on the PC but you need EC2: scrape DevelopmentAid on the
   desktop and merge with `scripts/merge_db.py`. EC2 has no screen *and* a
   datacentre IP, so it is the worst case on both counts.
3. If it still 403s headed: **email DevelopmentAid.** You are a paying member
   being refused programmatic access. Ask for API or bulk data access, and give
   them the Cloudflare **Ray ID** from `logs/devaid_grants_debug.html` — their
   support can look up the exact block and allow-list you. That single detail
   turns a vague complaint into something actionable on their side.

## An honest note on my own diagnosis

I have been wrong twice on this. I said the 25-sector query string was the cause
— the plain-URL log disproved it. I said ADB was Cloudflare when it was actually
a `TypeError` in my own code. In both cases I reached a conclusion from a
plausible mechanism instead of a test.

What is different about the headless finding is that it is **checkable in one
run**: flip one variable and read the HTTP status. If it comes back 403 again,
that hypothesis is dead too, and the next step is comparing the actual request
headers between your working Chrome and the scraper rather than reasoning
further.

---

# PART 2 — How the DevelopmentAid scraper works

This is the most complex of the 85 sources, and the only one that overrides
`crawl()` entirely. Roughly 1,600 lines in
`backend/app/scrapers/developmentaid.py`.

## Why it can't use the generic path

| Requirement | Why the shared crawler can't do it |
|---|---|
| Paid login | Deadlines are hidden and pagination is capped for guests |
| Angular SPA | Served HTML is an empty shell; content arrives after hydration |
| Two catalogues | Grants and tenders are separate searches, one run |
| ~100-record search depth cap | Coverage needs many *narrowed* searches, merged |
| Browser-driven paging | State lives in the app, not purely in the URL |

## The authentication model

**No password is ever submitted by the code.** A human signs in once; the
browser profile is reused forever after.

```
1. Admin clicks "Connect account"          POST /api/devaid/connect
2. Playwright opens a VISIBLE Chrome window
3. The person types email, password, solves the reCAPTCHA
4. They close the window
5. Cookies + localStorage are now in  backend/data/devaid_profile/
6. Every later scrape opens that same profile and is already signed in
```

`launch_persistent_context(PROFILE_DIR, channel="chrome")` is the key call. An
ordinary Playwright browser starts with an empty profile every time; a
*persistent* context points at a real Chrome user-data directory on disk, so it
starts signed in exactly as reopening Chrome on your laptop would.

Three details that exist because of specific failures:

- **`channel="chrome"`** — the site 403s Playwright's *bundled* Chromium, whose
  build fingerprint is a known automation marker. Falls back to bundled only if
  real Chrome is absent.
- **Stale lock recovery** — a killed run leaves Chromium's singleton lock files
  behind, and every later launch then refuses with "already in use" even though
  nothing is running. Detected and cleared automatically, because otherwise a
  crash creates a dead end only a human can fix.
- **Verification, not assumption** — after connecting it loads a real search
  page and looks for *positive* member signals (logout link, account menu,
  pagination controls), not merely the absence of "Sign in". The site keeps a
  "Sign in" link in a collapsed menu even for members, and trusting it wrongly
  declared a live Premium session dead.

### Moving the session to a server

A server has no screen, so nobody can log in on it.

```
GET  /api/devaid/session/export   -> devaid_session.json  (cookies + localStorage)
POST /api/devaid/session/import   -> installs it, and VERIFIES before reporting success
```

Priority rule: an uploaded session wins **only** when the machine has no
interactive login of its own. On a desktop the live profile stays authoritative;
on a server the uploaded session is all there is.

## The crawl: a browser thread feeding an async generator

Playwright's sync API cannot run inside the asyncio event loop, and on Windows
uvicorn's selector loop cannot spawn subprocesses at all. So the browser runs in
its own thread and hands pages back over a queue:

```
async crawl()                    │  _walk_sections()  (worker thread)
  ├─ start worker thread         │    sync_playwright()
  ├─ await queue.get()  ────────►│    open_persistent(...)
  ├─ parse_listing(html)         │    for section in (grants, tenders):
  ├─ yield items                 │        goto, wait for da-search-card
  └─ repeat until None           │        push page HTML to queue
```

The async side stays responsive — pause, stop and progress reporting all keep
working while a page renders.

## Per-page sequence

1. `goto(section_url, timeout=90s, wait_until="domcontentloaded")`
2. If the response is 403/429/503, **wait 12s** for an interstitial to resolve
   on its own
3. Check the page title against `_CHALLENGE_MARKERS` — a bot check must be
   identified *before* any login judgement, because its page has neither cards
   nor a sign-in link and every later test misreads it as a failed login
4. Dismiss the cookie banner (`"I Accept"`, harmless if absent)
5. `wait_for_selector("da-search-card", timeout=45s)` — Angular bootstrap has
   been taking 20–30s
6. If no cards: **reload once and wait 60s.** A failing grants capture was 11 KB
   with the *correct* title and zero cards — the shell arrived and the app never
   bootstrapped, which is transient, not a block
7. Parse the cards
8. `_page_url(base, n)` → `?pageNr=N`, built with `urlparse`/`urlencode` so it
   works whether or not the base URL already has a query string

## Parsing a card

```python
url = a["href"].split("?")[0]          # strip tracking params
if url.startswith("/"):
    url = self.website + url
title = (a.get("title") or a.get_text(" ", strip=True)).strip()
```

Fields are read from the card's own labels, which differ between the two
catalogues:

```python
organization = (fields.get("funding agency")          # grants
                or fields.get("contracting authority") # tenders
                or fields.get("client")
                or "")
```

- `entitytype="grant"|"tender"` becomes a **category hint** worth 2 points — a
  hint only, so an `"RFP - ..."` tender is still promoted to RFP by the
  classifier reading the title
- `Budget` → `clean_amount()`, which drops the `"N/A"` values on roughly
  three-quarters of listings
- `9999-12-31` → treated as **no deadline**, which is what "no closing date"
  means
- `assume_active` is set when status is open *and* the deadline is still locked
  behind the paywall, so a members-only date isn't misread as expired

## Coverage strategy: slicing, not paging

DevelopmentAid caps how deep any single search can be paged at roughly **100
records**. Paging one unfiltered search therefore cannot reach the archive.

So coverage comes from running **many narrowed searches** and merging the
results — filter by sector, by donor, by location, then walk each slice.
`devaid_max_slices = 25000` bounds how many one run performs, and
`devaid_open_first = True` covers currently-open listings before the historical
archive, because the archive is several times larger and would otherwise consume
the entire search budget on calls nobody can bid for.

`stale_page_streak_override = 0` — never stop early. The site sorts by Modified
Date, so a re-run re-reads the same early pages; a "3 pages with nothing new →
stop" rule ended one crawl at page 55 of 811, saving 27 rows out of 2,188 seen.
This is the archive source: it walks every page and lets the `unique_id`
constraint do the filtering.

## Guest-mode detection — the failure that looked like success

When signed out, this site answers `?pageNr=N` by **re-serving page 1**. Earlier
runs therefore appeared to walk 474 pages while only ever seeing the first 50
listings, then deduplication discarded them all. A run that walks hundreds of
pages, reports success, and saves nothing.

Two signals are combined, because neither is reliable alone:

```python
if checked_this_section and signed_out_detected and pages_reached <= 2:
    log.error("[developmentaid] SESSION EXPIRED — scraping as a logged-out guest…")
    email_service.send_alert(...)     # this failure is otherwise invisible
```

A "Sign in" sighting **plus** a walk that stopped after ≤2 pages. It also emails
an alert, because nothing on screen would otherwise reveal it.

## Numbers on record

| | |
|---|---|
| Rows collected | **42,984** |
| Active | 6,257 |
| Cards on one page-1 capture (30 Jul) | **2,463** |
| Largest single run | 3,217 pages / 25,296 saved |
| Last successful run | **30 July 2026** |

## The files involved

| File | Role |
|---|---|
| `scrapers/developmentaid.py` | The scraper — sections, slicing, card parsing (~1,600 lines) |
| `scrapers/devaid_auth.py` | Profile, connect, verify, session export/import |
| `scrapers/site_auth.py` | Unified Site Logins panel; delegates DevelopmentAid to `devaid_auth` |
| `services/experts_service.py` | Expert Pool counts, same session |
| `core/config.py` | `devaid_*` settings |
