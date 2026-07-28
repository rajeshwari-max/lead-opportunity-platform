"""GrantWatch International scraper (https://international.grantwatch.com/new-grants.php).

Server-rendered cards (title link /grant/<id>/..., 'Deadline: MM/DD/YY' or
'Ongoing', summary, GrantWatch ID#) but the pager is JavaScript-only, so with
Playwright installed the rendered session clicks through pages and accumulates
them. Dates are US-format (dayfirst=False). Detail pages are subscription-
gated; the public listing already carries title/deadline/summary.
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

log = logging.getLogger("scraper")

_GRANT_LINK = re.compile(r"/grant/(\d+)/", re.IGNORECASE)
_DEADLINE = re.compile(r"Deadline\s*:?\s*([0-9/]{6,10}|Ongoing)", re.IGNORECASE)
_MAX_PAGES = 30


@register
class GrantWatchScraper(BaseScraper):
    name = "grantwatch"
    display_name = "GrantWatch Intl"
    website = "https://international.grantwatch.com"
    start_url = "https://international.grantwatch.com/new-grants.php"
    prefer_js = True   # pager is JS-only; plain HTTP still yields page 1 (~14 items)

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None    # all pages accumulated in one rendered session

    def _fetch_rendered_sync(self, url: str) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=settings.user_agent)
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

                chunks: list[str] = []
                first_link_js = (
                    "(document.querySelector(\"a[href*='/grant/']\") || {}).href || ''"
                )
                for _ in range(_MAX_PAGES):
                    chunks.append(page.content())
                    marker = page.evaluate(first_link_js)
                    # click the '›' (next) pager control, wherever it lives
                    moved = page.evaluate(
                        """() => {
                            const els = Array.from(
                                document.querySelectorAll('a, button, li'));
                            const nxt = els.find(e =>
                                e.textContent.trim() === '›' ||
                                e.getAttribute?.('aria-label') === 'Next');
                            if (!nxt) return false;
                            const target = nxt.tagName === 'LI'
                                ? (nxt.querySelector('a,button') || nxt) : nxt;
                            target.click();
                            return true;
                        }"""
                    )
                    if not moved:
                        break
                    try:  # wait until the first grant link actually changes
                        page.wait_for_function(
                            f"{first_link_js} !== {marker!r}", timeout=12_000
                        )
                    except Exception:
                        break  # no change — last page reached
                log.info("[grantwatch] accumulated %s page snapshots", len(chunks))
                return "<html><body>" + "".join(chunks) + "</body></html>"
            finally:
                browser.close()

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            m = _GRANT_LINK.search(a["href"])
            title = a.get_text(" ", strip=True)
            if not m or len(title) < 15 or title.lower() == "view grant":
                continue
            url = a["href"] if a["href"].startswith("http") else self.website + a["href"]
            if url in seen:
                continue
            seen.add(url)

            # card container: nearest ancestor that includes the Deadline label
            card = a
            card_text = ""
            for _ in range(6):
                card = card.parent
                if card is None:
                    break
                card_text = card.get_text(" ", strip=True)
                if "Deadline" in card_text:
                    break
            deadline = _DEADLINE.search(card_text or "")

            # summary = longest paragraph-ish text minus the title
            summary = ""
            if card is not None:
                for p in card.find_all("p"):
                    text = p.get_text(" ", strip=True)
                    if len(text) > 80 and "GrantWatch" not in text[:20]:
                        summary = text[:1000]
                        break

            items.append(
                RawOpportunity(
                    title=title[:500],
                    deadline_raw=(deadline.group(1) if deadline else "")[:64],
                    summary=summary,
                    country="Global",
                    region="Global",
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,
                    dayfirst=False,   # US site: 09/18/26 = September 18
                )
            )
        return items
