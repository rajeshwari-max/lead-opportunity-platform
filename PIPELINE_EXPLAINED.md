# The Processing Pipeline, and How 73 Sites Run Without Code

*Two questions answered from the actual source, with the real numbers.*

---

# PART 1 — What happens to a row between scraping and the database

Everything below lives in one function: `_ingest()` in
`backend/app/services/scraper_manager.py`.

It runs **once per page**, not once per run. `crawl()` yields a page of rows,
`_ingest()` processes and saves them, then the next page is fetched. That's why
a source killed halfway still leaves everything it had already collected.

```
RawOpportunity (loose text from the page)
        │
   1.  parse the deadline
   2.  reject sentinel dates
   3.  decide Active / Expired / Ongoing
   4.  classify the category
   5.  classify the verticals            (multi-label)
   6.  classify work type + study type
   7.  optional vertical filter
   8.  normalise country + region
   9.  recover the organisation
   10. clean / recover the amount
   11. reject spam
   12. validate + rewrite the link
   13. fingerprint
   14. deduplicate
   15. insert
        ▼
Opportunity row
```

## Step 1–3 · The deadline, and what "expired" means

```python
deadline = self.deadline_parser.parse(raw.deadline_raw, dayfirst=raw.dayfirst)
if is_sentinel(deadline):
    deadline = None
ongoing = deadline is None and (
    self.deadline_parser.is_ongoing(raw.deadline_raw) or raw.assume_active
)
is_expired = not ongoing and not self.deadline_parser.is_active(deadline, today)
```

**Sentinels.** DevelopmentAid writes `9999-12-31` to mean "no closing date".
Parsed literally, that produced deadlines in the year 9999 and a countdown of
2.9 million days. `is_sentinel()` also rejects `0001-01-01`, `1970-01-01`,
`1900-01-01` and anything more than 3 years out.

**Three states, not two.** A row is Active, Expired, or *Ongoing* (no deadline,
displayed as "Ongoing"). Ongoing is not expired — a rolling call with no closing
date is a real lead.

**Expired rows are kept, not dropped** (`keep_expired = True`). Discarding them
threw away thousands of rows per run and made "found" wildly exceed "saved".
They're stored with `status=Expired`; the dashboard filters to Active by
default, so this changes what is *archived*, not what is *shown*.

## Step 4 · Category — one label

`services/classification.py`. Weighted keyword scoring:

```python
TITLE_WEIGHT = 3      # a title word was chosen deliberately
BODY_WEIGHT  = 1      # a body word may be incidental
HINT_WEIGHT  = 2      # what the source page claims to be
```

The source's hint ("this is our grants page") is worth 2 — influential but
**never absolute**, because a grants site does publish RFPs. Highest score wins;
ties break by declaration order, which is why `RFP` beats `Grant` on a title
containing both.

Recognises the full UN procurement vocabulary: RFP, RFQ, EOI, TOR, IC, LTA,
RFI, RFEI, SSSA → **RFP**; ITB, ICB, NCB, prequalification → **Tender**.

## Step 5 · Verticals — many labels

`services/verticals.py`. **436 compiled regexes** across six verticals:

| Vertical | Patterns |
|---|---|
| Innovative Finance | 97 |
| Climate/Sustainability(ESG) | 84 |
| Worker Wellbeing | 82 |
| E4C (Evidence for Change) | 63 |
| Livelihood | 60 |
| Health | 50 |

```python
_TITLE_WEIGHT = 3
_BODY_WEIGHT  = 1
_THRESHOLD    = 2

for vertical in VERTICALS:
    score = 0
    for pat in _COMPILED[vertical]:
        if pat.search(title):  score += 3
        if body and pat.search(body): score += 1
        if score >= 2: break
    if score >= 2:
        matched.append(vertical)     # no "best" pick — all of them
```

One title hit (3) clears the threshold alone. One body hit (1) does not. Two
body hits (2) do.

**Multi-label on purpose.** A solar-irrigation-for-smallholder-farmers grant is
genuinely both Climate/Sustainability *and* Livelihood. Forcing one label makes
it invisible to one of the two teams who should see it.

The body searched is not just the summary:

