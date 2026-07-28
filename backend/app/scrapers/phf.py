"""Paul Hamlyn Foundation funds scraper (https://www.phf.org.uk/funds/).

JS-rendered funds directory. Each fund card links to /funds/<slug>/; open funds
typically say 'Open' or carry a deadline; many are rolling (kept as ongoing).
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

_DEADLINE_NEAR = re.compile(
    r"(deadline|closing date|closes)[^\d]{0,20}"
    r"(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


@register
class PHFScraper(BaseScraper):
    name = "phf"
    display_name = "Paul Hamlyn Foundation"
    website = "https://www.phf.org.uk"
    start_url = "https://www.phf.org.uk/funds/"
    prefer_js = True

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/funds/([a-z0-9-]{5,})/?$", href, re.IGNORECASE)
            if not m:
                continue
            url = href if href.startswith("http") else self.website + href
            title = a.get_text(" ", strip=True)
            if url in seen or len(title) < 10:
                continue
            seen.add(url)

            card = a
            card_text = ""
            for _ in range(4):
                card = card.parent
                if card is None:
                    break
                card_text = card.get_text(" ", strip=True)
                if len(card_text) > len(title) + 30:
                    break
            deadline = _DEADLINE_NEAR.search(card_text or "")
            is_open = bool(re.search(r"\bopen\b", card_text or "", re.IGNORECASE))
            closed = bool(re.search(r"\bclosed\b", card_text or "", re.IGNORECASE))
            if closed and not deadline:
                continue

            items.append(RawOpportunity(
                title=title[:300],
                organization="Paul Hamlyn Foundation",
                deadline_raw=(deadline.group(2) if deadline else "")[:64],
                summary=(card_text or "")[:600],
                country="United Kingdom", region="Europe",
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
                assume_active=is_open and not deadline,   # 'Open', rolling fund
            ))
        return items
