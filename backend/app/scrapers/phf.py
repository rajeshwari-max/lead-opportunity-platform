"""Open Paul Hamlyn Foundation funding opportunities.

The old ``/funds/`` URL is now a 404.  The current ``/funding`` page contains
three different things: funds open for applications, funds not accepting
applications, and invitation-only funding.  Only the cards beneath the
``#heading-54836`` "Open for applications" heading are opportunities.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register
from app.services.amounts import clean_amount, extract_amount

FUNDING_URL = "https://www.phf.org.uk/funding#heading-54836"

_DEADLINE_NEAR = re.compile(
    r"(deadline|closing date|closes)[^\d]{0,20}"
    r"(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


@register
class PHFScraper(BaseScraper):
    name = "phf"
    display_name = "Paul Hamlyn Foundation"
    website = FUNDING_URL
    start_url = FUNDING_URL
    curated = True

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        open_heading = soup.find(id="heading-54836")
        if open_heading is None:
            return []

        # Stop at the next H2 so "Not currently accepting applications" and
        # invitation-only funds can never leak into this source.
        for node in open_heading.find_all_next(["h2", "li"]):
            if node.name == "h2":
                break
            if node.name != "li" or not node.get("data-link"):
                continue

            a = node.select_one("h3 a[href]")
            if a is None:
                continue
            href = str(node.get("data-link") or a.get("href") or "").strip()
            url = urljoin("https://www.phf.org.uk", href)
            title = a.get_text(" ", strip=True)
            if url in seen or len(title) < 10:
                continue
            seen.add(url)

            card_text = " ".join(node.get_text(" ", strip=True).split())
            deadline = _DEADLINE_NEAR.search(card_text)
            rolling = bool(re.search(
                r"\brolling application cycle\b", card_text, re.IGNORECASE
            ))
            amount_match = re.search(
                r"\bAmount:\s*(.+?)(?=\s+Duration:|\s+Deadline:|$)",
                card_text,
                re.IGNORECASE,
            )
            amount = clean_amount(amount_match.group(1)) if amount_match else ""
            is_india = "india" in title.lower() or "/india" in url.lower()

            items.append(RawOpportunity(
                title=title[:300],
                organization="Paul Hamlyn Foundation",
                deadline_raw=(deadline.group(2) if deadline else "")[:64],
                summary=card_text[:1000],
                funding_amount=amount or extract_amount(card_text),
                country="India" if is_india else "United Kingdom",
                region="South Asia" if is_india else "Europe",
                opportunity_url=url,
                website=FUNDING_URL,
                source_website=self.display_name,
                category_hint=Category.GRANT,
                assume_active=rolling,
                record_type="grant",
                source_status="open",
            ))
        return items