```python
vertical_body = " ".join(filter(None, [raw.summary, raw.vertical, raw.eligibility]))
```

## Step 6 · Work type and study type — the routing axis

- **work_type** → `Research` or `Implementation`. This decides *which team*
  gets the lead, even when both are filed as "RFP".
- **study_type** → Baseline / Endline / Evaluation / Data Collection, only ever
  present on research work.

## Step 7 · Optional vertical filter

If the operator ticked verticals in Scraper Control, rows matching none of them
are dropped *and counted* (`off_vertical`), so the progress panel can explain a
low save count instead of leaving it mysterious.

## Step 8 · Geography — a whitelist, not a cleanup

`services/geography.py`. `country` and `region` were arriving mixed together:
region names in the country column, money fragments (`$10`, `000 to $55`),
title artifacts.

`canonical_country()` is a **whitelist**, not a blacklist:

```python
def canonical_country(value: str) -> str:
    v = (value or "").strip().strip(".,;:-–—()[]").strip()
    if not v or _JUNK.search(v) or _NOT_A_PLACE.search(v):
        return ""
    alias = _COUNTRY_ALIASES.get(v.lower())
    if alias:
        return alias
    return _DISPLAY.get(v.lower(), "")
```

Anything not recognised becomes empty rather than being passed through. Region
is then *derived* from the canonical country, so the two columns can't disagree.

Two bugs worth knowing about, because they're instructive:

- The splitter broke `"Trinidad and Tobago"` into two non-countries.
- An alias cycle (`russia → Russian Federation → russia`) made the backfill
  non-idempotent — it never converged.

## Step 9 · Organisation — recovered from prose

```python
organization = tidy_organization(raw.organization)
if not organization:
    organization = extract_organization(raw.summary, raw.title)
```

Sources that publish a funder field win. FundsForNGOs names the funder only in
prose, so it's read out of the text rather than left blank.

## Step 10 · Amount — cleaned, then recovered

```python
amount = clean_amount(raw.funding_amount)
if not amount:
    amount = extract_amount(raw.summary, raw.title)
```

`clean_amount` strips page furniture; `extract_amount` reads figures stated in
the listing text ("provides up to US$250,000"). The INR conversion in the
dashboard is a *display* feature on top of this — nothing is rewritten in the
database.

## Step 11 · Spam rejection

Public tender boards accept submissions, and some of what is submitted is
advertising.

```python
def is_spam(title, summary=""):
    if _has_phone(t) or is_furniture(t) or _mostly_non_latin(t):
        return True
    return bool(_SPAM_TERMS.search(t) or _SPAM_TERMS.search(summary[:500]))
```

Rejected **before** the database, so junk never reaches the dashboard, the
digests, or the counts.

> **A near-miss worth remembering.** An early version treated *non-Latin script*
> as a spam signal. It flagged 100 rows, of which roughly 66 were genuine UNDP
> and Tunisian grant calls. Reverted before anything was deleted. Cheap
> heuristics can be confidently wrong.

## Step 12 · Link validation and rewriting

```python
opportunity_url = (
    canonical_link(raw.opportunity_url)
    if is_usable_link(raw.opportunity_url, raw.website) else ""
)
```

`is_usable_link` rejects `mailto:`, `javascript:`, bare fragments, relative
slugs with no host, domain roots with no path, and anything identical to the
source's own homepage. **An absent link costs less trust than a wrong one.**

`canonical_link` swaps a machine endpoint for the human page — UN Partner
Portal published `/api/public/export/projects/N/`, which downloads a file
instead of opening the call.

## Step 13–14 · Deduplication — the most important step

```python
def make_unique_id(title, organization, deadline, url) -> str:
    key = "|".join([
        _norm(title),
        _norm(organization),
        deadline.isoformat() if deadline else "",
        _norm(url),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def _norm(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()
```

Then two checks:

```python
exists = db.execute(select(Opportunity.id)
                    .where(Opportunity.unique_id == uid)).scalar_one_or_none()
if uid in batch_uids or exists is not None:
    dupes += 1
    continue
```

`batch_uids` catches duplicates **within the same page** — sites do list the
same call twice. The database check catches duplicates across runs.

