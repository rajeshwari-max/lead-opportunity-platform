"""NGOBOX grant announcements scraper (https://ngobox.org).

Parser is built against NGOBOX's real rendered markup (captured via debug dump):
    div.card
      a.card-title              -> title + detail href (full_grant_announcement_..._<id>)
      p.p_balck                 -> organization (class name is NGOBOX's own typo)
      strong "Deadline:" + text -> deadline, e.g. "03 Aug. 2026"
      "Grant Amount: 10000 USD" -> funding amount (when present)

NGOBOX serves stale cached pages to plain HTTP clients, so prefer_js renders it
through Playwright when installed (falls back to httpx otherwise).
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup, Tag

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

_DEADLINE_IN_TEXT = re.compile(
    r"deadline\s*:?\s*([0-3]?\d\s+\w{3,9}\.?,?\s+\d{4})", re.IGNORECASE
)
_AMOUNT = re.compile(r"grant\s+amount\s*:?\s*([^\n]{1,60})", re.IGNORECASE)
_PAGE_PARAM = re.compile(r"[?&](page|page_no|pageno|p)=(\d+)", re.IGNORECASE)


@register
class NGOBoxScraper(BaseScraper):
    name = "ngobox"
    display_name = "NGOBOX"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    website = "https://ngobox.org"
    start_url = "https://ngobox.org/grant_announcement_listing.php"
    # Stale-cache site: browser-render when Playwright is available.
    prefer_js = True

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        for card in soup.select("div.card"):
            a = card.select_one("a.card-title[href]")
            if a is None:
                continue
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if not title or len(title) < 8 or "grant_announcement" not in href.lower():
                continue
            url = str(httpx.URL(page_url).join(href))
            if url in seen:
                continue
            seen.add(url)

            card_text = card.get_text(" ", strip=True)
            deadline = _DEADLINE_IN_TEXT.search(card_text)
            amount = _AMOUNT.search(card_text)

            org_el = card.select_one("p.p_balck") or card.select_one("p.p_black")
            organization = org_el.get_text(" ", strip=True)[:512] if org_el else ""

            items.append(
                RawOpportunity(
                    title=title,
                    organization=organization,
                    funding_amount=(amount.group(1).strip() if amount else "")[:256],
                    deadline_raw=(deadline.group(1).strip() if deadline else "")[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,  # hint only — classifier decides
                )
            )
        return items

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        """Prefer an explicit Next link; else the smallest numbered page link
        greater than the current page (never hardcoded)."""
        nxt = super().next_page(html, page_url, page_number)
        if nxt:
            return nxt
        soup = BeautifulSoup(html, "lxml")
        candidates: list[tuple[int, str]] = []
        for a in soup.find_all("a", href=True):
            m = _PAGE_PARAM.search(a["href"])
            if m and int(m.group(2)) > page_number:
                candidates.append((int(m.group(2)), a["href"]))
        if candidates:
            candidates.sort()
            return PageRequest(str(httpx.URL(page_url).join(candidates[0][1])))
        return None

    async def parse_detail(self, item: RawOpportunity, client: httpx.AsyncClient) -> RawOpportunity:
        """Optional enrichment from the detail page (summary/eligibility)."""
        try:
            resp = await client.get(item.opportunity_url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return item
        soup = BeautifulSoup(resp.text, "lxml")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 80]
        if paragraphs and not item.summary:
            item.summary = paragraphs[0][:1000]
        text = soup.get_text("\n", strip=True)
        elig = re.search(r"eligibilit\w*\s*:?\s*(.{40,600})", text, re.IGNORECASE | re.DOTALL)
        if elig:
            item.eligibility = re.sub(r"\s+", " ", elig.group(1))[:600]
        return item
