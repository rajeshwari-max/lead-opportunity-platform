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
        passed = 0
        self.last_probe["all_notices_closed"] = False
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
                passed += 1
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
        # Every call on the page is marked "DEADLINE: PASSED" (the state on
        # 2026-11-16). That is the site saying it has nothing open, not a
        # parser that found nothing, so the run is recorded as "no open calls"
        # instead of a failure.
        if not items and passed:
            self.last_probe["all_notices_closed"] = True
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

    _NONE = re.compile(
        r"[^.]*\bno\s+(upcoming|current|open)\s+calls?\b[^.]*\.?", re.IGNORECASE)
    _DEADLINE = re.compile(
        r"(deadline|closes?|due)[^\.]{0,40}?"
        r"(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE)

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """Read the "Current call for proposals" section and nothing else.

        The page has three headings: "Calls for proposals", "Current call for
        proposals" and "Prior calls for proposals". The old parser stored each
        HEADING as an opportunity and would have listed every prior call (back
        to 2018) as open. Only what sits between "Current call" and "Prior
        calls" is a live call; when that section says there is none, that
        sentence is recorded as proof the source is empty.
        """
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        self.last_probe["empty_proof"] = ""
        current = next(
            (h for h in soup.find_all(["h2", "h3", "h4", "h5"])
             if re.search(r"current\s+calls?", h.get_text(" ", strip=True), re.I)),
            None)
        if current is None:
            return items
        texts: list[str] = []
        links: list[tuple[str, str]] = []
        for node in current.find_all_next(["h2", "h3", "h4", "h5", "p", "li", "a"]):
            if node.name in ("h2", "h3", "h4", "h5"):
                if re.search(r"prior|previous|past", node.get_text(" ", strip=True), re.I):
                    break
                texts.append(node.get_text(" ", strip=True))
            elif node.name == "a":
                href = (node.get("href") or "").strip()
                if href and not href.startswith(("#", "mailto:", "javascript:")):
                    links.append((node.get_text(" ", strip=True), href))
            else:
                texts.append(node.get_text(" ", strip=True))
        section = " ".join(t for t in texts if t)
        none = self._NONE.search(section)
        if none:
            self.last_probe["empty_proof"] = (
                f"the page's 'Current call for proposals' section says: "
                f"\"{none.group(0).strip()}\"")
            return items
        deadline = self._DEADLINE.search(section)
        calls = [(text, href) for text, href in links
                 if re.search(r"call|proposal|\.pdf", text + href, re.I)
                 and "funding-alert" not in href.lower()]
        title = next((t for t in texts
                      if 15 <= len(t) <= 200 and re.search(r"call|proposal", t, re.I)),
                     "")
        for text, href in calls[:5]:
            url = href if href.startswith("http") else self.website + href
            name = text if len(text) >= 15 else title.split(". ")[0]
            if not name:
                continue
            items.append(RawOpportunity(
                title=name[:300],
                organization="Blue Action Fund",
                deadline_raw=(deadline.group(2) if deadline else "")[:64],
                summary=section[:800],
                vertical="Environment",
                country="Global", region="Global",
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
            ))
        return items