**The guarantee comes from the schema, not this code:**

```python
unique_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
```

A `UNIQUE` constraint means a duplicate is *impossible*, not merely unlikely. No
future bug in the application can bypass it. That is what makes the scraper safe
to re-run any number of times — and it's why the demo query
`SELECT unique_id, COUNT(*) ... HAVING COUNT(*) > 1` returns zero rows across
70,000+ records.

Whitespace and case are normalised first, so `"Climate  Fund"` and
`"climate fund"` collapse. The URL is part of the key, which is deliberate: the
same call listed on two different boards is two leads with two links, and the
team may want either.

## What `_ingest` returns, and why

```python
return saved, expired, dupes, spam
```

Four numbers, because **"found 30, saved 0" is unreadable without them.** Those
are three completely different situations:

- `duplicates 30` → the source has nothing new. Normal.
- `expired 30` → all closed calls. Normal.
- `spam 30` → junk. Fine.
- `found 0` → **parsing failed.** A real problem.

Conflating them is what made a working scraper look broken for days.

---

# PART 2 — How 73 sites run with no Python file

## The split

| | Count |
|---|---|
| Scrapers registered | **85** |
| Configured in `sources.json` (no code) | **73** |
| Hand-written Python files | **12** |

So **86% of sources needed no code.**

## Yes, crawling is generic. Parsing is the part that isn't.

This is the distinction that answers the question:

| | What it means | Generic? |
|---|---|---|
| **Crawling** | *Finding* pages — following links, walking pagination | **Yes, completely** |
| **Parsing** | *Understanding* a page — which text is the title, which is the deadline | **Mostly, not always** |

`BaseScraper.crawl()` is written **once** and inherited by all 85 sources. It
handles fetching, retries, rate limiting, pagination, loop detection, pause/stop,
progress reporting and debug capture. No source overrides it except two that
must drive a browser themselves.

Only `parse_listing()` differs — and for 73 sources, even that is shared.

## What a configured source looks like

```json
{
  "name": "world_bank",
  "display_name": "World Bank",
  "url": "https://projects.worldbank.org/en/projects-operations/opportunities?lang=en",
  "page_url": "https://projects.worldbank.org/...&os={offset}",
  "page_size": 20,
  "stale_page_streak": 0
}
```

That's the whole "code". Adding a site is a JSON edit.

## How the classes are created — at import time

`generic_listing.py` reads the JSON and *generates* a class per entry:

```python
def _build():
    for cfg in _load_config():
        cls = type(
            f"Generic_{cfg['name']}",
            (GenericListingScraper,),
            {
                "name": cfg["name"],
                "display_name": cfg.get("display_name", cfg["name"]),
                "website": cfg.get("website") or f"https://{urlparse(url).netloc}",
                "start_url": cfg["url"],
                "config": cfg,
                "prefer_js": bool(cfg.get("requires_js", True)),
                "page_url_template": cfg.get("page_url", ""),
                "render_wait_text": cfg.get("render_wait_text", ""),
                ...
            },
        )
        built.append(register(cls))
```

`register()` adds it to the registry, so it appears in the dashboard's source
list automatically. There is no list of sources anywhere else to keep in sync.

## The generic parser: heuristics, not selectors

This is the interesting part. It does **not** use per-site CSS selectors —
those would need writing per site, which is the thing being avoided. It asks
"does this *look like* an opportunity?"

```python
def _looks_like_opportunity(text, href) -> bool:
    t = clean_title(text)
    if not (_MIN_TITLE <= len(t) <= _MAX_TITLE):     return False
    if t.lower() in _NAV_WORDS or _SECTION_TITLE.match(t): return False
    if _NAV_HREF.search(href or ""):                 return False
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        return False        # SHOUTING is a section header or a button
    return True
```

Six rules, in order:

1. **Strip the chrome first** — `script`, `style`, `nav`, `header`, `footer`,
   `aside` are removed before anything is examined.
2. **Length window.** Too short is a button; too long is a paragraph.
3. **Reject known navigation words** — ~40 of them, plus institutional chrome
   like "Skip to main content", "Procurement Policy", "Projects & Operations".
