"""Small funder-site scrapers: Packard, Open Society, Blue Action Fund.

These are foundations listing a handful of their own open calls (unlike
aggregators with hundreds). Same plugin contract; low volume, high quality.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

_MONTH_DATE = re.compile(
    r"deadline\s*:?\s*(\w{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+\w{3,9},?\s+\d{4}|passed|ongoing|rolling)",
    re.IGNORECASE,
)


@register
class PackardScraper(BaseScraper):
    """Packard Foundation RFPs — /funding-opportunity/<slug>/ cards with
    'Deadline: July 22, 2026' (US month-first text dates)."""

    name = "packard"
    display_name = "Packard Foundation"
    website = "https://www.packard.org"
    start_url = ("https://www.packard.org/grantees/funding-opportunties/"
                 "?visc-qf-funding_meta_filter_tf_active=true")

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            if "/funding-opportunity/" not in a["href"]:
                continue
            url = a["href"].split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            text = a.get_text(" ", strip=True)
            deadline = _MONTH_DATE.search(text)
            # title = text before the description run-on; fall back to slug
            title = text.split("Deadline:")[0].strip()[:300]
            if len(title) < 12:
                title = url.rstrip("/").split("/")[-1].replace("-", " ").title()
            items.append(RawOpportunity(
                title=title,
                organization="The David and Lucile Packard Foundation",
                deadline_raw=(deadline.group(1) if deadline else "")[:64],
                country="Global", region="Global",
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
                dayfirst=False,     # US date wording
            ))
        return items


@register
class OpenSocietyScraper(BaseScraper):
    """Open Society Foundations — /grants list; items carry 'DEADLINE: <date|Passed>'."""

    name = "opensociety"
    display_name = "Open Society Foundations"
    website = "https://www.opensocietyfoundations.org"
    start_url = "https://www.opensocietyfoundations.org/grants"

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/grants/" not in href or any(
                skip in href for skip in ("/grants/past", "/grants/faq", "?")
            ):
                continue
            url = href if href.startswith("http") else self.website + href
            if url in seen or url.rstrip("/").endswith("/grants"):
                continue
            seen.add(url)
            text = a.get_text(" ", strip=True)
            if len(text) < 20:
                continue
            deadline = _MONTH_DATE.search(text)
            deadline_raw = deadline.group(1) if deadline else ""
            if deadline_raw.lower() == "passed":
                continue        # site says it's over — don't even ingest
            title = text.split("DEADLINE")[0].strip()[:300]
            items.append(RawOpportunity(
                title=title,
                organization="Open Society Foundations",
                deadline_raw=deadline_raw[:64],
                country="Global", region="Global",
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
                dayfirst=False,
            ))
        return items


@register
class BlueActionFundScraper(BaseScraper):
    """Blue Action Fund — open Calls for Proposals on /funding-opportunities/.
    (Their /grant-programme/ grants list is awarded projects, deliberately skipped.)"""

    name = "blueaction"
    display_name = "Blue Action Fund"
    website = "https://www.blueactionfund.org"
    start_url = "https://www.blueactionfund.org/funding-opportunities/"

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        # calls appear as headed sections; find headings mentioning call/proposal
        for h in soup.find_all(["h2", "h3", "h4"]):
            heading = h.get_text(" ", strip=True)
            if not re.search(r"call|proposal", heading, re.IGNORECASE):
                continue
            if len(heading) < 15:      # section labels like 'Calls for Proposals'
                continue
            block = []
            for sib in h.find_all_next(["p", "li", "div"], limit=10):
                if sib.find(["h2", "h3"]):
                    break
                block.append(sib.get_text(" ", strip=True))
            block_text = " ".join(block)
            deadline = re.search(
                r"(deadline|closes?|due)[^\.]{0,40}?(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4})",
                block_text, re.IGNORECASE,
            )
            items.append(RawOpportunity(
                title=heading[:300],
                organization="Blue Action Fund",
                deadline_raw=(deadline.group(2) if deadline else "")[:64],
                summary=block_text[:800],
                vertical="Environment",
                country="Global", region="Global",
                opportunity_url=self.start_url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
            ))
        return items
