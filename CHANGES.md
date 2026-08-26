# CHANGES

A running log of changes made to this project, newest first. Each entry says
what was changed, **why**, and how to verify it.

---

## 2026-08-26 — the login was never required: 50% of the archive, signed out

A 17-minute signed-out run harvested **1,214 distinct grants of 2,422 (50%)**
and was still climbing when the `--pages 150` cap stopped it — mid-way through
sector 11 of 53.

```
items                           : 1534
links to a specific opportunity : 1534/1534 (100%)
carry a deadline string         : 1529/1534 (100%)
  ... that actually parses      : 1529/1534 (100%)
navigation furniture stored     : 0
```

The guest limit is **per search, not per account**. Signed out you cannot page
*within* a search, but you can run a different one — and the adaptive
partitioner turns 53 sectors x 190 donors x 2 purposes into as many searches as
it needs. No account, nothing to expire, nothing to re-push.

Coverage is non-linear in a good way: grants carry multiple sectors, so the
first sectors covered pull in rows that later sectors would have repeated. 11
sectors gave 50%; the remaining 42 add progressively fewer new rows.

### The four leaks had one cause, and it is fixed

```
WARNING sector=Culture & Arts+donor=Government+purpose=Project Ideas
        holds 291 but no split axis remains — captured 95 of them
```

Budget-range bisection exists precisely for this — an unlimited-depth numeric
axis for when the categorical ones run out. It never fired. `cover()` recurses
as `cover(child, dim_idx + 1, label)` on a categorical split and **drops `lo`
and `hi`**, whose defaults are `0, 0`. The bisection below is guarded by
`hi > lo + 1`, and `0 > 1` is never true. So the budget axis was discovered,
validated against the server, and then made unreachable everywhere except the
root of the recursion.

Reproduced with the real recursion shape — the simulation produces exactly the
four leaks the live run reported:

```
before (lo/hi dropped)     -> leaks: 4, rows stranded: 2420
after  (lo/hi carried)     -> leaks: 0, rows stranded: 0
```

### Reading truncated leaves from both ends

A leaf that still can't be split returns the first `reachable` rows of *some
ordering*. Flipping `sort` from `.desc` to `.asc` returns the last ones — a
different set, for one extra request, with no filter to discover and no
assumption about what the server permits. Roughly doubles an unsplittable
leaf's yield.

The shortfall warning now fires only when a leaf holds more than `2 x
reachable`, and reports what both directions actually returned instead of
implying the rest was never there.

### Still to push from the laptop

The `9999-12-31` sentinel fix and the shared membership detector are committed
locally but were not on the server for this run — `deadline: 9999-12-31` is
still visible in row 8 of the output above, and the over-eager `EVERY
pagination probe was REFUSED` still fires on an HTTP 400.

---

## 2026-08-26 (later still) — one membership detector, not three

`devaid_session.py` contradicted itself on the same machine, same profile,
seconds apart:

```
status : signed in  : True
push   : The saved session is no longer signed in
```

Both were reading the same page. They disagreed because they asked different
questions, and the one that said "True" was asking the wrong one.

`export_session_state()` uses `is_signed_in()`: *is there a visible Sign in
control?* It found one — the page was showing a Sign in button.

`verify_session()` looked for positive member evidence and accepted
`a[href*="/membership"]` as proof. That selector matches DevelopmentAid's
**Expert-plan upsell tile** — the advert shown to people who are *not* members.
It also counted `pagination > 0`, and guests are shown pagination controls too;
they just re-serve page 1. So an advert plus some dead page links outvoted a
Sign in button, and the machine reported a session it did not have.