4. **Reject navigation hrefs** — `/about`, `/privacy`, `/careers`, `/tag/`,
   `/author/`, `wp-`, `#`.
5. **Reject SHOUTING** — >80% uppercase means a header, not a title.
6. **Reject off-site links** — social icons, partner logos, funder badges.

### Fallback for the commonest card layout

```python
if not _looks_like_opportunity(title, href):
    title = clean_title(self._heading_title(a))
```

Many sites put the title in an `<h3>` and make the *link* a separate "Learn
More" or image anchor — so the anchor's own text is useless. It falls back to
the nearest heading in the same block. Gates Grand Challenges lists every open
call exactly this way, and none of them were being seen before this.

### Label cleanup

```python
def clean_title(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"^(title|name|subject|notice)\s*[:\-–]?\s+", "", t, flags=re.I)
    return t.strip(" :-–|")
```

UNDP's board marks up each row as `Title <the actual title>`, so all 588 of its
rows were stored with a leading "Title ".

### The deadline

```python
block = a.find_parent(["article", "li", "div", "tr"]) or a
text = block.get_text(" ", strip=True)[:1200]
m = _DEADLINE.search(text)
```

Walk up to the enclosing block and look for a deadline-shaped date near a
deadline-shaped word ("deadline", "closing date", "apply by", "due", "expires").
Handles `15 Sep 2026`, `Sep 15, 2026`, `2026-09-15` and `15/09/2026`.

### Pagination, two dialects

```python
size = int(self.config.get("page_size") or 10)
nxt = template.format(page=page_number + 1, offset=page_number * size)
```

- `{page}` → 1-based page index
- `{offset}` → 0-based row offset

World Bank uses the second. It was configured as `{page}`, so the crawler asked
for `os=2, 3, 4` — sliding one row at a time through a 20-per-page list. Nine of
every ten results were repeats and the source stopped after 38 rows *while
reporting success*.

When no template is given, `next_page()` tries, in order: `rel="next"`, a link
whose text is the next page number inside a pagination block, an arrow glyph
(`›`, `»`, "Older posts", "Load more"), and finally bumping a number it finds in
the URL itself.

### Optional per-site overrides

Escape hatches, used only when the heuristics aren't enough — still config, not
code:

| Key | Purpose |
|---|---|
| `item_selector` | container for each listing |
| `title_selector` | title/link within the container |
| `requires_js` | render with a browser |
| `render_wait_text` | wait for this text before parsing (XHR results) |
| `page_size` | rows per page, for `{offset}` |
| `stale_page_streak` | `0` = walk every page |

## Why 12 sites still needed real Python

Not because of styling. Because they do something **structurally** different:

| Site | Why |
|---|---|
| **DevelopmentAid** | Login required; Angular SPA; its own multi-section browser walk |
| **ADB Tenders** | Filtered URL is firewalled; results are XHR-only; pagination is by clicking |
| **DevNetJobs** | Pagination is an HTTP **POST** with form state, not a URL |
| **FundsForNGOs** | Amount and eligibility only exist on the detail page |
| **NGOBOX** | Serves stale cached HTML unless the request looks like a real browser |
| **Bond UK** | Listings live inside a JS-rendered widget |
| **GrantWatch** | Different DOM per category |

**The rule:** try the generic path first. Write a parser only when the site does
something the crawler cannot express — a login wall, POST pagination, or content
that only exists after JavaScript runs.

## The trade-off, honestly

| | Config + heuristics | Per-site selectors | LLM extraction |
|---|---|---|---|
| New site setup | Minutes (JSON) | Hours | Zero |
| Survives a redesign | Often | No | Yes |
| Extraction quality | Good, not perfect | Best | Good |
| Cost per page | ~0 | ~0 | API cost × 70,000 |
| Debuggable | Yes | Yes | "The model read it that way" |

The heuristics get titles, links and deadlines reliably. What they get *less*
reliably is amount, eligibility and country — which is exactly why 47% of active
rows carry no vertical, and why **56% of those have no text at all beyond a
title.** That is a collection gap, not a classification gap, and it's the
highest-value thing left to fix.
