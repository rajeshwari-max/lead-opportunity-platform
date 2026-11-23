"""UN Global Marketplace (UNGM): open procurement notices of ~40 UN agencies.

https://www.ungm.org/Public/Notice lists every open notice (RFP, RFQ, ITB,
EOI, individual consultants, calls for proposals) from UNDP, UNICEF, UNOPS,
WHO, FAO, ILO, IOM, UNHCR, UN Women, UNESCO and others. Viewing notices is
public: no login is needed (an account is only for expressing interest and
downloading documents, which this scraper never does — it stores the link to
the notice page). robots.txt allows /Public/Notice.

How the page works (read in a browser on 2026-11-23): the listing shows 15
notices and loads the next 15 each time the visitor scrolls to the bottom.
The page itself makes those requests, so this scraper opens the page in a real
browser and scrolls, exactly as a visitor would, until no more notices load,
then reads every row. Each row carries the title, deadline, published date,
agency, notice type, reference and beneficiary country, so no detail page is
visited.

UNDP notices also appear on UNDP's own board (undp_procurement); the ingest
step's cross-source check skips the copy when title, date and country match.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register
from app.services.notice_types import record_type_for

log = logging.getLogger("scraper")

LISTING_URL = "https://www.ungm.org/Public/Notice"
ROW = ".tableRow.dataRow"
_DATE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")

# UNGM's own wording -> record type, for the ones notice_types does not know.
_TYPES = {
    "request for eoi": "eoi",
    "call for individual consultants": "consultancy",
    "grant support-call for proposal": "call_for_proposals",
    "call for implementing partners": "call_for_proposals",
}


def _category(notice_type: str) -> Category:
    t = notice_type.lower()
    if "grant" in t or "implementing partner" in t:
        return Category.GRANT
    if "proposal" in t or "eoi" in t or "consultant" in t:
        return Category.RFP
    return Category.TENDER


@register
class UNGMScraper(BaseScraper):
    name = "ungm"
    display_name = "UNGM (UN Global Marketplace)"
    website = "https://www.ungm.org"
    start_url = LISTING_URL
    requires_js = True          # the rows are loaded by the page's own script
    prefer_js = True
    curated = True              # every row is a published UN procurement notice
    enrich_details = False      # the row already holds every field shown

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None             # one scrolled page holds every open notice

    def _fetch_rendered_sync(self, url: str) -> str:
        """Open the public listing and scroll until no more notices load."""
        from playwright.sync_api import sync_playwright

        cap = max(15, int(settings.ungm_max_notices))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=settings.user_agent, locale="en-US",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
                page = context.new_page()
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                page.wait_for_selector(ROW, timeout=60_000)
                count = page.locator(ROW).count()
                stalls = 0
                while count < cap and stalls < 3:
                    page.mouse.wheel(0, 20_000)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    try:
                        page.wait_for_function(
                            f"document.querySelectorAll('{ROW}').length > {count}",
                            timeout=15_000)
                        stalls = 0
                    except Exception:
                        stalls += 1     # nothing new in 15 s: probably the end
                    count = page.locator(ROW).count()
                log.info("[ungm] %s open notices loaded by scrolling%s", count,
                         " (cap reached)" if count >= cap else "")
                return page.content()
            finally:
                browser.close()

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        for row in soup.select(ROW):
            notice_id = (row.get("data-noticeid") or "").strip()
            title_el = row.select_one(".resultTitle .ungm-title") or row.select_one(".resultTitle")
            if not notice_id or notice_id in seen or title_el is None:
                continue
            seen.add(notice_id)
            cells = [" ".join(c.get_text(" ", strip=True).split())
                     for c in row.select(":scope > .tableCell")]
            # cells: options, title, deadline, published, agency, type, reference, country
            if len(cells) < 8:
                continue
            title = " ".join(title_el.get_text(" ", strip=True).split())
            deadline = _DATE.search(cells[2])
            published = _DATE.search(cells[3])
            agency, notice_type, reference, country = cells[4], cells[5], cells[6], cells[7]
            record_type = _TYPES.get(notice_type.lower()) or record_type_for(notice_type)
            summary = " · ".join(filter(None, [
                notice_type, agency, f"Reference {reference}" if reference else "",
                country, f"Published {published.group(0)}" if published else ""]))
            items.append(RawOpportunity(
                title=title[:500],
                organization=agency,
                country=country,
                deadline_raw=deadline.group(0) if deadline else "",
                opportunity_url=f"{self.website}/Public/Notice/{notice_id}",
                website=self.website,
                source_website=self.display_name,
                summary=summary[:1000],
                category_hint=_category(notice_type),
                record_type=record_type,
                source_status="open",
                dayfirst=True,
            ))
        return items