**This is the same bug, in a third place.** It has now been fixed in
`developmentaid.py` (the scraper's session check) and in `verify_session()`,
which is two fixes too many for one rule. So there is now exactly one:

`devaid_auth.membership_state(page) -> ("in" | "out" | "unknown", evidence)`

- proof of membership must be a control that only exists once authenticated —
  a log-out link, or a link into the signed-in account area
- promo / card / banner / cta / pricing classes are advertising and prove
  nothing in either direction
- **pagination proves nothing** — guests see it
- a visible Sign in control, or the site's own members-only notice, is evidence
  *against*; the site stating its answer beats us inferring one
- `unknown` no longer returns `True`. The old code, on a page it could not
  inspect, logged "assuming the session is usable" and returned success — which
  is how the dashboard came to advertise a live account while every scrape
  returned one page

`developmentaid._membership_state()` is now a three-line delegate to it, so the
scraper and the session tooling cannot disagree again.

Verified against reconstructions of the real pages:

```
OUT     | THE LAPTOP RIGHT NOW: Sign in button + Expert upsell card
OUT     | old verify_session would have said IN here (pagination present, guest)
IN      | genuinely signed in
UNKNOWN | Cloudflare interstitial
```

**What this means in practice:** the laptop is signed out too. `signed in:
True` was the bug, and `push` was right to refuse. The account needs a fresh
interactive login before any session is worth moving.

---

## 2026-08-26 (later) — DevelopmentAid: fix confirmed; session is the real blocker

The in-page fetch landed and the API is answering. Measured, not inferred:

| | before | after |
|---|---|---|
| page size | `300 not honoured (returned 0)` | `100 accepted (100 items in one request, was 50)` |
| filter option lists | `could not fetch any` | `53 sectors, 190 donors, 2 purposes` |
| total known | `? listings to cover` | `2,422 listings` |
| **grant deadlines** | **0/150 (0%)** | **39/39 (100%)** |

The grants deadline problem solved itself exactly as predicted: the listing
cards don't carry a closing date, the JSON does. No parser change was needed.

**And the session question finally has an honest answer:**

```
session check -> SIGNED OUT (a visible Sign in control and no signed-in controls)
NOT LOGGED IN — the saved session has expired
```

It was never a subscription tier. The saved session on the server is expired,
and the old detector was reading the Expert-plan advert as proof of membership.

`pageNr (1-based) overlaps (100 unique of 200)` — pages 1 and 2 returned the
*same* 100 rows. That is the documented signed-out behaviour: this site ignores
`pageNr` for guests. So pagination is untested until the session is restored.

### Two corrections in this commit

**1. My own new diagnostic was crying wolf.** It printed

> EVERY pagination probe was REFUSED (last HTTP 400) — the API was never reached

in a run where the API had just returned 53 sectors, 190 donors and a 100-item
page. The `refused` latch fired on *any* refusal, and the refusals were HTTP
**400** — probe variants the API rejects by design (`pageNr=0`, an offset where
a page number belongs). A warning that fires on a normal negative result is how
a diagnostic loses its credibility. It now requires **both** a 401/403 **and**
`_api_reached` still false; a 400 with a working API is logged as the ordinary
result it is.

**2. `deadline: 9999-12-31` is a placeholder, not a date.** It parses cleanly as
a real date 7,973 years out, so it clears every expiry check and shows the user
a deadline that does not exist. `_clean_sentinel_deadline()` blanks it (and
`0001-01-01`, `1970-01-01`, `2099-12-31`, `3000-01-01`, with or without a time
part) so the three-state model files the row as **rolling** rather than dated.
Applied to both the API and the card path. Real dates pass through untouched —
`2026-09-14` and `Sep 14, 2026` are unchanged.

### Next

Restore the session (`scripts/devaid_session.py push`, Chrome fully closed),
then re-run. Only then is pagination measurable.

---

## 2026-08-26 — DevelopmentAid: the "plan limit" was a self-inflicted 403

**The verdict from the last run was wrong, and this entry retracts it.**

The 2026-08-26 check reported `session check -> SIGNED IN` and then
`PAGINATION RESTRICTED BY PLAN — ... This is a limit on the ACCOUNT'S TIER`.
Both halves were unfounded, and the same log contained the disproof of each.

### 1. The API was never reached — `page.request` is a different client

Four lines apart, the same run recorded:

```
search API seen: POST .../api/frontend/tender/search (200)
tenders: API page 1 -> HTTP 403, stopping
```

Same endpoint, same session, same browser, seconds apart. **200** when the
site's own JavaScript called it; **403** when the scraper replayed it.

`page.request` shares the browser context's *cookies* but not its *network
stack*. It is Playwright's own HTTP client — its own TLS handshake, header
order, HTTP/2 settings frame and client-hint set. Cloudflare fingerprints
exactly those. So the replay presented a valid session from a client that
didn't match it, which is the shape of a stolen cookie, and got a 403.

One fault, reported five different ways, every one of which read like a real
measurement:

| Line in the log | What it actually was |
|---|---|
| `page size 300 not honoured (returned 0)` | 403 |
| `pageSize (1-based) failed after 0 probe pages` | 403 |
| `could not fetch any filter option lists` | 403 |
| `no status taxonomy available` | 403 |
| `this account reads one page per search` | inferred from the four above |

**Fix:** every API call now runs *inside* the page via `fetch(…, {credentials:
'include'})` evaluated in the document, instead of `page.request.post`. That is
Chromium's own network stack, same origin, same cookie jar, same client hints
— indistinguishable from the SPA's request because it *is* the SPA's request.
`page.request` is kept only as a fallback for when `evaluate` is unavailable,
and the log says which path answered.

New `_api_json()` / `_api_json_via_request()` in `developmentaid.py`; all five
call sites converted (`_count_items`, `_probe_pagination`, `_fetch_taxonomies`,
`_probe_total`, `_api_page`).

### 2. `session check -> SIGNED IN` was reading an advert

The evidence string gave it away: `member chrome present (a.membership-card
expert)`. `a.membership-card.expert` is the site's **upsell tile** — the thing
that advertises the Expert plan to people who don't have it. The old selector
list accepted `a[href*="/membership"]` and `[class*="avatar"]` as proof of
membership, so marketing aimed at non-members was read as evidence of
membership. On a page that was simultaneously displaying *"Info available only
for members"*.

`_membership_state()` now requires a control that only exists once
authenticated — a log-out link, or a link into the signed-in account area with
promo/card/banner/cta classes excluded. And the members-only paywall text is
now read as evidence **against**, because it is the site stating its own answer
rather than us inferring one.

Verified against a reconstruction of the observed DOM:

```
OUT  | the 2026-08-26 page: upsell card + members-only notice
IN   | genuinely signed in: log-out control present
IN   | signed-in account link, no logout in DOM
OUT  | plainly logged out
```

### 3. The plan-limit verdict now has to earn itself

"Your subscription is the ceiling" sends someone to spend money, so it now
requires **both** that membership was proved by a signed-in-only control **and**
that at least one API call succeeded this run (`_api_reached`). A run where the
API never answered has measured nothing about what the account may read, and
now says so instead:

> pagination dialog at page N while the session looks signed in — but NOT ONE
> API call succeeded this run, so the dialog is the visible half of a request
> that was refused, not proof of a plan limit.

`_probe_pagination` likewise now distinguishes "the API does not paginate for
this account" from "we never got an answer out of the API at all".

### Verify

```bash
python scripts/check_scraper.py developmentaid --pages 3
```

Look for `API page 1` no longer returning 403, and for the page-size probes
returning real counts rather than 0.

### Still open

`LOP_DEVAID_SECTIONS` in `backend/.env` is set to tenders only, so **grants are
not being scraped at all** — unrelated to the above, and a one-line fix.

---

## 2026-08-26 — NGOBOX confirmed fixed; GrantWatch names its own blocker

**NGOBOX: 0% → every row `[deep]`, `VERDICT: LOOKS CORRECT`, 8.5s.** The
scraper was never broken; `link_kind` was calling its per-grant URLs index
pages. Nothing in `ngobox.py` changed.

**GrantWatch: the new diagnostic answered in one line.**

```
[grantwatch] the page is a bot wall, not a listing
    (matched 'just a moment', title='Just a moment...').
    A parser change cannot fix this.
```

Cloudflare's JS challenge — and this scraper was walking straight into it.

`BaseScraper._fetch_rendered_sync` already waits out interstitials for every
other JS source. GrantWatch **overrides that method**, and in doing so lost the
behaviour: it navigated, waited 15s for networkidle, took a snapshot of the
challenge page, and reported zero grants. A 22-second run spent entirely on a
page that was asking to be waited for.

`_wait_out_challenge()` polls the title until the challenge clears, up to 45s.
This is not defeating a bot check — it is doing exactly what the check asks:
load the page, run its JavaScript, wait. It polls rather than sleeping a fixed
amount, so a page that clears in three seconds costs three seconds.

If it does **not** clear, the log now says what that means rather than leaving
it as a parser mystery: the challenge is not clearing for this client, most
likely because the server's datacenter IP is refused outright, and the options
are a different network, feed access from GrantWatch, or dropping the source.
That is a decision for a person, and the log should hand it over rather than
retry forever.

### Verified

`_wait_out_challenge` against three cases: a challenge that clears on the third
poll (returns True and logs how long it took), one that never clears (returns
False within the budget), and a page that was never challenged (returns True
immediately, no wasted wait).

---

## 2026-08-26 — `link_kind` was mislabelling deep links across every source

The batch 2 verification run reported NGOBOX at **0% deep links** and flagged it
`NEEDS WORK`. Its URLs are per-grant. The scraper was fine; the measurement was
wrong, and it has been wrong for every source.

**Two defects in `services/links.py::link_kind`, both saying "index" about a
page that names one specific call.**

**1. A greedy regex swallowed the slug.** The listing test was:

```
calls?(-for-[\w-]+)?$
```

`[\w-]+` is greedy, so it consumed the whole slug. That made

```
/community-development-2/call-for-proposals-biodiversity-fund-2026-ireland
```

match as a LISTING — a URL naming one call, one country, one year. Replaced
with a set of index segment names matched **whole**.

**2. Every single-segment path was treated as an index.** NGOBOX publishes each
grant at `/full_grant_announcement_Applications-Invited-for-2026-Civil-Society-…`
— one segment, unmistakably one grant. `_looks_specific()` now asks whether the
segment names a thing: 25+ characters, or built from three or more words. A bare
script name (`listing.php`, `index.aspx`) still reads as an index.

This is not cosmetic. `link_kind` decides what the dashboard **tells the reader**
— "opens the funder's listing page, you'll need to find the row" versus a direct
link — so readers were being warned off links that go straight to the call. It
also feeds the `deep%` column that every quality verdict in this project has
been judged on.

Corrected by this: NGOBOX 0% → its real figure, FundsForNGOs 83% → higher, Bond
42% → higher. Genuine index pages still read as listings: `/apply/`,
`/funding/`, `/grants`, `/applyingforfunds/`, and DevNetJobsIndia's
`rfp_assignments.aspx`, which really is the only route to some of its rows.

### GrantWatch returned 0 items — now it will say why

The run rendered for 21 seconds, presented a correct browser identity,
accumulated one page snapshot and parsed nothing. "0 items" is the least useful
thing that could be reported, because three different situations produce it: the
listing never rendered, the `/grant/<id>/` URL shape changed, or the site served
a bot wall.

`_report_empty()` now separates them. A bot wall is named as one and says a
parser change cannot fix it. Otherwise it logs the character count, the link
count, and the **commonest link prefixes on the page** — if grants have moved to
`/grants/<slug>/`, that line names the new shape immediately instead of leaving
it to be guessed.

### The rest of batch 2, confirmed working

- **FundsForNGOs** — 100 items over 2 pages, 100% of deadlines parse. The
  `blank organisation: 92` in the check output is expected: that column shows
  the raw scrape, and `_ingest` fills the funder from the summary at save time.
- **DevNetJobsIndia** — 35 items, and 14 rows dropped with
  `no job_id recoverable … dropping the row rather than pointing it at the
  index`. That is the batch 2 fix working, not a regression.
- **Bond UK** — 448 items via 49 Load More clicks. Its 94 duplicate URLs are a
  characteristic of the source, not a fault: one funder page often hosts several
  distinct programmes, and the rows carry different titles, so dedup keeps them
  apart. Left alone deliberately.

### Verified

18 URLs taken from the live run: every one previously mislabelled now reads
`deep`, and every genuine index page still reads `listing`. Two cases I had
expected to flip did not — `/applyingforfunds/` and `rfp_assignments.aspx` — and
on inspection the code was right and my expectation was wrong; both really are
index pages. GrantWatch's diagnostic exercised on a bot wall, a moved URL shape
(correctly reporting `[('/grants', 12), ('/about', 1), ('/login', 1)]`), and a
normal page that still parses.

---

## 2026-08-26 — Batch 3: the nine "awarded grants" sources

Clean Air Fund, Rockefeller, Gates, Laudes and CJRF all point at a page listing
money **already given**: "our grants", "committed grants", "grants database".
Four more sources in the audit do the same. Whatever those pages yield, nobody
can apply to it, and no parser change reaches that — the URL is aimed at the
wrong page.

Two things were needed, and neither is a guess.

### 1. The gate could not tell a past award from a call

This is the hardest junk to reject. *"$500,000 grant to the Clean Air Institute
for monitoring in Lagos"* carries every funding word a real call carries — the
vocabulary test passes it, the amount test passes it, and the URL is under
`/grants/`, so the href test passes it too. All three of the gate's positive
signals fire on a grant that closed years ago.

`is_already_awarded()` matches **past-tense phrasing only**, and that
restriction is the whole design. "Award" alone is not evidence and never can be
— an award is also a thing you apply for. *"Young Scientist Award 2026: call for
nominations"* must survive. What gives a past award away is the grammar around
it: awarded **to** someone, someone **receives**, we **announce** the
recipients, **congratulations** to our fellows, **funded projects** 2025.

Plus procurement's own name for a decision already taken — `Contract Award`,
`Notice of Award` — which is followed by a colon rather than by "to", so it
needs naming explicitly.

**This one applies to curated sources too.** A tender board publishes contract
awards alongside its open notices — World Bank's feed is mostly awards — so
"this page contains only opportunities" does not mean "and none of them have
already been decided". It is a second line of defence behind
`worldbank.py`'s `notice_type` filter.

### 2. New — `backend/scripts/find_listing_url.py`

Repointing five sources means finding each funder's real open-calls page. The
tempting way is to guess `/funding-opportunities` and move on. That is exactly
how World Bank ended up with a pagination template that had been doing nothing
for months: **a URL that loads tells you nothing about whether it lists what you
want.**

So this measures. For every candidate it fetches the page, runs the source's own
parser, puts every row through the opportunity gate, and scores what survives:

```
rows  40  open   0  dated   0  awarded  38   [configured] .../our-grants/
rows   9  open   9  dated   7  awarded   0   [site navigation] .../funding-opportunities
```

The winner is the URL yielding the most rows that are real opportunities — not
the most rows, and not the one that merely responds.

Candidates come from the site's own navigation first (a path the site offers
beats one invented here), filtered so a link saying "our grantees" is never
followed, then a list of conventional paths tried on the same domain.

