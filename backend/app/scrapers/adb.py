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

        Parses by visible label rather than CSS class. The widget's classes are
        build-generated and would break on ADB's next deploy; "Notice Type:" is
        content and will not.
        """
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        items: list[RawOpportunity] = []
        seen: set[str] = set()

        # A result is a link to a tender detail page. Everything describing it
        # sits in the surrounding block.
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/projects/tenders/" not in href and "/tenders/" not in href:
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 12:
                continue
            url = href if href.startswith("http") else f"{self.website}{href}"
            if url in seen:
                continue

            block = a.find_parent(["article", "li", "div"])
            hops = 0
            while block is not None and hops < 4:
                text = block.get_text(" ", strip=True)
                if _NOTICE.search(text) or _DEADLINE.search(text):
                    break
                block = block.parent
                hops += 1
            text = block.get_text(" ", strip=True) if block else title

            status = (_STATUS.search(text) or [None, ""])[1] if _STATUS.search(text) else ""
            if status and status.lower() not in ("active", "open"):
                continue     # the search is already filtered to Active; belt and braces

            seen.add(url)
            deadline = _DEADLINE.search(text)
            country = _COUNTRY.search(text)
            sector = _SECTOR.search(text)
            notice = _NOTICE.search(text)
            posted = _POSTED.search(text)

            summary_bits = [
                f"Notice type: {notice.group(1).strip()}" if notice else "",
                f"Sector: {sector.group(1).strip()}" if sector else "",
                f"Posted: {posted.group(1).strip()}" if posted else "",
            ]
            items.append(
                RawOpportunity(
                    title=title[:500],
                    organization="Asian Development Bank",
                    country=(country.group(1).strip() if country else "")[:128],
                    location=(country.group(1).strip() if country else "")[:512],
                    vertical=(sector.group(1).strip() if sector else "")[:256],
                    summary=" | ".join(b for b in summary_bits if b)[:2000],
                    deadline_raw=(deadline.group(1) if deadline else "")[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                )
            )

        log.info("[adb] parsed %s listing(s) from the rendered page", len(items))
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
                # The bare URL only. Adding the searchstax[...] parameters is
                # what gets the request refused — see the module docstring.
                page.goto(LISTING_URL, timeout=90_000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass

                # Proof the widget rendered: "Notice Type" is on every result row
                # and nowhere in the page shell.
                try:
                    page.wait_for_function(
                        "() => document.body && document.body.innerText.includes('Notice Type')",
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

                for n in range(1, self.max_pages + 1):
                    if stop_event.is_set() or done.is_set():
                        return
                    queue.put(page.content())

                    nxt = self._next_control(page)
                    if nxt is None:
                        log.info("[adb] no further page control after page %s", n)
                        return
                    before = page.inner_text("body")[:4000]
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
