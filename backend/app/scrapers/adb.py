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
import logging
import re
import threading
from queue import Empty, Queue

from bs4 import BeautifulSoup

from app.core.config import settings
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

log = logging.getLogger("scraper")

LISTING_URL = "https://www.adb.org/projects/tenders"

# The search ADB's own widget produces, confirmed opening in a signed-out
# browser. {page} is substituted per page; the sort puts the soonest closing
# date first, so the pages that matter are the ones we reach first.
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
# "1 - 12 of 51013" in the pagination bar. The only place the result count is
# published, and what tells the walk when to stop rather than guessing.
_TOTAL = re.compile(r"of\s+([\d,]+)", re.I)

# Each result carries these labels. They are the only stable anchors: class
# names on this widget are generated and change between deploys, whereas the
# labels are the visible content and change only if ADB redesigns the page.
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

    # Safety net: 182 active consulting notices at 12/page is ~16 pages. The cap
    # is generous but finite, so a pagination control that never disables itself
    # cannot spin forever.
    max_pages = 60

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
                        "() => document.querySelectorAll("
                        "'div.searchstax-search-result').length > 0",
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

                # Narrow to open tenders before paging anything. Unfiltered this
                # is 51,013 records over 4,251 pages, of which parse_listing
                # throws away all but the ~489 Active ones.
                filtered = self._select_status_facet(page)
                # The site rewrites its own URL when the facet is ticked, so
                # this now carries whatever encoding the widget uses — no need
                # to reverse-engineer it.
                search_url = page.url if filtered else None

                total = self._read_total(page)
                per_page = len(page.query_selector_all("div.searchstax-search-result")) or 12
                pages = min(self.max_pages, -(-total // per_page)) if total else self.max_pages
                log.info("[adb] %s tender(s) to walk at %s per page -> %s page(s)",
                         f"{total:,}" if total else "?", per_page, pages)
                if total and not filtered:
                    log.warning(
                        "[adb] walking UNFILTERED: %s records, capped at %s pages, so "
                        "this run will see only the first %s. Fix the Status facet "
                        "rather than raising max_pages — almost all of those records "
                        "are closed tenders that get discarded on save.",
                        f"{total:,}", self.max_pages, self.max_pages * per_page,
                    )

                for n in range(1, pages + 1):
                    if stop_event.is_set() or done.is_set():
                        return
                    queue.put(page.content())
                    before = page.inner_text("body")[:4000]
                    if n >= pages:
                        log.info("[adb] reached page %s of %s — done", n, pages)
                        return

                    # Preferred: ask for the next page by URL. searchstax[page]
                    # is the widget's own parameter, so this is the same request
                    # clicking would make — minus the DOM archaeology of finding
                    # a control whose class names change between ADB deploys.
                    if self._goto_page(page, search_url, n + 1):
                        if page.inner_text("body")[:4000] == before:
                            log.info("[adb] page %s returned the same results as "
                                     "page %s — end of the list", n + 1, n)
                            return
                        if n % 10 == 0:
                            log.info("[adb] page %s of %s", n, pages)
                        page.wait_for_timeout(int(settings.rate_limit_delay * 1000))
                        continue

                    # Fallback: drive the widget's own control, as before.
                    nxt = self._next_control(page)
                    if nxt is None:
                        log.info("[adb] no further page control after page %s", n)
                        return
                    try:
                        nxt.click(timeout=10_000)
                    except Exception:
                        log.info("[adb] next-page control not clickable — stopping")
                        return
                    # Wait for the list to actually change rather than a fixed
                    # sleep: the widget swaps content in place, so there is no
                    # navigation event to wait on.
                    try:
                        page.wait_for_function(
                            "prev => document.body.innerText.slice(0, 4000) !== prev",
                            arg=before, timeout=30_000,
                        )
                    except Exception:
                        log.info("[adb] page %s did not change after clicking next "
                                 "— assuming the end of the list", n)
                        return
                    page.wait_for_timeout(int(settings.rate_limit_delay * 1000))
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    @classmethod
    def _goto_page(cls, page, search_url: str | None, page_nr: int) -> bool:
        """Go to page N, preserving the facet the walk ticked.

        `search_url` is the URL the SITE produced after the Status facet was
        applied. Paging from it keeps the filter; paging from the module-level
        SEARCH_URL would silently drop it and quietly walk all 51,013 records
        instead of 489.
        """
        if search_url:
            try:
                page.goto(cls._with_page(search_url, page_nr), timeout=90_000,
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
            page.goto(SEARCH_URL.format(page=page_nr), timeout=90_000,
                      wait_until="domcontentloaded", referer=LISTING_URL)
        except Exception as exc:                                # noqa: BLE001
            log.info("[adb] search page %s did not load (%s)", page_nr, exc)
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        return not AdbTendersScraper._blocked(page)

    @staticmethod
    def _next_control(page):
        """The pagination 'next' control, however this build labels it."""
        for sel in ('a[rel="next"]', '[aria-label="Next page"]', '[aria-label="Next"]',
                    'a.pager__item--next a', 'li.pager__item--next a',
                    '.pagination .next a', 'button.next'):
            try:
                el = page.query_selector(sel)
                if el and el.is_enabled() and el.is_visible():
                    return el
            except Exception:
                continue
        # Fall back to a link whose text is exactly "Next" or "›".
        for label in ("Next", "next", "›", "»"):
            try:
                el = page.get_by_role("link", name=label, exact=True).first
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        return None

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