**The `awarded` column is the point.** A page scoring `rows 40 / open 0 /
awarded 38` is a grantee list, and saying so is more useful than any repointing:
that funder may publish no open calls at all. The script reports `AWARDS ONLY`
and recommends **removing the source** rather than leaving it to add noise every
run. Deleting a source is a real answer, and one a URL guess would have hidden.

`--write` applies the recommended URL to `sources.json`.

### Verified

The past-award rule against 18 cases: 11 rejections including
`$500,000 grant to…`, `Announcing our 2026 grantees`, `Meet the grantees of…`,
`Six organisations have been selected…`, `Congratulations to our 2026 Fellows`,
`Contract Award:` and `Notice of Award —`; and 7 keepers including
`Young Scientist Award 2026: call for nominations` and
`Innovation Award — apply by 30 September`, which a naive "award" rule would
have deleted. 18/18.

The finder against a stub Clean Air Fund: the configured grantee page scores
`rows 3 / open 0 / awarded 3`, a funding page scores `rows 2 / open 2 / dated
2`, and the ranking picks the funding page. Nav-link filter confirmed to follow
"Funding opportunities", "Apply for funding" and "Open calls" while skipping
"Our grantees", "Past grants", "Committed grants", "News" and "Annual report".

---

## 2026-08-26 — Batch 2: FundsForNGOs, NGOBOX, DevNetJobsIndia, GrantWatch, Bond UK

All five are hand-written modules. Three carried real defects; NGOBOX was clean.

### 1. GrantWatch and Bond UK were building the browser the wrong way

Both had their own `_fetch_rendered_sync` doing:

```python
browser = pw.chromium.launch(headless=True)
page = browser.new_page(user_agent=settings.user_agent)
```

That is the exact pattern `site_auth.py` was written to eliminate, and its
docstring explains why: `settings.user_agent` hard-codes `Chrome/126.0.0.0`,
but a browser also announces its version in the **Sec-CH-UA client hints**,
which come from the real build and cannot be overridden that way. So every
request said "I am Chrome 126" in one header and something else in the next —
a stock bot signature, and the documented cause of ADB being refused by
Cloudflare for two days.

Both now use `site_auth.open_context(...)`, which keeps the browser's own
identity, removes only the word "Headless" from it, drops
`navigator.webdriver`, and prefers real Chrome over bundled Chromium. Neither
site is known to be blocking today — this is removing a signature that has
already cost this project once.

### 2. Rows that open the index instead of the opportunity

Your original complaint, still alive in two modules.

**DevNetJobsIndia** — `_detail_url()` ended `return self.start_url`. That is
literally the bug `services/links.py` documents: *"86 different RFPs all
pointing at rfp_assignments.aspx"*. Every one opened the index it was scraped
from, and every one shared a URL, which also defeats deduplication. It now
returns `""` and logs the title it dropped: `ScraperManager` refuses to store a
row with no link to the call, so the row goes rather than shipping as a lead
that goes nowhere. Losing a row beats publishing one that wastes a click — and
unlike a bad link, a missing one shows up in the counts.

