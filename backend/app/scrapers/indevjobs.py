"""IndevJobs funding scraper (https://indevjobs.org/funding).

React app (empty HTML shell) — rendered via Playwright.

Why this produced nothing, and what it reads now
------------------------------------------------
The last rendered capture (data/debug/indevjobs_page1.html, 2026-09-07) shows
the listing did render: two cards, "2 Active Grants · 2 Total Available". The
parser found zero of them because the detail links had moved from
/funding/<slug> to /funding-opportunity/<slug>, and the old pattern required
"/funding/" followed immediately by the slug. The page loaded, the rows were
there, and every one was skipped — PARSE_ZERO with a working source.

Each card, as rendered:

    <a href="/funding-opportunity/<slug>"><h3>TITLE</h3></a>   <span>Open</span>
    <span>ORGANISATION</span> <span>Deadline: Jan 11, 2027</span>
    <a href="/funding-opportunity/<slug>">View Details →</a>

Pagination: there is none. The page lists every open call at once (the
counter says how many), and https://indevjobs.org/funding is the canonical
URL. The old scraper appended ?page=2, ?page=3 ... blindly; an SPA ignores
that parameter and re-serves the same cards, so each run paid for a second
browser render only for the repeat-guard to stop it. That fallback is gone;
a real rel=next link is still followed if the site ever adds one.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

# /funding-opportunity/<slug> today; /funding/<slug> is what it used to be.
_DETAIL = re.compile(r"/funding(?:-opportunit(?:y|ies))?/[a-z0-9][a-z0-9-]{7,}", re.IGNORECASE)

_DEADLINE_NEAR = re.compile(
    r"(deadline|closing|apply by|due)[^\w]{0,10}[^\d]{0,20}"
    r"(\d{1,2}\s+\w{3,9},?\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE,
)
_STATUS_WORDS = {"open", "upcoming", "closed", "closing soon", "new"}


def _organisation(card) -> str:
    """The span that sits beside the deadline in the card's meta row."""
    for span in card.find_all("span"):
        text = span.get_text(" ", strip=True)
        if not text or "deadline" in text.lower() or text.lower() in _STATUS_WORDS:
            continue
        sibling_text = span.parent.get_text(" ", strip=True).lower() if span.parent else ""
        if "deadline" in sibling_text and len(text) <= 200:
            return text
    return ""


@register
class IndevJobsScraper(BaseScraper):
    name = "indevjobs"
    display_name = "IndevJobs"
    website = "https://indevjobs.org"
    start_url = "https://indevjobs.org/funding"
    requires_js = True   # SPA — empty without a browser
    # /funding is a dedicated board: every card is a call, grant or contract
    # notice, and titles like "NOFO Innovative Health Practices ..." carry no
    # funding vocabulary. See services/opportunity_gate.py.
    curated = True

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not _DETAIL.search(href):
                continue
            url = href if href.startswith("http") else self.website + href
            title = a.get_text(" ", strip=True)
            # The avatar letter ("u") and "View Details →" share the URL; only
            # the anchor carrying the real title makes a row.
            if url in seen or len(title) < 12 or title.lower().startswith("view details"):
                continue
            seen.add(url)

            deadline_raw, org = "", ""
            card = a
            for _ in range(5):
                card = card.parent
                if card is None:
                    break
                m = _DEADLINE_NEAR.search(card.get_text(" ", strip=True))
                if m:
                    deadline_raw = m.group(2)
                    org = _organisation(card)
                    break

            items.append(RawOpportunity(
                title=title[:400],
                organization=org[:200],
                deadline_raw=deadline_raw[:64],
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
                dayfirst=False,   # "Jan 11, 2027" — month-first
            ))
        return items
