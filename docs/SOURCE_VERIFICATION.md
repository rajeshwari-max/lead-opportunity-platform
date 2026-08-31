# The eleven priority sources: what is proven, and what is not

This is the register the verification work is measured against. It has two
halves, and keeping them apart is the point of the document:

* **Established by inspection and tests** — checked in this repository, failing
  in CI when it regresses. True right now.
* **Unproven until a live run** — needs the source itself, so it can only be
  established on a machine that can reach the internet. Every one of these has
  the exact command beside it.

Nothing in the second column is filled in from a guess, and no coverage figure
appears anywhere in this repository that was not divided by a
**source-reported total**.

---

## 0. Status

The first all-eleven run happened on **2026-08-30**: 3 passed, 8 failed, 2
blocking, and no source proved coverage. §2 has the numbers, §2b the five real
defects, §3 the ones found by reading the code before the run.

Two of the eight failures turned out to be wrong thresholds rather than broken
scrapers, which is its own finding: a bar set in the wrong place fails correct
code and sends somebody to fix something that is not broken. Both are corrected
and both now have tests.

Coverage is still `unproven` for all eleven, and will stay that way until a run
supplies `--official-total` per source. That is the honest state rather than a
gap in the tooling — nothing here divides our count by our count.

---

## 1. Run them

```bash
cd ~/Deployment/lead-opportunity-platform/backend
source .venv/bin/activate            # PowerShell: .\.venv\Scripts\Activate.ps1

pgrep -fc "chrome|chromium" || echo 0        # baseline BEFORE anything

python scripts/verify_source.py --all-priority --pages 3 --json verify.json

pgrep -fc "chrome|chromium" || echo 0        # must return to the baseline
```

Exit code 0 means every source met its contract; non-zero names the ones that
did not. Run them one at a time to supply an official total:

```bash
python scripts/verify_source.py un_partner_portal --pages 5 --official-total 812
```

**Do not run a full all-source production scrape until these eleven pass.**
`verify_source.py` writes nothing, so it is safe to run against production.

---

## 2. The register — measured 2026-08-30

First all-eleven run: **3 passed, 8 failed, 2 blocking, 11 could not prove
coverage.** Every number below is from that run.

| Source | | Pages | Extracted | Unique | Stored | Deep links | Deadlines parse | Org named | What failed |
|---|---|---|---|---|---|---|---|---|---|
| DevelopmentAid | **PASS** | 3 | 36 | 35 | 35 | 100% | 100% | 100% | — |
| UN Partner Portal | **PASS** | 2 | 70 | 70 | 70 | 100% | 100% | 100% | — |
| NGOBOX | **PASS** | 1 | 17 | 17 | 17 | 100% | 100% | 100% | — |
| World Bank | ~~FAIL~~ | 3 | 87 | 86 | 83 | 100% | 100% | 100% | 1 duplicate vs a 1% bar — **the bar was wrong**, now fixed |
| DevNetJobsIndia | ~~FAIL~~ | 1 | 32 | 32 | 21 | 65.6% | 100% | 100% | **measured wrong**, now fixed — but it loses 34% of rows |
| ADB Tenders | FAIL | 3 | 36 | **12** | 12 | 100% | 100% | 100% | pagination never advanced |
| Bond UK | FAIL | 1 | 448 | 354 | 271 | **50.9%** | 100% | 100% | half the rows open the index; 94 in-page repeats |
| UNDP Procurement | FAIL | 1 | 568 | 568 | 562 | 100% | 50% | 100% | **0.4%** of rows carry a deadline at all |
| FundsForNGOs | FAIL | 3 | 150 | 150 | 134 | 100% | 100% | **2.7%** | the funder is almost never named |
| Clean Air Fund | **BLOCKED** | 0 | 0 | 0 | 0 | — | — | — | fetched nothing in 3.7s |
| Devex | **BLOCKED** | 0 | 0 | 0 | 0 | — | — | — | paywalled, as expected |

Coverage is `unproven` for all eleven, because no run supplied
`--official-total`. That is the honest state, not a gap in the tooling — see §5.

### The two that were my measurement's fault, not the scraper's

**World Bank** failed on one repeated row in 87 — 1.1% against a 1% bar. A
listing whose order shifts between two requests produces exactly that. A
percentage alone is unusable at small sample sizes, so a duplicate finding now
needs to clear both the percentage **and** a floor of 3 rows.

**DevNetJobsIndia** failed at 65.6% deep links against a 100% bar — for
behaving correctly. It deliberately returns an empty link when no `job_id` can
be recovered, so the row is dropped rather than shipped pointing at the index.
Measuring over everything extracted counted rows that never reach a reader.
Deep links are now measured over rows that would be **stored** (100% for
DevNet), and the dropped rows get their own number: `link loss`, 34.4% — which
is still a finding, because those are calls the source published that never
reach the dashboard. Losing a row and shipping a bad one are different problems
and both deserve a number.