**Bond UK** — `opportunity_url=apply_url or self.start_url` had the same
fallback; the bare index form is gone. The anchored form (`start_url` plus the
post's own `#id`) is kept, because that one does scroll the reader to the card.

### 3. FundsForNGOs walked past the end of its own data

`next_page()` never returned `None`. It relied on the *next* request failing —
spending one guaranteed-failed request per run and logging a fetch error that
reads like a fault. Worse, the stop test was:

```python
if isinstance(posts, list) and len(posts) < PER_PAGE: return None
```

The API answers past-the-end with `{"code": "rest_post_invalid_page_number"}` —
a **dict**, so `isinstance(..., list)` is `False`, the whole condition is
`False`, and it asked for the page after that too. Walking past the end of the
data asking for more of it.

Now: not-a-list stops, a short page stops, invalid JSON stops, and `MAX_PAGES =
400` is a runaway ceiling rather than a target. Also removed a dead
`... if False else ...` expression left in the pagination line.

### NGOBOX — no change

Its parser reads NGOBOX's real captured markup, its `next_page` prefers an
explicit Next link and otherwise takes the smallest numbered page above the
current one (never hard-coded), and `parse_detail` enriches from the detail
page. Nothing to fix.

### Verified

FundsForNGOs pagination across five payload shapes — full page, short page, the
API's error dict, an empty list, and non-JSON — each stopping or continuing
correctly, plus the cap. DevNet returning `""` for an unrecoverable id and a
real `job_id=300671` URL when one is present. Both browser modules confirmed to
route through `site_auth.open_context`. All five import and register; the
registry still reports 85.

---

## 2026-08-26 — World Bank: three faults only the live data could show

The API module works — 300 rows over 3 pages, 100% deep links, 100% parseable
deadlines, 0 junk, 0 duplicates, 2.7s. Up from 34 rows. But the run also
exposed three faults, and each was invisible until real records arrived.

**1. The deadline was the wrong field.** All 300 rows came back with the same
date: yesterday. `submission_date` is when the notice was **published**;
`submission_deadline_date` is the deadline. My candidate list had them the wrong
way round. A uniform value across hundreds of rows is the shape of a wrong
field, not of a real coincidence — and every one of those rows would have been
stored as already-closed.

**2. Most records are Contract Awards.** `notice_type: Contract Award`
dominated the sample — contracts already given to someone. Not opportunities,
nobody can bid on them, and they would have flooded the dashboard with the same
"awarded, not open" noise the audit flagged on nine other sources.

`is_open_notice()` keeps Invitations for Bids, Requests for
Expressions of Interest / Proposals / Quotations, procurement and
prequalification notices, and drops awards, cancellations and annulments.
Matched as case-insensitive substrings because the API spells these out in
prose. Closed types are checked **first**, so "Contract Award Notice" cannot
match "procurement notice". A blank type is kept — unlabelled is not the same
as closed, and the deadline decides it downstream.

The kept/skipped counts are logged per page, and a page where *everything* was
filtered logs the `notice_type` values it saw — if the vocabulary ever changes,
that line says so instead of the source quietly going to zero.

**3. The total is 416,361 — the entire historical archive.** At 100 per page
that is 4,164 pages, and `stale_page_streak_override = 0` ("walk to the end")
plus the platform's 2,000-page cap would have walked 200,000 records of mostly
closed history on every run.

The API returns newest-first, so `max_pages = 60` walks the 6,000 most recently
published notices, which is where every open one lives. The comment says the
part that matters: **raising this does not find more open tenders, it finds
older closed ones.** If open notices are being missed, the fix is a server-side
filter on `notice_type`, not a bigger number.

**Also:** `procurement_group` arrives as a two-letter code. "CW" in the sector
column is not information; `PROCUREMENT_GROUPS` maps it to "Civil Works",
"Goods", "Consultant Services" and the rest, falling back to the raw value for
anything unmapped.

### Verified

`is_open_notice` across nine notice types including the "Contract Award Notice"
trap. A page shaped like the live one — two awards, one Invitation for Bids, one
Request for EOI — keeps exactly the two open records, reads their deadlines from
`submission_deadline_date`, and renders `CW`/`CS` as Civil Works / Consultant
Services. Pagination steps to `os=5900` and stops at page 60 with the window
explained. A page of nothing but awards returns empty and logs the types it saw.

---

## 2026-08-25 — World Bank moves to its API; UNDP needed nothing

The probe's API observation paid for itself on the first run.

### World Bank — new module `backend/app/scrapers/worldbank.py`

```
the page loads its listings from an API:
  https://search.worldbank.org/api/v2/procnotices?format=json&fct=...
=> SINGLE PAGE
```

The `os={offset}` template in `sources.json` was never part of that page's URL
contract. The listing is rendered client-side from
`search.worldbank.org/api/v2/procnotices`, and the paging lives in **that**
request — so no query parameter on the page URL could ever have worked, and the
source had been returning its first 34 rows while looking perfectly configured.

Now it reads the API directly. Two consequences worth stating:

- **No browser.** It is a plain JSON endpoint, so `requires_js = False` and the
  JSON is parsed inside `parse_listing()`. That reuses every part of
  `BaseScraper` — retries with backoff, rate limiting, the pagination loop, the
  repeated-content guard — instead of reimplementing them in a custom `crawl()`.
  The only unusual thing is that the "html" handed to the parser is JSON.
- **It stops on the API's own `total`**, the same exactness that makes UN
  Partner Portal's walk complete, rather than guessing when the list ended.

Field names are read through candidate lists rather than hard-coded, and the
first run logs the keys it actually saw — the endpoint could not be reached from
where this was written, and guessing one spelling is how a scraper ends up
storing rows with an empty deadline that the pipeline then treats as
permanently open. One run turns the guess into a fact.

`organization` is set to the **borrowing agency** (Ministry of Health,
Nairobi Water Authority…), not "World Bank". The Bank finances the procurement;
the agency runs it, and that is what a bidder needs to see.

The dead `world_bank` entry is removed from `sources.json` (71 entries remain).

### UNDP Procurement — nothing to fix

I had this down as "the weakest of the five, most likely page 1 only". Wrong,
and in the good direction: the probe reports **394 listings on page 1** and every
candidate returning the same rows, with no API behind it. UNDP publishes its
whole notice board on one page. `SINGLE PAGE` is the correct answer and needs no
template — which is exactly the distinction that verdict was added for. Without
it this source would have been "fixed" into a bug.

### Verified

`worldbank.py` against a realistic `v2/procnotices` payload carrying both field
spellings (`bid_description`/`noticetitle`, `project_ctry_name`/`country_name`,
`submission_date`/`submission_deadline_date`): both rows mapped correctly,
ISO timestamps trimmed to dates, `organization` taken from the borrower,
`opportunity_url` scoring `deep` and `is_usable_link` True, deadline parsing to
a real date. Pagination steps `os=100`, `os=200`, then stops exactly on
`total=250`. Hostile payloads — a 403 HTML body, a JSON object with no record
list, a bare array, a response with no total — each produce a specific error or
a correct stop rather than a silent empty page.

---

## 2026-08-25 — Probe round 1: three blind spots, and a verdict of mine that was wrong

First live run of `probe_pagination.py` on EC2. It worked — including by
contradicting me.

### UN Partner Portal now works on the server

`VERDICT: LOOKS CORRECT`, 19.0s, countries resolving (Uzbekistan, Mali,
Argentina, Ukraine, Burundi, Gambia, Senegal). The two-step form login carried
on EC2, where there is no Chrome profile. That closes the item.

### World Bank is **not** complete — my earlier verdict was wrong

I called it "the only correctly configured source". The probe says otherwise:

```
World Bank — already configured: ...opportunities?lang=en&os={offset}
page 1: 34 listing(s)
[no] ?os=34   ...&os=34   same rows as page 1 (100% overlap)
   ... all 18 candidates: same rows as page 1
=> SINGLE PAGE
```

**The configured `os` parameter changes nothing.** The template is real, it is
the right dialect, and the site ignores it — so World Bank has been returning
its first 34 rows and nothing else, while looking correctly configured. A
template that is present is not a template that works, which is the entire
reason this probe compares listings rather than checking that a URL loads.

Two loose ends it also exposes: the config says `page_size: 20` while page 1
parses to 34 rows, so even the offset arithmetic was working from a wrong
number.

### Three fixes to the probe itself

**1. "page 1 never loaded" now says why.** UNDP Procurement returned exactly
that and nothing more — and a timeout, a DNS failure, a TLS error and a WAF
block all produce it, only one of which is worth retrying. The exception is now
captured and printed. This is the same class of unhelpful message this project
keeps replacing; I wrote a fresh one.

**2. It now watches the page's own API traffic.** When a board renders its list
from an XHR, the pagination lives in that request and not in the address bar —
so every URL guess correctly reports "same rows as page 1", because the URL
genuinely changes nothing, and the probe concludes nothing when the answer was
in plain sight. It now records JSON responses, picks out those carrying
page/offset/rows parameters (including bracketed names like `searchstax[page]`),
and prints them under *"the page loads its listings from an API — pagination
lives in that request, not in the page URL"*. New verdict `API PAGINATED`.

**3. Bespoke sources are no longer misreported.** ADB drives the SearchStax
widget with `searchstax[page]=N`; no generic parameter guess will ever produce
that, so probing it and printing `NO CANDIDATE` reads as "this source is broken"
when the truth is "this probe does not test it". Hand-written modules are now
labelled up front and their verdict is `OWN CODE`.

### Verified

Against a stub JS board whose URL parameters do nothing and whose listings come
from `/api/opportunities/search?rows=20&os=0`: all 18 candidates correctly
rejected, the API call correctly identified, and an analytics script correctly
ignored.

---

## 2026-08-25 — DevelopmentAid: say whether we were signed in, then blame the plan

The pagination-restriction message asserted a subscription limit:

> *"This is a subscription limit on how deep any single search can be read, so
> no scraper change can reach the rest of the archive."*

That conclusion was not supported by what the code actually knew, because the
session check that should have established it had a false negative.

**The bug.** The check was `not is_signed_in(page) and not <member chrome>`.
`is_signed_in()` returns True when it finds no visible "Sign in" control — and a
blank page, a Cloudflare interstitial or a failed load contains no such control
either. So a broken page read as **SIGNED IN**, the "NOT LOGGED IN" error never
fired, and the run went on to blame the subscription for a restriction it might
never have hit as a member. A logged-out visitor gets the same dialog.

**Fixed.** `_membership_state(page)` returns one of three values with the
evidence for it, and the result is logged on **every** run rather than only on
failure:

| state | evidence |
|---|---|
| `SIGNED IN` | member chrome present (avatar / user menu / logout link) |
| `SIGNED OUT` | a visible Sign in control and no member chrome |
| `UNCLEAR` | neither — a near-empty page, a challenge, or an uninspectable one |

`UNCLEAR` is not a failure, it is the honest third answer, and it is logged as a
warning saying every later verdict in the run is unattributed.

The restriction message now branches on it. Signed in, it says the account's
**tier** is the limit and no code change reaches past it. Signed out or unclear,
it says explicitly *"do not read this as a subscription limit yet"* and points
at `scripts/devaid_session.py push`.

**On credentials.** `scripts/devaid_session.py` already documents why an email
and password cannot be used here: DevelopmentAid's login is behind reCAPTCHA, so
a scripted sign-in is blocked by design and attempting it is what their terms
restrict. `LOP_DEVAID_EMAIL` / `LOP_DEVAID_PASSWORD` exist in config but no code
path types them into that form. The supported route is unchanged and is the
right one: a human signs in once on a machine with a screen, and only the
resulting session travels.

### Verified

`_membership_state` against five page shapes: member chrome → `in`; visible Sign
in → `out`; a 120-character "Just a moment..." page → `unknown` (this is the
case that previously reported `in`); a full page with neither signal →
`unknown`; and an evaluate that raises → `unknown`.

---

## 2026-08-25 — Batch 1: the five big boards

### New — `backend/scripts/probe_pagination.py`

71 of 85 sources have no pagination configured. Writing a `page_url` template
for each by reading its markup is 71 sites of manual work, and the answer is
often wrong on the first try: **a URL that loads is not a URL that returns
different listings.** Several boards answer an out-of-range page by re-serving
page 1, and some accept `?page=2` and ignore it.

So this probes rather than guesses. For every candidate it fetches the page,
runs **the source's own parser** over it, and compares the set of opportunity
URLs against page 1. A candidate only wins if it produces listings page 1 did
not — the test a human would apply, applied consistently.

```
python scripts/probe_pagination.py undp_procurement
python scripts/probe_pagination.py undp_procurement --write
python scripts/probe_pagination.py --all --only-unconfigured
```

Candidates come from two places: the page's own controls (`rel=next`, a
numbered "2" link, a next/older/load-more label) tried first because a URL the
site offers cannot be an invented parameter, then 7 page-number parameters, 8
row-offset parameters and 3 `/page/N/` path shapes.

Five verdicts, and the distinctions matter:

| verdict | meaning |
|---|---|
| `FOUND` | a template that works — `--write` puts it in `sources.json` |
| `SINGLE PAGE` | page 2 exists but repeats page 1: the listing genuinely is one page, no fix needed |
| `NO CANDIDATE` | nothing returned different rows — likely XHR or infinite scroll, needs its own module |
| `NO BASELINE` | page 1 parsed to nothing, so there is nothing to compare; fix the source first |
| `ERROR` | the probe itself failed |

`SINGLE PAGE` versus `NO CANDIDATE` is the one worth having: the first needs no
work at all, and without the distinction every small foundation site looks like
a bug.

Output templates keep literal `{page}` / `{offset}` braces — `urlencode` turns
those into `%7Bpage%7D`, which is not a format placeholder, so the crawler would
have asked each site for a page literally named `%7Bpage%7D`.

### Changed — `backend/app/scrapers/adb.py`

Two related corrections, both about the same misunderstanding.

**The sort comment was backwards.** It read "the sort puts the soonest closing
date first". The code asks for `ds_date_closing desc` — *latest* closing date
first. The code is right and the comment was wrong, and the direction is
load-bearing: a tender that is still open closes in the future, so
descending puts open tenders at the FRONT and the 37,769 closed ones at the
back where the walk never reaches. Flipped to ascending, the crawl would spend
its entire page budget on records that get discarded on save.

