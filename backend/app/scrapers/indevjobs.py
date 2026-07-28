"""IndevJobs funding scraper (https://indevjobs.org/funding).

JavaScript app (empty HTML shell) — rendered via Playwright. Parsing is
defensive: funding detail links + deadline text near each card; the first run's
debug capture refines selectors if the DOM differs.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

_DEADLINE_NEAR = re.compile(
    r"(deadline|closing|apply by|due)[^\w]{0,10}[^\d]{0,20}"
    r"(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE,
)


@register
class IndevJobsScraper(BaseScraper):
    name = "indevjobs"
    display_name = "IndevJobs"
    website = "https://indevjobs.org"
    start_url = "https://indevjobs.org/funding"
    requires_js = True   # SPA — empty without a browser

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.search(r"/funding/[a-z0-9-]{8,}", href, re.IGNORECASE):
                continue
            url = href if href.startswith("http") else self.website + href
            title = a.get_text(" ", strip=True)
            if url in seen or len(title) < 12:
                continue
            seen.add(url)

            deadline_raw, org = "", ""
            card = a
            for _ in range(5):
                card = card.parent
                if card is None:
                    break
                text = card.get_text(" ", strip=True)
                m = _DEADLINE_NEAR.search(text)
                if m:
                    deadline_raw = m.group(2)
                    break

            items.append(RawOpportunity(
                title=title[:400],
                organization=org,
                deadline_raw=deadline_raw[:64],
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
            ))
        return items

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        nxt = super().next_page(html, page_url, page_number)
        if nxt:
            return nxt
        # SPA-style ?page=N fallback; crawl stops on no-change/no-new pages
        if "page=" not in page_url and page_number == 1:
            return PageRequest(f"{self.start_url}?page=2")
        m = re.search(r"page=(\d+)", page_url)
        if m:
            return PageRequest(re.sub(r"page=\d+", f"page={int(m.group(1)) + 1}", page_url))
        return None