### The report itself was wrong in two more ways

Every source read `outcome: unrecorded`, so both BLOCKING findings said "no page
was fetched (outcome: unrecorded)" — precisely the uninformative state the
outcome taxonomy exists to replace. Devex behind a paywall and Clean Air Fund's
URL failing outright are different problems needing different people. The
script now classifies through `scrape_outcome.classify()`.

Every source also read `browsers: not measured`, because `psutil` is not in the
venv — an UNPROVEN on all eleven rows, which is the reading that trains people
to skip the section. It now falls back to `tasklist` / `ps`.

A **synthetic** fixture is built from the field names and selectors the
parser's own code documents. It catches a regression in our parser; it cannot
catch drift at the source, because it was derived from the parser rather than
from the site. `tests/fixtures/README.md` says which is which per file and how
to replace each one with a real capture. Treating the two as equivalent is how
a suite stays green while every scrape returns nothing.

---

## 2b. The five real defects the run exposed

Ordered by how much bad data each puts in front of a reader.

### UNDP Procurement: 562 rows a night, none of them with a deadline

568 rows off one page; **two** carried a deadline string, of which one parsed.
This is a procurement notice board — essentially every notice has a closing
date. The generic heuristic reads the block around each anchor, and UNDP puts
the deadline in its own table cell, so it is never seen. Those 562 rows would
be stored in the UNKNOWN deadline state, which never expires on its own; 1,274
are already there.

The manifest's open question — *"whether the generic scraper is reliable enough
or needs a documented API scraper"* — is now answered, and the answer is no.
This needs a bespoke scraper reading the notice table by column. A wider
heuristic will not do it.

### ADB Tenders: pagination never advanced

Three pages, 36 rows, **12 distinct** — the same twelve, three times.
`searchstax[page]=N` is a parameter the widget accepts and ignores on a fresh
navigation: the World Bank `os={offset}` failure in a different source, and the
shape that looks most like success. Only 24 rows have ever been stored under
this source, which is what one page of it looks like.

There *was* a guard for this. It compared `page.inner_text("body")[:4000]` — and
the first four thousand characters of that page are header, nav and facet
counts, chrome that changes between navigations while the result rows do not.
So it passed, three times running. It now compares a fingerprint of the result
rows themselves, and that fingerprint is a **pure function of HTML**, which is
the other half of the fix: nothing could exercise the old guard without a
browser, so nothing did. `tests/test_adb_pagination.py`.

Stopping is not the same as paging. If page 2 still repeats page 1, the fix is
to click the pager in the live page rather than re-navigate — the same reasoning
that already made `_select_status_facet` tick the box instead of building a URL.

### Bond UK: half of what it ships opens the index

50.9% deep links, against the 90% its contract sets — the defect predicted from
the code last round, now measured. And 448 rows extracted from **one** page of
which 354 were distinct: 94 repeats with no pagination involved, so the parser
is reading some cards twice (most likely the rendered `article` path and the
headings fallback both firing).

### FundsForNGOs: the funder is named on 2.7% of rows

Four rows out of 150. The WordPress record has no funder field, so the name has
to come out of prose via `extract_organization()`, and on this source's phrasing
it almost never does. This source is **45% of the database**, so the
Organization column and its filter are effectively empty for nearly half of
everything stored.

### Clean Air Fund: fetches nothing

0 pages in 3.7 seconds — too fast for a timeout, so the request failed outright
rather than being slow or blocked. No login is involved, so this is a URL, a
redirect or a status code, not access. Most likely the page moved.

```bash
python scripts/check_scraper.py clean_air_fund --pages 1     # read status + final URL
python scripts/find_listing_url.py clean_air_fund            # if it moved
```

---

## 3. Defects found by inspection this round

Each of these is now pinned by a test, so it fails rather than drifting back.

### 3.1 `production_enabled` was never read — Devex ran anyway

`SourceContract.production_enabled` and `disabled_sources()` were written in the
previous round, with a docstring explaining exactly when to hold a source out of
production. A grep across the whole backend found **no caller outside the module
that defines them**.

So Devex — whose manifest says in as many words *"Disabled until the access
question is answered"* — ran on every scheduled scrape, fetched zero pages
exactly as before, and recorded `completed` exactly as before. A disable switch
nobody reads is worse than none, because people believe it.

Now: an all-source run drops disabled sources and logs the reason; naming a
source explicitly still runs it, so an operator testing a fix is not blocked by
the flag; and the health page reports `disabled` **before** it reports stale or
never-produced, so nobody is sent to debug a scraper that is off on purpose.
`tests/test_production_gate.py`.

### 3.2 Bond UK and Clean Air Fund had no scope contract at all

Neither appeared in `MANIFESTS`, so `contract_for()` returned the placeholder,
and `record_is_in_scope()` was a **no-op** for both — a scope check that looked
exactly like a working one. Both now have contracts.