**The unfiltered warning overstated the damage, and the budget understated the
need.** The old message said an unfiltered run sees "only the first 720 of
51,013" and was therefore nearly useless. That ignored the sort. ADB's own facet
counts are ~489 Active plus ~396 Advance Notice — roughly 885 records with a
future closing date, about 74 pages at 12 per page. The 60-page cap stopped ~15
pages short of that, so an unfiltered run really did drop live tenders — just
for a different reason than the warning gave.

Added `unfiltered_max_pages = 110` (1,320 records), used only when the Status
facet fails to apply, and rewrote the warning to say what is actually true: the
sort puts the open tenders at the front and they should all be inside the
budget, but it is guesswork rather than a filter, so fix the facet if it
recurs.

### Verdicts for the other three

- **UN Partner Portal** — complete. Stops on the API's own `count`, 61 of 61.
  No change needed; EC2 is waiting on the two-step login fix.
- **World Bank** — complete. The only source that already had a correct
  `{offset}` template. No change.
- **DevelopmentAid** — capped at 50 listings per section by the account's
  subscription, which refuses page 2 with a dialog. `_MAX_PAGES` is 30,000; the
  scraper never gets the chance. Nothing to fix in code. Worth noting the
  default sort is Modified Date, so the 50 it does get are the freshest.

### Verified

`probe_pagination.py` against a stub site in three shapes: one that paginates
with `?page=N` (→ `FOUND`, correct template, and the template proven to
`.format()` into a real URL), one whose page 2 repeats page 1 (→ `SINGLE PAGE`,
after correctly rejecting all 18 parameter candidates), and one whose page 1
parses to nothing (→ `NO BASELINE`). `--write` confirmed to patch `sources.json`
with literal braces and the right `page_size` for offset templates. ADB's page
budgets checked against its published facet counts.

---

## 2026-08-25 — UNPP sign-in is two-step; the API route was CSRF, not credentials

The previous attempt still returned 0 rows, and its own diagnostics named both
reasons in one screen.

**1. The login form is two-step.** The improved error printed the field list:

```
no password field on https://www.unpartnerportal.org/login after 30s.
title='UN Partner Portal' inputs=['email:email']
```

One field. An email box, no password. The page rendered perfectly — UNPP asks
for the email, you advance, and *then* the password field appears. Code that
navigates to `/login` and waits for `input[type=password]` waits forever on a
page that is working exactly as designed. The 30-second timeout was reporting a
real fact and drawing the wrong conclusion from it.

`_form_login()` now: fills the email → looks for a password field for **2.5s**
(the single-step case, deliberately short so the common two-step path is not
delayed) → if absent, submits the email step and waits up to 30s for the
password field → fills it and submits. Handles both form shapes without needing
to know which it is.

**2. Every API endpoint answered with Django's own 403 page**, not DRF's JSON:

```
POST https://www.unpartnerportal.org/api/login/ -> 403 <!DOCTYPE html>
    <meta name="robots" content="NONE,NOARCHIVE"> <title>403 Forbidden</title>
```

That is `CsrfViewMiddleware` — the request never reached a view, so it says
nothing about whether the credentials are right. `_api_login()` now lands on a
portal page first so Django sets its `csrftoken` cookie (the fetch helper
already forwards it as `X-CSRFToken`), and when a 403 does come back as HTML the
log says *"Django's own 403 page — CSRF was rejected, so this never reached a
view. Not a credentials problem"* rather than leaving it to be misread.

**Order changed: form first, API second.** The server showed us which route the
portal actually supports. The API stays as a fallback because when it does work
it is cleaner, but it no longer costs 15 seconds of 403s before the real route
is tried.

**3. A token in browser storage now counts as a session.** A single-page app
that authenticates by token keeps it in `localStorage`/`sessionStorage` and
attaches it to its own requests — a plain `fetch` from this code would not, so a
perfectly good sign-in could still look absent. `_token_from_storage()` scans
both stores (including tokens nested inside JSON blobs), and verifies each
candidate against `/api/accounts/me/` before using it. A long string under a key
called "token" is a guess until the portal confirms it.

**4. The failure message now distinguishes its cases.** `inputs=[]` → the page
never rendered. `inputs=[...]` with no password → a multi-step sign-in that did
not advance, most likely because the email step reported an error such as an
unknown account. Off-domain redirect → sign-in moved to an external identity
provider.

### Verified

Against a stub that behaves the way the server logs describe — email field
first, password only after the step is submitted: sign-in succeeds via a session
cookie, and separately via a token recovered from `localStorage` when no cookie
is set. Regression: an already-valid browser session still short-circuits
everything; the API fallback is still reachable when the form cannot be driven
at all; registry, curated flag, ISO country mapping and the DOM fallback parser
all unchanged.

---

## 2026-08-25 — UNPP signs in through the API, not the form

UN Partner Portal returned 0 rows on EC2 while working perfectly on the laptop.
The server log named the cause exactly, which is what the logging was for:

```
[un_partner_portal] the portal's own request failed:
    GET https://www.unpartnerportal.org/api/accounts/me/ -> 401
[un_partner_portal] signing in as nitin@catalysts.org
[un_partner_portal] the login form did not look the way this code expects —
    no email/password field found on https://www.unpartnerportal.org/login
```

**Why the two machines differ.** The laptop never used the password: it copies
your everyday Chrome profile, which is already signed in (`already signed in via
the saved browser session`). EC2 has no Chrome, no mirrored profile and no
session file, so it falls through to the credential route — the one the laptop
had never exercised.

**And that route had a bug of mine.** The portal is a React app: `/login` serves
an empty shell and renders the form afterwards. My code waited a fixed
`wait_for_timeout(2_000)` and then called `query_selector`, **which does not
wait at all**. Look at the timestamps — 11:24:07.786 "signing in", 11:24:10.254
"no email/password field": it gave up 2.5 seconds after navigation, before the
form existed. The message reads like the portal had been redesigned. It had not.

### Fixed — and not merely by waiting longer

**1. Sign in through the API instead of driving the form.** Posting credentials
to the portal's own sign-in endpoint is strictly better than filling a form:
nothing to render, no button to locate, no CAPTCHA to trip over, and a definite
answer — a token, or an HTTP status saying why not.

