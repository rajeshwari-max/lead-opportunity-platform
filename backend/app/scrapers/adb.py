"""ADB tenders — browser-driven, because the filtered URL is firewalled.

Why this is not a sources.json entry
------------------------------------
ADB's tender search is a SearchStax widget. Its state lives in query parameters
shaped like `searchstax[query]=*&searchstax[page]=2`, and requesting those
directly is refused by ADB's WAF:

    /projects/tenders                      -> 200, full page
    /projects/tenders?page=2               -> 200, full page
    /projects/tenders?searchstax[page]=2   -> BLOCKED
    /projects/tenders?searchstax[query]=*  -> BLOCKED

    "Sorry, you have been blocked ... several actions could trigger this
     block including submitting a certain word or phrase, a SQL command or
     malformed data."

Square brackets and `*` in a query string are a stock injection signature. A
real browser gets through carrying cookies, a referer and site reputation; a
fresh automated request does not. Retrying harder, or dressing the request up
to look more browser-like, is working around a security control — so this does
the honest thing instead and drives the page the way a person does: open the
plain URL, let the widget load its own results, then click through pages.

The second reason a browser is required: the served HTML contains **no tenders
at all**. Every link in it is navigation. The listings arrive by XHR after load,
so there is nothing for a plain HTTP parser to read.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from queue import Empty, Queue

from bs4 import BeautifulSoup

from app.core.config import settings
from app.schemas.opportunity import RawOpportunity
from app.services.notice_types import record_type_for
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

log = logging.getLogger("scraper")

LISTING_URL = "https://www.adb.org/projects/tenders"

# The search ADB's own widget produces, confirmed opening in a signed-out
# browser. {page} is substituted per page.
#
# The sort is ds_date_closing DESC — latest closing date first, NOT soonest.
# That is the right way round and it is load-bearing: a tender that is still
# open closes in the future, so open tenders sort to the front and the 37,769
# closed ones sit at the back. It is what makes an unfiltered walk (see
# unfiltered_max_pages) still return live tenders rather than an archive.
# Flipping it to ASC would put the oldest closed tenders first and the crawl
# would spend its whole page budget on records that get discarded on save.
#
# The module docstring above says requesting these parameters is refused. That
# was true of a COLD request — one arriving with no cookies, no referer, and a
# user agent that contradicted its own client hints (fixed in site_auth). It was
# never a rule against the parameters: the same URL opens fine in an ordinary
# browser, signed out.
#
# So the walk below arrives the way a person does — load the plain page first,
# let the site set its cookies, then move to the search carrying that page as
# the referer. Arriving properly is the difference, not disguising the request.
SEARCH_URL = (
    "https://www.adb.org/projects/tenders"
    "?searchstax[query]=*"
    "&searchstax[page]={page}"
    "&searchstax[order]=ds_date_closing%20desc"
)

# The facets, applied IN THE URL rather than by clicking checkboxes.
#
# Supplied by the platform owner on 2026-09-02 from their own signed-out
# browser session, which is the only reason these are here. The comment below
# _select_status_facet says the widget's facet encoding is undocumented and
# that guessing it would silently return all 51,013 records with nothing in the
# output to say the filter had been ignored — that reasoning still stands, and
# is exactly why an OBSERVED encoding is worth more than the clicking path it
# replaces. Clicking survives as the fallback, not the primary.
#
#   or:sm_fct_status:Active    open for bidding — the ~489 records that matter
#   or:ss_fct_group:consulting Consulting Services only
#
# The consulting facet is a SCOPE DECISION, not a technical one: it excludes
# ADB's goods, works and civil-works tenders entirely. Confirmed deliberate on
# 2026-09-02 — the team bids consulting assignments and the rest would be noise
# in the digest. Widen it by setting LOP_ADB_TENDER_FACETS (drop the group
# entry) rather than editing this file; the count in the log is what tells you
# the change took effect.
DEFAULT_FACETS = ("or:ss_fct_group:consulting", "or:sm_fct_status:Active")
# The Solr field that carries the open/closed distinction, checked against the
# rendered rows to prove the facet actually applied.
_STATUS_FACET_FIELD = "sm_fct_status"
# Backstop only, used when the plain listing publishes no count to compare
# against. ADB's facet counts are ~489 Active and ~396 Advance Notice against
# 51,013 total, so anything in the thousands means no filter was applied.
_MAX_PLAUSIBLE_FILTERED = 5_000

# The Status facet, ticked before paging. This is the difference between a
# viable crawl and an unusable one:
#
#     Active      489        <- open for bidding, what a lead platform wants
#     Advance Notice 396
#     Awarded  12,527
#     Closed   37,769
#     Archived    209
#     ------------------
#     total    51,013        -> 4,251 pages at 12 per page
#
# Unfiltered, a run spends hours fetching 50,524 tenders that parse_listing
# discards on the Status line anyway. Ticked, it is 489 records over ~41 pages
# and finishes in a couple of minutes.
#
# The facet's Solr field is sm_fct_status, read off the container class
# "desktop-0sm_fct_status" in a captured page. Rather than guess how the widget
# encodes that in a URL, the walk ticks the box and then reads the URL the site
# itself produces — see _select_status_facet.
STATUS_FACET = "Active"

_PAGE_PARAM = re.compile(r"(searchstax(?:%5B|\[)page(?:%5D|\])=)(\d+)", re.I)


def configured_facets() -> tuple[str, ...]:
    """The facets to apply, from settings, falling back to DEFAULT_FACETS.

    Read at call time rather than import time so the setting can be changed
    without a redeploy of this module's constants.
    """
    raw = getattr(settings, "adb_tender_facets", "") or ""
    chosen = tuple(f.strip() for f in raw.split(",") if f.strip())
    return chosen or DEFAULT_FACETS


def search_url(page: int, facets: tuple[str, ...] | None = None) -> str:
    """The faceted search URL for page `page`.

    Built rather than stored as a template, because the facet list is variable
    length and each entry needs its own indexed parameter:

        &searchstax[facets][0]=or:ss_fct_group:consulting
        &searchstax[facets][1]=or:sm_fct_status:Active

    Pure, so the exact URL this scraper requests can be asserted in a test
    instead of inferred from a live run — the same reasoning that produced
    scripts/devaid_urls.py after DevelopmentAid was found requesting a URL
    nobody had checked.
    """
    url = SEARCH_URL.format(page=page)
    for i, facet in enumerate(configured_facets() if facets is None else facets):
        url += f"&searchstax[facets][{i}]={facet}"
    return url

# The container the results live in. Named once, because the pagination guard
# and the per-page count both have to agree with the parser about what a
# "result" is — and they did not.
RESULT_BLOCK = "div.searchstax-search-result"
# "1 - 12 of 51013" in the pagination bar. The only place the result count is
# published, and what tells the walk when to stop rather than guessing.
_TOTAL = re.compile(r"of\s+([\d,]+)", re.I)

# Each result carries these labels. They are the only stable anchors: class
# names on this widget are generated and change between deploys, whereas the
# labels are the visible content and change only if ADB redesigns the page.
# Read the result rows inside the browser, keyed the same way
# `_results_signature` keys them in Python — link plus first line of text. One
# expression, used both to snapshot before a click and to wait for the change,
# so the wait and the verification cannot disagree about what "the page moved"
# means.
#
# They disagreed before, and it was not a subtle disagreement: the wait compared
# `document.body.innerText.slice(0, 4000)` against a value that had been changed
# to a 16-character sha256 prefix. A 4,000-character text slice is never equal
# to a 16-character hash, so `!==` was true on the predicate's first evaluation
# and `wait_for_function` returned immediately, having waited for nothing.
_ROWS_JS = f"""(() => Array.from(document.querySelectorAll({RESULT_BLOCK!r})).map(b => {{
    const a = b.querySelector('a[href]');
    return (a ? a.getAttribute('href').split('?')[0] : '') + '|'
         + (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
}}).join('\\n'))"""
# Rows present AND different from the snapshot. Requiring rows to be present
# matters: the widget empties the container while it fetches, and an empty
# container is "different" without being the next page.
_ROWS_CHANGED_JS = f"prev => {{ const now = {_ROWS_JS}(); return !!now && now !== prev; }}"

# The Next control, as ADB actually renders it. Read off a captured page
# (logs/adb_no_results.html, kept as tests/fixtures/adb_pagination_bar.html)
# rather than guessed, because every guess here was wrong in a way that would
# have shipped looking correct:
#
#     <a class="searchstax-pagination-next " tabindex="0"
#        id="searchstax-pagination-next">Next &gt;</a>
#
#   * There are no numbered page buttons. Previous and Next are the whole bar,
#     so "click the button labelled 3" was never going to match anything.
#   * The label is "Next >", not "Next". An exact-match on "Next" finds nothing.
#   * It is an <a> with no href and a tabindex — a JS handler, not a link, so
#     a[rel=next] and friends never applied either.
#
# The id is stable and unambiguous, so it leads. The class and the label follow
# it only as insurance against an ADB redeploy that renames the id.
_NEXT_SELECTORS = (
    "#searchstax-pagination-next",
    "a.searchstax-pagination-next",
    '[class*="pagination"] a[id*="next" i]',
)
# How this widget says "there is no next page". Not the disabled attribute and
# not aria-disabled — both absent here — but a class plus inline
# pointer-events:none. Every stock disabled-check reads this control as live,
# which would mean clicking a dead anchor on the last page and waiting the full
# 30s for rows that were never going to change.
_DISABLED_CLASS = "disabled"

# Last-resort control finder, for a redeploy that renames the id. Prefix match,
# not equality — the label is "Next >".
_FIND_BY_LABEL_JS = """
(wanted) => {
  const bars = Array.from(document.querySelectorAll(
      '[class*="pagination"], [class*="pager"], nav[aria-label*="agination"]'));
  const scope = bars.length ? bars : [document.body];
  const want = wanted.trim().toLowerCase();
  for (const bar of scope) {
    for (const el of bar.querySelectorAll('a, button, [role="button"]')) {
      if (el.classList.contains('disabled')) continue;
      if (getComputedStyle(el).pointerEvents === 'none') continue;
      const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (t.toLowerCase().startsWith(want)) return el;
    }
  }
  return null;
}
"""


class PagingMode:
    """Which mechanism moves this widget forward — and it only decides once.

    Extracted from the walk so the fallback rule can be tested without a
    browser, because "the walk never reached its own fallback" is precisely the
    bug that shipped, and nothing could see it from outside Playwright.

    The latch is the important part. Trying the URL again after it has been
    shown not to page would reset the widget to page 1 on every iteration, so
    the walk would re-yield page 1 forever while looking like it was making
    progress. Once CLICK, always CLICK.
    """

    UNTESTED, URL, CLICK = "untested", "url", "click"

    def __init__(self) -> None:
        self.mode = self.UNTESTED

    def should_try_url(self) -> bool:
        return self.mode in (self.UNTESTED, self.URL)

    def url_result(self, attempted: bool, changed: bool) -> str:
        """What the walk should do next: "continue", "click" or "end".

        `attempted` is whether the navigation itself succeeded — a blocked or
        failed load is not evidence that the parameter does not page, so it
        falls through to clicking without condemning the URL mechanism.
        """
        if changed:
            self.mode = self.URL
            return "continue"
        if self.mode == self.URL and attempted:
            # It paged on earlier iterations and has stopped changing now, so
            # this really is the end of the list rather than a broken parameter.
            return "end"
        self.mode = self.CLICK
        return "click"


_STATUS = re.compile(r"Status:\s*(\w+)", re.I)
_DEADLINE = re.compile(r"Deadline:\s*([0-9]{1,2}\s+\w{3,9}\s+[0-9]{4})", re.I)
_COUNTRY = re.compile(r"Country/Economy:\s*([^|\n]+)", re.I)
_SECTOR = re.compile(r"Sector:\s*([^|\n]+)", re.I)
_NOTICE = re.compile(r"Notice Type:\s*([^\n|]+)", re.I)
_POSTED = re.compile(r"Posting Date:\s*([0-9]{1,2}\s+\w{3,9}\s+[0-9]{4})", re.I)


@register
class AdbTendersScraper(BaseScraper):
    name = "adb_tenders"
    display_name = "ADB Tenders"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    website = "https://www.adb.org"
    start_url = LISTING_URL
    requires_js = True
    enrich_details = False        # everything needed is on the listing row

    # Safety net: ~489 Active tenders at 12/page is ~41 pages. The cap is
    # generous but finite, so a pagination control that never disables itself
    # cannot spin forever.
    max_pages = 60
    # Used only when the Status facet failed to apply. Higher on purpose: with
    # the closing-date-descending sort, every still-open tender sorts to the
    # front, and ADB's own facet counts (~489 Active + ~396 Advance Notice)
    # put that at ~74 pages. A 60-page budget would stop short of it and drop
    # live tenders while reporting success.
    unfiltered_max_pages = 110

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """Pull rows out of the rendered DOM.

        Structure, read off a real captured page (logs/adb_no_results.html):

            div.searchstax-search-result.searchstax-tender-result   one result
              a.searchstax-search-result-title                      title + link
              span.searchstax-search-result-common   x7             "Label: value"
                Status: · Deadline: · Country/Economy: · Sector:
                Posting Date: · Notice Type: · Approval Number:

        The previous version searched every anchor on the page and then walked
        up looking for a block containing "Notice Type". It found nothing —
        because ADB writes that label as "Notice&nbsp;Type", so a match on
        "Notice Type" with an ordinary space never fires. Reading the labelled
        spans directly avoids guessing at whitespace altogether.
        """
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        blocks = soup.select("div.searchstax-search-result")
        for block in blocks:
            a = block.select_one("a.searchstax-search-result-title") or \
                block.find("a", href=True)
            if a is None or not a.get("href"):
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 12:
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"{self.website}{href}"
            if url in seen:
                continue

            # "Label: value" pairs, normalised so a non-breaking space in either
            # the label or the value cannot cause a miss.
            fields: dict[str, str] = {}
            for span in block.select("span.searchstax-search-result-common"):
                raw = span.get_text(" ", strip=True).replace(" ", " ")
                label, sep, value = raw.partition(":")
                if sep and value.strip():
                    fields[" ".join(label.split()).lower()] = " ".join(value.split())

            status = fields.get("status", "")
            if status and status.lower() not in ("active", "open"):
                continue     # the search is already filtered to Active; belt and braces

            seen.add(url)
            country = fields.get("country/economy", "")
            sector = fields.get("sector", "")
            summary_bits = [
                f"Notice type: {fields['notice type']}" if "notice type" in fields else "",
                f"Sector: {sector}" if sector else "",
                f"Posted: {fields['posting date']}" if "posting date" in fields else "",
                f"Approval number: {fields['approval number']}" if "approval number" in fields else "",
            ]
            items.append(
                RawOpportunity(
                    title=title[:500],
                    organization="Asian Development Bank",
                    country=country[:128],
                    location=country[:512],
                    vertical=sector[:256],
                    summary=" | ".join(b for b in summary_bits if b)[:2000],
                    deadline_raw=fields.get("deadline", "")[:64],
                    # ADB prints both on every result row and this scraper was
                    # writing them into the summary text only. Handed to the
                    # contract, they are what lets ADB's manifest — which
                    # excludes contract awards and projects — actually reject
                    # something.
                    record_type=record_type_for(fields.get("notice type", "")),
                    source_status=status[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                )
            )

        if blocks and not items:
            log.warning("[adb] %s result blocks on the page but none parsed — the "
                        "widget's markup has changed", len(blocks))
        log.info("[adb] parsed %s listing(s) from %s result block(s)",
                 len(items), len(blocks))
        return items

    def next_page(self, html, page_url, page_number):
        """Unused — pagination happens by clicking inside crawl()."""
        return None

    async def crawl(self, stop_event, pause_event, progress):
        """Drive the page in a browser, yielding one batch per rendered page."""
        queue: Queue = Queue()
        done = threading.Event()

        def worker() -> None:
            try:
                self._walk(queue, stop_event, done)
            except Exception:
                log.exception("[adb] browser walk failed")
            finally:
                done.set()
                queue.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        page_number = 0
        while True:
            if stop_event.is_set():
                done.set()
                break
            await pause_event.wait()
            try:
                payload = await asyncio.to_thread(queue.get, True, 1.0)
            except Empty:
                if done.is_set():
                    break
                continue
            if payload is None:
                break
            page_number += 1
            html = payload
            await progress("page_start", {"source": self.name, "page": page_number,
                                          "url": LISTING_URL})
            items = self.parse_listing(html, LISTING_URL)
            if items:
                yield items
            await progress("page_done", {"source": self.name, "page": page_number,
                                         "found": len(items)})
        await progress("pages_end", {"source": self.name, "page": page_number})

    def _walk(self, queue: Queue, stop_event, done) -> None:
        """Blocking browser walk. Pushes the rendered HTML of each page."""
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        with sync_playwright() as pw:
            context = site_auth.open_context(pw, self.name, headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()

                # Step one: the plain page, exactly as a person opens it. This
                # is what sets ADB's cookies and gives the next request a real
                # referer to carry.
                page.goto(LISTING_URL, timeout=90_000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass

                if self._blocked(page):
                    log.error(
                        "[adb] BLOCKED on the plain listing page. This is "
                        "Cloudflare's firewall-rule page (\"you have been "
                        "blocked\"), not a challenge that clears on its own. "
                        "Open %s in an ordinary browser: if that works too, the "
                        "block is against this machine's requests; if it also "
                        "shows the block page, the IP itself is refused and no "
                        "scraper change reaches it.", LISTING_URL,
                    )
                    self._dump(page, "adb_blocked")
                    return

                # The unfiltered count, read here while the plain listing is
                # still on screen. It is the baseline the facet check compares
                # against, and reading it now is the only chance — after the
                # faceted navigation there is nothing left to compare with.
                baseline = self._read_total(page)

                # Step two: move to the sorted search, carrying the page we just
                # loaded as the referer. Navigating straight here from a cold
                # context is what used to be refused.
                if not self._goto_search(page, 1):
                    log.warning("[adb] the sorted search did not load — falling "
                                "back to the plain listing, which yields page 1 "
                                "only")

                # Proof the widget rendered: count the result elements it
                # creates. The previous check looked for the text "Notice Type",
                # which ADB writes as "Notice&nbsp;Type" — so innerText contains
                # a non-breaking space and the match never fired. A page holding
                # 12 perfectly good tenders was reported as "results never
                # rendered" and thrown away. Counting nodes cannot be defeated
                # by an invisible character.
                try:
                    page.wait_for_function(
                        f"() => document.querySelectorAll({RESULT_BLOCK!r})"
                        ".length > 0",
                        timeout=45_000,
                    )
                except Exception:
                    log.error(
                        "[adb] results never rendered. Saving the page for "
                        "inspection — if its title mentions Cloudflare, ADB is "
                        "refusing automated access and the fix is to ask them "
                        "for a tender feed rather than to retry."
                    )
                    self._dump(page, "adb_no_results")
                    return

                # The facets arrived in the URL above. Prove it, rather than
                # assume it: a facet parameter that is accepted and ignored
                # looks exactly like one that worked, and this scraper has
                # already been bitten twice by precisely that shape
                # (searchstax[page], and World Bank's os={offset}).
                #
                # The proof is the rendered rows themselves — if every row says
                # Status: Active, the status facet applied.
                faceted_total = self._read_total(page)
                filtered = self._facet_applied(faceted_total, baseline)
                if filtered:
                    log.info(
                        "[adb] facets applied via URL (%s): %s of %s tender(s). "
                        "Row statuses on this page: %s",
                        ", ".join(configured_facets()),
                        f"{faceted_total:,}",
                        f"{baseline:,}" if baseline else "?",
                        dict(self._status_mix(page.content())))
                    walk_url = page.url
                else:
                    log.warning(
                        "[adb] the URL facets did NOT take — still %s record(s) "
                        "against an unfiltered %s. Falling back to ticking the "
                        "Status checkbox.",
                        f"{faceted_total:,}" if faceted_total else "?",
                        f"{baseline:,}" if baseline else "?")
                    filtered = self._select_status_facet(page)
                    # The site rewrites its own URL when the facet is ticked, so
                    # this carries whatever encoding the widget uses.
                    walk_url = page.url if filtered else None

                total = self._read_total(page)
                per_page = len(page.query_selector_all("div.searchstax-search-result")) or 12
                # Unfiltered runs get a bigger page budget, because the SORT is
                # doing the filtering's job — see below.
                budget = self.max_pages if filtered else self.unfiltered_max_pages
                needed = -(-total // per_page) if total else 0
                pages = min(budget, needed) if total else budget
                log.info("[adb] %s tender(s) to walk at %s per page -> %s page(s)%s",
                         f"{total:,}" if total else "?", per_page, pages,
                         "" if filtered else " (Status facet NOT applied)")
                if filtered and needed > budget:
                    # "All the active tenders should be scraped" is the whole
                    # requirement, and a budget that silently truncates the walk
                    # is how it stops being true without anyone noticing. The
                    # cap stays — an unbounded walk against a control that never
                    # disables itself is worse — but it says so now.
                    log.warning(
                        "[adb] the page budget TRUNCATES this walk: %s page(s) "
                        "needed for %s filtered record(s), cap is %s, so ~%s "
                        "tender(s) will not be reached. Raise max_pages.",
                        needed, f"{total:,}", budget, (needed - budget) * per_page)
                if total and not filtered:
                    # This used to warn that an unfiltered run sees "only the
                    # first 720 of 51,013" and was therefore nearly useless.
                    # That reading ignored the sort order, and the sort is what
                    # makes an unfiltered run survivable.
                    #
                    # SEARCH_URL asks for ds_date_closing DESC — closing date,
                    # latest first. Every tender that is still open closes in
                    # the future, so open tenders sort to the FRONT and the
                    # 37,769 closed ones sit at the back where the walk never
                    # reaches. ADB's own facet counts are ~489 Active plus ~396
                    # Advance Notice, so roughly 885 records carry a future
                    # closing date — about 74 pages at 12 per page.
                    #
                    # 60 pages (the filtered budget) would have stopped ~15
                    # pages short of that and quietly missed live tenders. The
                    # unfiltered budget is set past it deliberately.
                    log.warning(
                        "[adb] the Status facet did not apply, so this is an "
                        "UNFILTERED walk of %s record(s), capped at %s pages "
                        "(%s records). The sort is closing-date-descending, so "
                        "the open tenders are at the front and should all be "
                        "inside that — but it is guesswork rather than a "
                        "filter, so fix the facet if this recurs.",
                        f"{total:,}", pages, pages * per_page,
                    )

                mode = PagingMode()
                for n in range(1, pages + 1):
                    if stop_event.is_set() or done.is_set():
                        return
                    html = page.content()
                    queue.put(html)
                    if n >= pages:
                        log.info("[adb] reached page %s of %s — done", n, pages)
                        return
                    before = self._results_signature(html)

                    # Mechanism one: ask for the next page by URL. searchstax
                    # [page] is the widget's own parameter, so on a site where
                    # it works this is the same request clicking would make,
                    # minus the DOM archaeology.
                    #
                    # On this site it does NOT work: the widget re-runs its
                    # default query on a fresh navigation and re-serves page 1.
                    # The URL is still tried once, because it is cheaper and
                    # because a fixed ADB deploy should be picked up
                    # automatically rather than needing this file edited again.
                    if mode.should_try_url():
                        moved = self._goto_page(page, walk_url, n + 1)
                        changed = (moved
                                   and self._results_signature(page.content()) != before)
                        action = mode.url_result(attempted=moved, changed=changed)
                        if action == "continue":
                            if n % 10 == 0:
                                log.info("[adb] page %s of %s", n, pages)
                            page.wait_for_timeout(
                                int(settings.rate_limit_delay * 1000))
                            continue
                        if action == "end":
                            log.info("[adb] page %s returned the same results as "
                                     "page %s — end of the list", n + 1, n)
                            return
                        # action == "click": the URL never paged at all.
                        #
                        # This is where the walk used to give up, and it is the
                        # whole defect. `_goto_page` returns True whenever the
                        # URL *loads* — it loads fine, it just serves page 1 —
                        # so the guard below it saw unchanged rows and returned
                        # out of the walk entirely. The click path underneath
                        # was unreachable on every healthy run, and ADB yielded
                        # 12 of ~489 Active tenders while reporting success.
                        #
                        # The failed navigation has left the widget showing
                        # page 1, which is exactly where clicking next has to
                        # start from, so falling through here is safe. It is
                        # only safe ONCE, which is why PagingMode latches: a
                        # later navigation would reset the widget to page 1 and
                        # silently restart the walk.
                        log.warning(
                            "[adb] searchstax[page]=%s re-served page 1's %s "
                            "result(s) on a fresh navigation — the parameter is "
                            "accepted and ignored. Switching to the widget's own "
                            "next control for the rest of this walk (%s record(s) "
                            "to go).",
                            n + 1, per_page, f"{total:,}" if total else "?")

                    if not self._click_next(page, n + 1):
                        return
                    if n % 10 == 0:
                        log.info("[adb] page %s of %s", n, pages)
                    page.wait_for_timeout(int(settings.rate_limit_delay * 1000))
            finally:
                # close_owned also closes the Browser that owns this
                # context; context.close() alone left the Chromium
                # process running. It guards each step internally, so
                # the try/except that used to wrap this is redundant.
                site_auth.close_owned(context)

    @classmethod
    def _goto_page(cls, page, walk_url: str | None, page_nr: int) -> bool:
        """Go to page N, preserving the facet the walk ticked.

        `walk_url` is the URL the SITE settled on once the facets were applied.
        Named apart from the module-level `search_url()` deliberately: one is the
        URL we construct, the other is the URL the widget ended up on, and a
        single name for both is one edit away from an UnboundLocalError.
        The old wording follows. It was the URL the SITE produced after the facet was
        applied. Paging from it keeps the filter; paging from the module-level
        SEARCH_URL would silently drop it and quietly walk all 51,013 records
        instead of 489.
        """
        if walk_url:
            try:
                page.goto(cls._with_page(walk_url, page_nr), timeout=90_000,
                          wait_until="domcontentloaded", referer=LISTING_URL)
            except Exception as exc:                            # noqa: BLE001
                log.info("[adb] page %s did not load (%s)", page_nr, exc)
                return False
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            return not cls._blocked(page)
        return cls._goto_search(page, page_nr)

    @staticmethod
    def _results_signature(html: str) -> str:
        """A fingerprint of the RESULT ROWS on a page, and nothing else.

        Pure and takes HTML rather than a Playwright page, so the guard that
        decides whether pagination advanced can be tested without a browser —
        which is why the previous guard's defect survived: nothing could
        exercise it.

        Built from each result block's link plus its first line of text. The
        link alone would be enough on a well-behaved listing, but a widget that
        re-renders the same rows with rotated tracking parameters would defeat
        it, and the text would not.

        An empty result set returns "" — the caller must treat "no results" as
        its own case, not as "the same as last time".
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "lxml")
        parts: list[str] = []
        for block in soup.select(RESULT_BLOCK):
            a = block.find("a", href=True)
            href = (a["href"].split("?")[0] if a else "")
            first_line = block.get_text(" ", strip=True)[:120]
            parts.append(f"{href}|{first_line}")
        if not parts:
            return ""
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _read_total(page) -> int:
        """Result count from the pagination bar, or 0 if it can't be read."""
        try:
            bar = page.query_selector(".searchstax-pagination-details")
            if bar is None:
                return 0
            m = _TOTAL.search(bar.inner_text() or "")
            return int(m.group(1).replace(",", "")) if m else 0
        except Exception:
            return 0

    @staticmethod
    def _select_status_facet(page, want: str = STATUS_FACET) -> bool:
        """Tick a Status facet and wait for the result count to drop.

        Ticking the box rather than constructing a facet URL, because the
        widget's URL encoding for facets is undocumented and guessing it would
        silently return the unfiltered set — 51,013 records instead of 489, with
        nothing in the output to say the filter had been ignored. Clicking is
        what a person does, and the site then rewrites its own URL, which the
        caller reads back to page with.

        Returns False if the facet isn't there or the count never changed.
        """
        before = AdbTendersScraper._read_total(page)
        target = None
        for sel in (f'input.searchstax-facet-input-checkbox[aria-label^="{want} "]',
                    f'[class*="sm_fct_status"] input[aria-label^="{want} "]'):
            try:
                target = page.query_selector(sel)
                if target:
                    break
            except Exception:
                continue
        if target is None:
            log.warning("[adb] no %r status facet on the page — crawling unfiltered, "
                        "which is 51k records instead of ~489", want)
            return False

        # The checkbox is readonly; the widget listens on the surrounding
        # container, so click that rather than the input itself.
        try:
            container = target.evaluate_handle(
                "el => el.closest('.searchstax-facet-value-container') || el.parentElement")
            (container.as_element() or target).click(timeout=10_000)
        except Exception as exc:                                # noqa: BLE001
            log.warning("[adb] could not tick the %r facet (%s)", want, exc)
            return False

        # Wait for the count to actually change, not a fixed sleep.
        for _ in range(40):
            page.wait_for_timeout(500)
            now = AdbTendersScraper._read_total(page)
            if now and now != before:
                log.info("[adb] status filter %r applied — %s of %s tenders",
                         want, f"{now:,}", f"{before:,}" if before else "?")
                return True
        log.warning("[adb] the %r facet did not change the result count (still %s)",
                    want, before or "?")
        return False

    @staticmethod
    def _with_page(url: str, n: int) -> str:
        """The same search URL, asking for page n."""
        if _PAGE_PARAM.search(url):
            return _PAGE_PARAM.sub(lambda m: f"{m.group(1)}{n}", url, count=1)
        return f"{url}{'&' if '?' in url else '?'}searchstax[page]={n}"

    @staticmethod
    def _blocked(page) -> bool:
        """True on Cloudflare's firewall-rule page.

        Distinct from "Just a moment..." on purpose. That one is a challenge
        that can pass on its own; "Sorry, you have been blocked" is a rule that
        has already fired, and waiting for it achieves nothing but delay.
        """
        try:
            title = (page.title() or "").lower()
        except Exception:
            return False
        if "attention required" in title or "access denied" in title:
            return True
        try:
            body = (page.inner_text("body") or "")[:600].lower()
        except Exception:
            return False
        return "you have been blocked" in body or "unable to access" in body

    @staticmethod
    def _goto_search(page, page_nr: int) -> bool:
        """Navigate to the sorted search, carrying the current page as referer.

        The referer is the point. A request for searchstax[...] that appears out
        of nowhere looks like someone probing query parameters; the same request
        arriving from ADB's own tenders page looks like what it is — a person
        using the widget. Playwright will not add a referer on its own, so it is
        passed explicitly.
        """
        try:
            page.goto(search_url(page_nr), timeout=90_000,
                      wait_until="domcontentloaded", referer=LISTING_URL)
        except Exception as exc:                                # noqa: BLE001
            log.info("[adb] search page %s did not load (%s)", page_nr, exc)
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        return not AdbTendersScraper._blocked(page)

    @classmethod
    def _click_next(cls, page, page_nr: int) -> bool:
        """Advance one page by driving the widget, and verify that it moved.

        Returns True only when the RESULT ROWS changed. "The DOM changed" is
        not enough — the widget empties its container while it fetches, and a
        spinner is a change.

        Three things happen in order, and each has to hold:
          1. snapshot the rows as they are now;
          2. click a control, preferring the target page NUMBER over "next"
             (a numbered button says which page it goes to, where a "next"
             that is really a disabled decoration says nothing);
          3. wait for the rows to differ from the snapshot, then confirm with
             the same signature the URL path is judged by.
        """
        try:
            before_rows = page.evaluate(_ROWS_JS)
        except Exception as exc:                                # noqa: BLE001
            log.info("[adb] could not read the result rows (%s) — stopping", exc)
            return False
        before_sig = cls._results_signature(page.content())

        # Ask the bar whether there IS a next page before touching it. On the
        # last page the anchor is still present and still says "Next >"; only
        # its class and pointer-events say otherwise. Clicking it would do
        # nothing and the walk would then wait the full 30 seconds for rows
        # that were never going to change, once per source, every run.
        state = cls._pagination_state(page.content())
        if state == "end":
            log.info("[adb] the Next control is disabled — end of the list at "
                     "page %s", page_nr - 1)
            return False

        control, how = cls._pagination_control(page, page_nr)
        if control is None:
            if page_nr == 2:
                # On page 2 this is not the end of anything — it means neither
                # mechanism works and the walk is about to return twelve rows
                # out of hundreds. Saving the page is what turns that from a
                # guess into a fact: the labels this build actually uses are in
                # the HTML, and no amount of reading this file supplies them.
                log.error(
                    "[adb] neither searchstax[page] nor any pagination control "
                    "advanced the list, so this run holds ONE page of a source "
                    "with hundreds of Active tenders. Saved "
                    "logs/adb_pagination.html — the pagination markup in it is "
                    "what _pagination_control needs to match.")
                cls._dump(page, "adb_pagination")
            else:
                log.info("[adb] no usable pagination control for page %s — "
                         "treating this as the end of the list", page_nr)
            return False
        try:
            control.click(timeout=10_000)
        except Exception as exc:                                # noqa: BLE001
            log.info("[adb] the %s control was not clickable (%s) — stopping",
                     how, exc)
            return False

        # Wait for the list to actually change rather than sleeping a fixed
        # time: the widget swaps content in place, so there is no navigation
        # event to wait on.
        try:
            page.wait_for_function(_ROWS_CHANGED_JS, arg=before_rows,
                                   timeout=30_000)
        except Exception:
            log.info("[adb] the result rows did not change within 30s of using "
                     "the %s control — assuming the end of the list", how)
            return False
        if cls._results_signature(page.content()) == before_sig:
            log.info("[adb] the rows re-rendered identical after the %s control "
                     "— end of the list", how)
            return False
        if page_nr == 2:
            # Said once, on the first successful click, because it is the fact
            # that resolves how this widget actually pages. The log is the only
            # place that can answer it from production.
            log.info("[adb] pagination is working via the %s control", how)
        return True

    @staticmethod
    def _status_mix(html: str):
        """Counter of the Status value on each rendered row.

        Pure and takes HTML, so "did the facet apply" is a question a test can
        ask. The clicking path could only answer it by watching a number change
        in a browser, which is why it could never be checked in CI.
        """
        from collections import Counter

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "lxml")
        mix: Counter = Counter()
        for block in soup.select(RESULT_BLOCK):
            m = _STATUS.search(block.get_text(" ", strip=True))
            mix[m.group(1).strip() if m else "unlabelled"] += 1
        return mix

    @staticmethod
    def _facet_applied(total: int, baseline: int = 0) -> bool:
        """True when the RESULT COUNT dropped — the only honest evidence.

        The obvious test is "are all the rendered rows Active", and it is
        wrong. Checked against the captured page on 2026-09-02: it reads
        "1 - 12 of 51013" — the whole unfiltered universe — and all twelve of
        its rows say Status: Active. The sort is ds_date_closing DESC, so open
        tenders come first by construction, and page 1 of an UNFILTERED walk
        looks exactly like page 1 of a filtered one.

        A row-level check would therefore have passed on an unfiltered crawl,
        taken the 60-page budget and covered 720 of 51,013 records while
        reporting success — the same "accepted and ignored" shape as
        searchstax[page] and World Bank's os={offset}, produced by the guard
        written to catch it.

        The count is the discriminator, because the count is the one thing a
        filter cannot leave unchanged. `baseline` is the unfiltered total read
        off the plain listing page moments earlier, so this is a comparison
        rather than a threshold.
        """
        if not total:
            return False                    # nothing read; cannot claim either
        if baseline:
            return total < baseline
        # No baseline (the listing page did not publish a count). Fall back to
        # plausibility: ADB's filtered sets are hundreds, the unfiltered one is
        # 51,013. This is a backstop, not the test.
        return total <= _MAX_PLAUSIBLE_FILTERED

    @staticmethod
    def _pagination_state(html: str) -> str:
        """"next", "end" or "missing", read from the pagination bar.

        Pure and takes HTML, for the same reason `_results_signature` is: the
        last defect in this file survived because it lived inside a Playwright
        loop where no test could reach it. This one is checked against a real
        captured bar in tests/fixtures/adb_pagination_bar.html.

        "missing" is deliberately distinct from "end". A bar that is not there
        at all means the page did not render what we think it renders, which is
        a defect to report; a bar whose Next is disabled means the walk
        finished, which is success.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "lxml")
        nxt = soup.find(id="searchstax-pagination-next")
        if nxt is None:
            nxt = soup.select_one("a.searchstax-pagination-next")
        if nxt is None:
            return "missing"
        classes = nxt.get("class") or []
        style = (nxt.get("style") or "").replace(" ", "").lower()
        if _DISABLED_CLASS in classes or "pointer-events:none" in style:
            return "end"
        return "next"

    @classmethod
    def _pagination_control(cls, page, page_nr: int):
        """(element, description) for the control that advances the list.

        Selectors, not label text, because the id here is stable and the label
        is "Next >" — a space and an entity away from every exact-match guess.
        The label search stays as a last resort for a redeploy that renames the
        id, and matches on a prefix rather than equality for that same reason.
        """
        for sel in _NEXT_SELECTORS:
            try:
                el = page.query_selector(sel)
            except Exception:
                continue
            if el is not None:
                return el, f"Next control ({sel})"
        # Last resort: an anchor or button whose visible label STARTS WITH
        # "next". Startswith, because this one reads "Next >".
        try:
            handle = page.evaluate_handle(_FIND_BY_LABEL_JS, "next")
            el = handle.as_element()
            if el is not None:
                return el, "control labelled Next"
        except Exception:
            pass
        return None, ""

    @staticmethod
    def _dump(page, stem: str) -> None:
        try:
            d = settings.log_dir
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{stem}.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(d / f"{stem}.png"), full_page=False)
            log.warning("[adb] saved logs/%s.html and .png", stem)
        except Exception:
            pass