Clean Air Fund's matters most: `/what-we-do/our-grants/` is largely a portfolio
of money **already awarded**, which nobody can apply to. Its contract excludes
`contract_award` and `project`, and it is deliberately **not** marked curated —
marking it so would exempt its rows from the funding-vocabulary gate and let the
awarded portfolio through onto the dashboard.

### 3.3 Bond UK ships rows that link back to the index

A card with no apply link is stored as `start_url + "#post-NNN"`.
`is_usable_link()` accepts that, so the row is **not** dropped — but
`link_kind()` calls it a listing, and the reader lands on the index they came
from. Same class of defect as DevNetJobsIndia's 86 index-linked rows, in live
code, unfixed.

Not changed here, because how often it happens decides what the fix should be,
and that needs a live run. Its contract holds Bond to **90% deep links**, which
is the measurement that answers it. Pinned by
`test_a_bond_card_with_no_apply_link_falls_back_to_the_index_anchor`.

### 3.4 FundsForNGOs silently drops all-numeric deadlines

`_DEADLINE` requires at least three word characters in the month position:

```
deadline\s*:?\s*([0-3]?\d[-/ ]\w{3,9}[-/ ]\d{2,4})
```

It matches `15-Mar-2027` and `9 January 2027`. It matches **nothing** in
`09-01-2027` or `09/01/2027`. A post writing its date numerically is stored with
no deadline at all: the row never expires on its own and sits in the UNKNOWN
deadline state until `audit_deadlines()` retires it. This source is 45% of the
database.

Deliberately **not** fixed, and the order matters: widening the pattern to
accept `09-01-2027` immediately forces the question of whether that is 9 January
or 1 September — which is exactly the convention the manifest records as never
established, across 48,350 stored rows. Settle it first:

```bash
python scripts/deadline_convention_audit.py --source FundsForNGOs
```

then widen the pattern. Doing it the other way round decides the meaning of
those dates by accident.

### 3.5 DevelopmentAid stores the abbreviated donor name

A record carrying both `abbreviatedDonorNames: "EU"` and
`donorNames: "European Commission"` stores **"EU"** — `_pick` returns the first
key matching the needle and dicts iterate in insertion order. The code names
`abbreviatedDonorNames` deliberately, so this is intended, not accidental.

It is still worth a decision: the Organization column and its filter then hold
"EU" and "European Commission" as two different funders, which fragments both.
That is a product call about how the column reads, not a bug fix, so it is
recorded and pinned as observed behaviour rather than changed.

---

## 4. What each source is held to

Thresholds live in `app/services/verification.py`, one contract per source, with
the reason on the line. Three of them are worth stating here because they look
wrong until you know why.

**World Bank coverage is not gated.** The API's total is 416,361 — the entire
historical archive — and the scraper walks a bounded 60-page window of the
newest notices on purpose. Coverage against that total is ~1.4% *by design*, and
a threshold would fail a correct scraper every night. Same for FundsForNGOs,
whose WordPress total counts every article ever published.

**UN Partner Portal coverage *is* gated, at 98%.** Its API publishes an exact
count of **open** calls and the walk is meant to reach all of them. Falling
short is a pagination defect, not a design choice. It is the one source where
the total means what a coverage figure needs it to mean.

**DevelopmentAid is allowed 60% in-run duplicates.** Its walk partitions the
catalogue into overlapping searches, so the same tender legitimately arrives
several times; deduplication is the mechanism. A 5% bar would fail every correct
run, and only the unique count means anything for this source.

---

## 5. Severities, and why UNPROVEN does not fail a run

| | |
|---|---|
| **BLOCKING** | The run produced nothing trustworthy — no pages, an auth wall, a parser that found nothing. Everything downstream is meaningless, so the report stops there rather than printing "0% of links are deep" next to it. |
| **QUALITY** | Data arrived and misses a stated threshold. Fails the run. |
| **UNPROVEN** | A claim could not be checked. Recorded, and does **not** fail the run. |

UNPROVEN not failing is a deliberate choice. Failing on it would mean every
source without a published total is permanently broken — which is false, and
which trains people to ignore the result. It is reported *beside* the pass
count, never folded into it: `4 of 11 passed, 7 could not prove coverage` is a
sentence with two facts in it, and losing either one is how a report becomes
reassuring instead of true.

---

## 6. Standing constraints

* No credentials, cookies, `.env`, session files or browser profiles in the
  repository, in logs, or in any output of these tools.
* No CAPTCHA, Cloudflare or access-control is bypassed. If a source refuses
  automated access, the answer is an official API, a licensed feed, or dropping
  the source — never a workaround.
* No opportunity is deleted. Expired and invalid rows are archived; a wrongly
  archived row comes back and a deleted one does not.
* No scraper rewrites itself in production.
* No all-source production scrape until these eleven pass their contracts.