`_api_login()` tries `/api/accounts/login/` and four other DRF-shaped paths,
with both `email` and `username` field names, reads the token from any of six
key spellings, and tries `Token` / `Bearer` / `JWT` as the scheme. **Every
combination is verified against `/api/accounts/me/` before being used** — a 200
from a login endpoint is not proof the token works, and a token sent with the
wrong scheme fails silently as an empty listing, which is the exact failure this
module exists to prevent. It stops probing field names on a 404/405 (the
endpoint isn't there, so field names cannot help) and logs each rejection.

**2. Session checks now ask the portal, not the page.** `_signed_in()` used to
read the rendered DOM for words like "Dashboard" — a guess about a React app
mid-render. It is now `_whoami()`: a 200 from `/api/accounts/me/`. That is the
portal's own answer, and it is what correctly reported 401 on the server while
the identical code was signed in on the laptop.

**3. The form is still there as a last resort, and now actually works.**
`wait_for_selector("input[type='password']", timeout=30_000)` instead of a fixed
sleep plus a non-waiting query.

**4. When it does fail, it says what it saw.** "No email/password field found"
is true and useless — it cannot distinguish a page that never rendered, a
redirect to a corporate SSO host, and a genuinely redesigned form.
`_report_login_page()` now logs the final URL, the page title and every `input`
on the page, and calls out an off-domain redirect explicitly:

- `inputs=[]` → the page never rendered
- `inputs=[password:pw, text:user]` → the form is there, selectors are wrong
- redirected to another host → sign-in moved to an external identity provider,
  the credential route cannot work, use an imported session

Order of preference is unchanged and now enforced by working code: existing
browser session → API credentials → form.

### Verified

Against a stub portal, all four paths: DRF `Token` at `/api/accounts/login/`;
`Bearer` with an `access` key at `/api/auth/login/`; a `username` field instead
of `email`; and no login API at all, which falls through to the form and then
reports `inputs=[]` rather than blaming the portal. Plus the pre-existing route
where the browser session is already valid and no header is needed, and a full
discovery + pagination run confirming the token header is carried on every
request. Registry, curated flag, ISO country mapping and the DOM fallback all
still pass.

**Not verified: the live endpoint.** `/api/accounts/login/` is the expected DRF
shape and the first thing tried, but the portal has not confirmed it yet. If
none of the five paths answers, the log now prints the status and body of each
attempt, which names the real one.

---

## 2026-08-25 — `deploy/update.sh` reported a false failure

The first deploy of the changes below ended with:

```
FAILED: the API did not answer on http://127.0.0.1:8001
```

**The deploy had worked.** `supervisorctl status` showed the service RUNNING
with three minutes of uptime, `python -c "import app.main"` imported cleanly,
and `curl $API/api/config` returned the new payload. Nothing was broken.

The script did `sudo supervisorctl restart` then `sleep 8`. Startup runs the
migrations, the FTS index check and the column backfills against the whole
database — on the production database (~176 MB) that takes well over eight
seconds, so the check ran while the worker was still booting.

A false failure is worse than no check at all: it arrives exactly when someone
is deciding whether to roll back, and it argues for rolling back a good deploy.

**Fixed:** the script now polls `/api/config` every 3s for up to `BOOT_TIMEOUT`
(default 180s, override with `BOOT_TIMEOUT=600 ./deploy/update.sh`) and prints
how long the API actually took. Crucially it also breaks out **early** if
supervisor stops reporting RUNNING — slow and dead look identical to a fixed
sleep, and the real failure is the one worth catching quickly. If it does time
out while the service is still running, the message now says so and tells you to
check again rather than implying the deploy failed.

Also corrected: the two `die` messages pointed at `backend/logs/app.log`, which
does not exist on the server. The real log is
`<repo>/logs/supervisor-err.log`.

Verified both paths against a stub server: a service that starts answering after
7s is now reported as *"answered after 9s"* (the old script failed it at 8s),
and a service that never answers produces the timeout message rather than a
crash.

---

## 2026-08-25 — Closed calls, wrong rows, dead links, duplicates

Three complaints, three separate root causes. All three were found in the code,
not guessed at.

### 1. Closed calls showing as "Ongoing" forever

**Root cause — one line.** `generic_listing.py` ended every scraped row with:

```python
assume_active=not bool(m)     # m = the deadline regex match
```

In `ScraperManager._ingest`, `assume_active` means *"the source says this call
has no closing date"* — rolling, open-ended, until filled. That line instead
made it mean *"our regex failed to find a date"*. The two are completely
different things, and conflating them made every unparsed row a **rolling call,
which by definition never expires**. A call that closed in March was still
listed as open in August, and nothing downstream could ever close it, because
as far as the pipeline knew the funder had said it never closes.

**Fixed in three places, because one alone would have made things worse:**

- `generic_listing.py` — `assume_active` is now set by `_says_rolling()`, which
  delegates to the same `DeadlineParser.is_ongoing()` the ingest pipeline uses.
  One definition of "ongoing", shared by the scraper and the saver; two
  independent notions is how a row ends up live in one layer and closed in the
  other.
- `scraper_manager._ingest` — the deadline now has **three** states instead of
  two:

  | state | meaning | result |
  |---|---|---|
  | dated | a date we could read | Active or Expired, decided by the date alone |
  | rolling | the source *says* there is no closing date | Active |
  | unknown | no date, and no such statement | Active **for now** — retired by `audit_deadlines()` once the source stops listing it |

  "unknown" must not be treated as expired (that would hide every row from a
  funder who simply doesn't print dates) and must not be treated as rolling
  (that is the immortality bug). It is a lease: renewed each time a scrape sees
  the row again (`last_seen`), allowed to lapse after
  `LOP_ONGOING_MAX_AGE_DAYS` (21).

  Without the `_ingest` change, the `generic_listing.py` fix alone would have
  flipped thousands of rows straight to Expired and emptied the dashboard.
- Each page now logs the split — *"14 undated row(s): 2 state no closing date,
  12 we could not read one from"*. The fix is only trustworthy if that ratio can
  be watched.

### 2. Rows that open a random search page

**Root cause.** `links.py → search_link()` ended with:

```python
return f"https://duckduckgo.com/?q=site%3A{domain}+{q}"
```

When a row had no usable URL, `resolve_link` handed the reader a **web search**.
The intention was reasonable — better than a dead end — but the effect is what
you saw: entries that look clickable and land on a search engine instead of the
call. A search result is not a lead. The reader still has to find the call,
judge whether the top hit even is the call, and often discover it never existed.

**Fixed at the source, not the symptom:**

- `_ingest` now **refuses to store a row with no link to the call itself**
  (`LOP_REQUIRE_USABLE_LINK`, default on). The title alone is not a lead.
- `search_link()`'s web-search and homepage fallbacks are gone. `resolve_link`
  returns `("", "none")` rather than inventing a destination.
- Frontend: `link_kind: "none"` is handled and labelled *"no link published —
  nothing to open"*. Without this those rows fall through to `isDirect` and are
  presented as a direct link to the call while actually opening the funder's
  homepage. Only matters until the cleanup has run — it is a safety net, not the
  fix.

### 3. Rows that are not opportunities at all

**Root cause.** Most sources are scraped by the heuristic path: harvest every
`<a>` on the page, then reject what looks like site furniture. A blocklist can
only ever catch junk it has already met, so every new funder site contributes
fresh vocabulary and a few more wrong rows — which is why the guard lists in
`generic_listing.py` keep growing.

**New — `app/services/opportunity_gate.py`.** Flips it round: a row must show
**positive** evidence that it is an opportunity, against the team's own
definition — grants, RFPs/RFQs/RFIs/EOIs, calls for proposals, funding
opportunities, partnership opportunities, tenders, plus the adjacent forms the
classifier already knows (fellowships, scholarships, awards, challenges).

Three ways to fail, and the reason is returned so rejections can be grouped by
cause rather than counted in one lump:

- `furniture` — "Skip to main content", bare email addresses
- `section heading, not a call` — "Our Grants", "How to apply", "Programme"
- `page type is never an opportunity` — `/news/`, `/blog/`, `/grantees/`,
  `/annual-report/`, `/team/`… checked as **whole path segments**, so `/news`
  cannot match `/newsletter-signup`
- `no opportunity signal` — nothing in the title, summary or URL says this is a
  call

**The `curated` flag, and why it is not optional.** A blanket vocabulary
requirement would have been a disaster. UN Partner Portal's `/cfei/open` is a
list of Calls for Expression of Interest — every row is an opportunity by
construction — but its titles read *"Disability Inclusion Assessment"* and
*"First Foods Gujarat — Implementation and Capacity Support Partner"*. Not one
funding word between them. A strict filter would have deleted a whole source of
good leads.

So `BaseScraper.curated = True` declares "the page I read contains opportunities
and nothing else". Those rows skip the vocabulary test but **not** the furniture
or page-type tests — a curated board can still link to its own news page.

Curated: UN Partner Portal, ADB Tenders, DevelopmentAid, DevNetJobsIndia,
NGOBOX, GrantWatch Intl, and (via `"curated": true` in `sources.json`) UNDP
Procurement, World Bank, Globaltenders. Everything else must prove itself row by
row.

Measured effect of the flag on real titles:

| title | non-curated | curated |
|---|---|---|
| Consulting Services for Urban Water Supply (adb.org/projects/8891-002) | rejected | kept |
| Strengthening Health Systems in Kurdistan (devnetjobsindia jobdescription.aspx) | rejected | kept |
| Announcing our 2026 grantees (cleanairfund.org/news/...) | rejected | **rejected** |

### 4. New — `backend/scripts/clean_dashboard.py`

One command that cleans the existing database. **Dry run by default**: it prints
exactly what `--apply` would remove, with real examples from your own data, so
you can disagree with a pass before anything is deleted.

```
python scripts/clean_dashboard.py                    # report only
python scripts/clean_dashboard.py --apply            # do it
python scripts/clean_dashboard.py --apply --vacuum   # ...and shrink the file
python scripts/clean_dashboard.py --skip gate,duplicates   # drop a pass you disagree with
```

Six passes, in this order — junk is removed *before* duplicates are counted, so
a duplicate group is never "resolved" by keeping the junk copy:

1. **Deadlines** — recompute Active/Expired from the date, clear sentinel dates,
   retire stale Ongoing. **Archives, never deletes.**
2. **Furniture** — deletes page furniture stored as calls.
3. **No link** — deletes rows with nothing to open (the search-page entries).
4. **Not an opportunity** — deletes rows failing the gate, reported by reason
   and by worst source.
5. **Duplicates** — the `unique_id` fingerprint includes the URL and the
   deadline, so the same call stored with a `?utm_source=` parameter, under two
   source names, or after its date was re-parsed, hashes differently and both
   rows survive. Groups on normalised title + organisation and keeps the best
   copy: real deep link > any link, **clean URL > tracking parameters**, dated >
   undated, Active > Expired, longer summary, then newest. (The tracking-URL
   rule matters more than it looks — without it the survivor is decided by
   scrape order, and half your links end up carrying someone else's campaign
   parameters.)
6. **Old archive** — deletes rows Expired more than `LOP_EXPIRED_PURGE_DAYS`
   (90) ago. Closed calls are worth keeping for a while, not forever.

### New settings (`backend/.env`, all optional)

| setting | default | effect |
|---|---|---|
| `LOP_REQUIRE_USABLE_LINK` | `true` | refuse to store a row with no link to the call |
| `LOP_STRICT_OPPORTUNITY_GATE` | `true` | apply the opportunity gate at scrape time |
| `LOP_EXPIRED_PURGE_DAYS` | `90` | delete rows expired longer ago than this; `0` keeps everything |

### Verified before shipping

Built a seeded SQLite database with representative rows and ran the real code:

- **Ingest** — one batch of 8 rows through the real `_ingest`:
  `(saved 4, expired 1, dupes 0, spam 1, rejected 3)`. Future-dated RFP → Active.
  Past-dated grant → **Expired, archived not lost**. Undated-unknown → Active.
  Rolling → Active. News page, team page and linkless row → rejected.
- **The actual bug** — `generic_listing.parse_listing` on synthetic HTML:
  a call with no date now yields `assume_active=False` (was `True`, i.e.
  immortal); one whose text says "rolling basis, no fixed deadline" yields
  `True`; a dated one captures the date.
- **Cleanup script** — dry run reported 7 of 12 rows; `--apply` removed exactly
  those 7. Survivors: the two real calls, the past-dated grant **archived rather
  than deleted**, the best copy of the duplicate trio (clean URL, dated), and the
  undated rolling row — which was correctly **held back** from retirement
  because its source had no successful scrape in the window. That guard matters:
  without it a source that is merely *down* deletes its whole catalogue.
- **Gate** — 18 hand-built cases covering every rule, 18/18 as expected.
- `pyflakes` clean on every changed file.

**Not verified: the effect on your real 176 MB database.** The dry run is
non-destructive and prints per-source counts — read it before running `--apply`.

---

## 2026-08-25 — UN Partner Portal scraper rebuilt; registry precedence bug fixed

### The problem, as the code itself recorded it

`backend/data/debug/un_partner_portal_page1.html` is the page the last run
actually received. It settles what was wrong, and it is not what it looked like:

| Evidence in that file | What it means |
|---|---|
| Header reads "Nitin Editor - Swasti", with Dashboard / Your Applications / Profile and a notification count | The session **was** signed in. The Chrome-profile mirror works. Login was never the problem. |
| The results table rendered its headers — Project Title, Country, Sector & Area of Specialization, UN Agency, Application Deadline, Estimated Start Date | The right page loaded. The route was correct. |
| A single cell reading "No data available", pagination reading "0–0 of 0" | The table received no records. |
| A Toastify error banner reading **"Unable to load data"** | The XHR that fills the table **failed**. |
| Zero `<a>` tags in the whole document | There was never anything for an HTML link-scraper to find. |

UN Partner Portal is a React single-page app. Its HTML contains no
opportunities at any time — the table is filled by a call to the portal's own
API after the page loads. The generic HTML scraper could not see any of this.
All it knew was "0 links that look like funding", so it reported an empty page
and the run completed successfully with nothing in it. **A source silently
returning nothing while reporting success is worse than one that errors**, and
that is the class of failure this change is aimed at.

### Changes

**1. New — `backend/app/scrapers/unpp.py`**

A bespoke scraper that talks to the portal's API instead of scraping rendered
HTML. Three things it does that the generic path structurally cannot:

- **Never guesses the endpoint.** It attaches a response listener, lets the app
  make its own request, and takes whichever URL returns a paginated payload —
  filters and all. Only if the app's own call failed does it probe a short
  candidate list (`/api/projects/open/` first — the portal's own filter controls
  are id'd `table_filter_select_projects_list_open_*`, so "projects list, open"
  is the table's own name for itself), and every candidate is *verified* before
  use rather than assumed.
- **Says what failed.** Any `/api/` response with a 4xx/5xx status is logged with
  its method, URL, status and body. "Unable to load data" cost a debugging
  session; `GET /api/projects/open/ -> 403 {"detail": ...}` does not.
- **Refuses to scrape the wrong thing.** Sign-in is *verified*, not assumed
  (URL check plus header chrome). If no session can be established the scraper
  yields nothing and logs why. It deliberately does **not** fall back to
  `/landing/opportunities`, the public teaser — that is a different and much
  shorter list, so an anonymous run does not produce "fewer rows", it produces
  the *wrong* rows, and they look like success in the dashboard.

Other details:
- Requests are issued from inside the authenticated browser via `fetch()`, so
  they carry cookies and the SPA's `Authorization` header exactly as the app
  sends them. Reproducing that by hand with httpx is how a scraper ends up
  reading the signed-out view.
- Field mapping tolerates key-name drift (`deadline_date` /
  `application_deadline_date`, `agency` / `agency_name`, string vs `{name:}` vs
  list). A hard-coded spelling breaks silently the day the other one ships, and
  a silently empty deadline is stored as *permanently open*.
- `organization` is set to the **UN agency running the call** (UNICEF, UNHCR, …),
  not to "UN Partner Portal". The portal is where the call was found, not who is
  funding it.
- `opportunity_url` is `…/landing/opportunities/{id}/` — the same target
  `services/links.py` already rewrites UNPP machine endpoints into, and the only
  version a colleague without a UNPP account can open. The signed-in
  `/cfei/open/{id}/overview` URL is recorded in the summary.
- A DOM fallback parser reads the rendered MUI table if the API path yields
  nothing. It locates columns by their **header text**, never by position or CSS
  class — the class names are emotion-generated hashes that change on every
  frontend deploy.
- Playwright runs in a worker thread (same pattern as `adb.py`): its sync API
  cannot be driven from the event loop, and on Windows uvicorn's selector loop
  cannot spawn the browser subprocess at all.

**2. Fixed — `backend/app/scrapers/generic_listing.py` (`_build`)**

`register()` is a plain `SCRAPER_REGISTRY[cls.name] = cls`, and
`scrapers/__init__.py` imports the config-driven sources **last** — with a
comment claiming that made bespoke scrapers win. It did the exact opposite: a
`sources.json` entry silently **overwrote** any hand-written scraper of the same
name.

UN Partner Portal was precisely that case. Without this fix, adding `unpp.py`
would have changed nothing at all and the generic parser would have kept running.

`_build()` now skips a name that is already registered and logs which module
claimed it. The comment in `__init__.py` was corrected to describe reality.

*This affects every future bespoke scraper, not just this one.*

**3. Changed — `backend/app/scrapers/sources.json`**

Removed the `un_partner_portal` entry (72 entries remain). It is now a module.
Left in place it would be dead config that reads as if it were live — and if
`unpp.py` ever failed to import, the generic scraper would silently take over
again, which is the same invisible regression in a new costume.

**4. Changed — `backend/app/core/config.py`**

Added `unpp_email`, `unpp_password`, `unpp_headless`, mirroring the existing
`devaid_email` / `devaid_password` pattern.

**5. Changed — `backend/.env` and `backend/.env.example`**

`LOP_UNPP_EMAIL` / `LOP_UNPP_PASSWORD` / `LOP_UNPP_HEADLESS`. The real values
went into `.env` only, which is gitignored; `.env.example` carries blanks.

Authentication order is now: **credentials first, Chrome session as fallback.**
Credentials are the only route that works headless on EC2, where there is no
Chrome profile to borrow. The Chrome route still works locally and still needs
Chrome fully closed.

**6. New — `backend/scripts/check_scraper.py`**

Deep check of one source, complementing `validate_sources.py`:

```
python scripts/check_scraper.py un_partner_portal --pages 2
python scripts/check_scraper.py un_partner_portal --pages 3 --json unpp.json
```

`validate_sources.py` answers *"which of the 85 sources are broken?"* — one page
each, three samples, a verdict column. That is the sweep.

This answers the question you act on: *"is what this ONE source produced
**correct**?"* It walks several pages, prints every field of every row, and
separates the two failures a verdict cannot tell apart — *produced nothing*
(fetch / login / markup) versus *produced rows that are wrong* (titles that are
navigation, links that open a listing instead of the call, deadline strings that
never parse into a date). Logging is INFO by default and not optional: for a
source behind a login, the reason for a zero is only ever in the log.

### How to verify

```powershell
cd E:\lead-opportunity-platform\backend
.venv\Scripts\activate
python scripts\check_scraper.py un_partner_portal --pages 2 --json unpp_check.json
```

Expected: a row count, `links to a specific opportunity` at 100%, real
`deadline` strings, and `VERDICT: LOOKS CORRECT`.

If it fails it will now say *why*, in one of these shapes:

| Log line | Meaning |
|---|---|
| `not signed in, so /cfei/open has nothing to show` | Neither credentials nor Chrome session worked. `backend/data/debug/unpp_signed_out.html` shows what the portal served. |
| `the credentials were submitted but the portal still shows a signed-out page` | Wrong password, expired account, or a CAPTCHA/SSO on the form. See `unpp_login_failed.html`. |
| `the portal's own request failed: GET … -> 403 …` | The session is fine; the API refused. The status and body are the fix. |
| `probe /api/projects/open/ -> 404` | The endpoint moved. The probe list in `unpp.py` names where it looked. |
| `page 1 returned 200 but no recognisable list of records` | The response shape changed; the logged keys say what it is now. |

### Verified before shipping

Run in a clean checkout of this backend (the live site is not reachable from
where these edits were made, so everything short of the network was tested):

- 85 scrapers register; `un_partner_portal` resolves to `app.scrapers.unpp`.
- **Precedence fix proven**: re-adding a conflicting `un_partner_portal` entry to
  `sources.json` and re-importing still resolves to `app.scrapers.unpp`. Before
  the fix, the config entry won.
- DOM fallback returns **0** rows on the real failing page (correct — it holds no
  records) and parses a synthetic UNPP table correctly by header text.
- API mapping produces correct rows from a realistic payload, including the
  alternate key spellings.
- Produced URLs pass the project's own checks: `is_usable_link` → True,
  `link_kind` → `deep`, `is_furniture` → False, and `canonical_link` maps the
  UNPP machine endpoint onto the identical URL the scraper emits.
- Deadlines parse: `2026-09-12` and `12 Sep 2026` → `2026-09-12`.
- `py_compile` + `pyflakes` clean on every changed file.

### Live run — confirmed working

First run on the scraping machine, `check_scraper.py un_partner_portal --pages 2`:

```
pages 2 · items 61 · deep 61/61 (100%) · dated 61/61 (100%)
deadlines that parse 61/61 · junk 0 · dup_url 0 · 16.7s
VERDICT: LOOKS CORRECT
```

61 records is the complete open-CFEI list (page 2 returned 11 and the walk
stopped on the API's own count). Every row carries a title, an agency, a
country, a sector, a parseable deadline and a unique deep link. Agencies:
UNICEF 23, UNHCR 18, WFP 16, UNFPA 3, UN Secretariat 1. Deadlines run
2026-05-30 → 2026-09-30.

The generic scraper's score on the same source was **0**.

---

## 2026-08-25 — UNPP follow-up: country codes and agency enum keys

Two defects the live run exposed. Both were producing rows that *looked* right
in the check output and would have been degraded on the way into the database.

**1. Country codes were being thrown away entirely.**

The portal returns ISO alpha-2 codes — `SD`, `RW`, `IN`, `UA`. `geography.py`
is a deliberate whitelist of country *names*, so `canonical_country("SD")`
returns `""`. Every one of the 61 rows would have lost **both** its country and
its region (region is derived from the country, so one loss causes the other).
15 distinct countries, all silently dropped.

Fixed with an ISO alpha-2 → name table in `unpp.py`, resolved before the row is
emitted. `SD` → `Sudan` → region `Africa`.

The mapping deliberately does **not** go into `geography.py`. Adding two-letter
aliases to that global table would make every scraper treat a bare `IT`, `IN`,
`IS`, `AT`, `BE`, `NO`, `SO`, `ME` or `OR` found anywhere on any page as a
country — and stopping exactly that class of junk is the reason that whitelist
exists (its own docstring lists the 138 non-countries it was built to reject).
Inside this scraper the field is *known* to be an ISO code, so the mapping is
safe there and only there.

All 238 entries are checked against `canonical_country()` at import; a
`_check_iso_table()` warning fires if `geography.py` ever renames one, instead
of silently dropping that country's rows again.

**2. `UN_SECRETARIAT` was being stored as an organisation name.**

The API returns an enum key for multi-word agencies. `_agency_name()` converts
`UN_SECRETARIAT` → `UN Secretariat` and `UN_WOMEN` → `UN Women`, and touches
only values containing an underscore — so `UNICEF` is never lower-cased into
`Unicef`.

Both fixes apply to the API path and the DOM fallback.

**Re-run confirms it.** Same 61 rows, now carrying Mexico, Ecuador, Kyrgyzstan,
Nepal, Iraq, Kenya, Gambia, Senegal, Uzbekistan, Mali, Argentina, Ukraine,
Burundi, India — each of which resolves to a region through `geography.py`.
Verdict `LOOKS CORRECT`, 15.4s.

**3. Endpoint confirmed and recorded.** Discovery found

```
GET https://www.unpartnerportal.org/api/projects/open/?page=N&page_size=50
    -> {"count": 61, "results": [...]}
    fields: id · title · agency · country_code · specializations
            · deadline_date · start_date
```

which is the first entry in `CANDIDATE_ENDPOINTS` — the guess taken from the
portal's own `table_filter_select_projects_list_open_*` DOM ids was right. That
is now documented as fact in `unpp.py`. The probe list is deliberately **kept**
rather than replaced with a hard-coded path: discovery still runs first, so if
the portal moves the endpoint the scraper follows it instead of breaking.

Auth used the saved Chrome session (`already signed in via the saved browser
session`) — the credential path was not needed, which is the intended order.

**4. `stale_page_streak_override = 0`.**

The manager's default stops a source after 3 consecutive pages that saved
nothing new. Harmless today at 2 pages; once this list grows past three pages of
already-stored calls it would abandon the source *before* reaching pages holding
calls the database has never seen. Walking to the end costs two requests. This
is the same setting World Bank and UNDP Procurement already carry, and the
failure it prevents is one that only shows up months later.

### Two things that are correct but worth knowing

- **21 of 61 titles are not in English** (Spanish and French — the CFEIs are
  written by country offices in the local language). The dashboard's
  `english_only` filter defaults to **on**, so those rows are scraped, stored,
  and then hidden. That is the filter working as designed, not a scraper fault,
  but it means a third of this source is invisible in the default view.
- **One row's deadline (2026-05-30) is already past.** The portal itself still
  lists it under "open"; the pipeline will archive it on save. Correct behaviour
  on both sides.

---

## 2026-08-25 — Static audit of all 85 registered sources

No code changed. Findings, ordered by how much they cost.

### 1. Not one `sources.json` entry uses the selectors the code provides

All 72 config entries carry only `name` / `display_name` / `url` / `website`
(plus `page_url` on World Bank and `stale_page_streak` on two).

**Zero** use `item_selector`, `title_selector` or `deadline_selector`.

`generic_listing.py` documents all three at length, and its own comment says:

> "Every one of UNDP Procurement, World Bank, UN Partner Portal and ADB came out
> with 0% deadlines for exactly this reason … Point `deadline_selector` at the
> cell in `sources.json` and the date is read directly."

That mechanism was built and then never configured for a single source. Every
one of the 72 is running the fully heuristic path: harvest every `<a>` on the
page, then filter by blocklist and funding vocabulary. That is why the guards in
that file keep having to grow — "Skip to main content" under Pfizer Foundation,
Clean Air Fund's three navigation entries. Each new site brings its own
vocabulary of chrome, and a blocklist can only ever catch junk it has already
met.

**Highest-value next step**, and it costs nothing to try: run
`validate_sources.py --dump-html <dir>` (the flag already exists), then write
real `item_selector` / `title_selector` / `deadline_selector` values from the
captured markup for the ~10 sources that matter most. A source with selectors
stops depending on heuristics entirely.

### 2. ~23 entries point at a URL that structurally cannot list open calls

Not markup faults — wrong target pages. No parser fix reaches these.

**Site-search URLs (6)** — a WordPress `?s=` search returns blog posts about
grants, not a grants listing:
Charity Excellence Framework, Goldman Sachs Foundation, Doen Foundation,
Silicon Valley Community Foundation, JPIAMR, CEPI.

**Homepage roots (10)** — the scraper harvests the whole homepage:
Triple Funds, Helmsley Charitable Trust, Woodcock Foundation,
Grantmakers Community Project, Fidelity Charitable, SDG Impact Finance
Initiative, J-PAL, Charity Excellence Framework, JPIAMR, UNDP Procurement.

**Awarded/past grants, not open calls (9)** — these list money already given.
Anything scraped from them is an opportunity nobody can apply to:
Clean Air Fund (`/our-grants/`), Rockefeller Foundation (`/our-grants/`),
Gates Foundation (`/committed-grants`), Laudes Foundation (`/grants-database/`),
CJRF (`/our-grants/`), Tawingo Fund (`/grantees`),
Audacious Project (`/grantees`), The Agency Fund (`/past-calls`),
Women Strong International (`/grantmaking/`).

`source_coverage_report.txt` already flags 11 more where the scraper's URL
disagrees with the research sheet's.

### 3. Pagination is configured on exactly one source

Only World Bank has a `page_url` template. `generic_listing.next_page()` says
plainly that the inherited detection "found nothing on virtually all of them —
every source stopped at page 1". The auto-detection covers `?page=N` and
`/page/N/`; anything else needs a template. So most sources are returning page 1
only — which for a busy funder board is a fraction of what is there.

### 4. Two sources default to a non-English page

- AFD → `https://www.afd.fr/fr/appels-a-projets/liste?` (French)
- Nippon Foundation → `/en/grant` (fine, noted for completeness)

The dashboard's `english_only` filter defaults to **on**, so non-Latin and
non-English rows are scraped, stored, and then hidden — work done twice for
nothing. `page_url` can carry the English URL (its docstring says exactly this).

### 5. `stale_page_streak` is set on two sources, defaults to 3 elsewhere

For a large archive, three consecutive pages of already-seen rows ends the crawl
before it reaches pages holding listings the database has never seen. World Bank
and UNDP Procurement are set to `0` (walk everything). Any other source with a
deep archive has the same exposure.

### Bespoke scrapers

`adb.py`, `developmentaid.py`, `devnet.py`, `bond.py`, `fundsforngos.py`,
`grantwatch.py`, `ngobox.py`, `phf.py`, `indevjobs.py`, `funders_misc.py` were
read, not run. Each carries a specific parser and a documented failure history;
nothing in them looks structurally wrong the way the config entries above do.
They need a live run to judge, which is what `check_scraper.py` is for.

---
