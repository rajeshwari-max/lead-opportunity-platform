"""FundsForNGOs scraper — via their open WordPress REST API.

The HTML site shows bot-check interstitials, but /wp-json/wp/v2/posts is open
and returns clean JSON (title, link, content with "Deadline: DD-MMM-YYYY",
excerpt). Posts are newest-first; combined with the platform's stale-page stop,
each run walks back only until everything is expired/known (~a few pages of
100), never the full multi-year archive.
"""
from __future__ import annotations

import html as htmllib
import json
import re

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

_DEADLINE = re.compile(
    r"deadline\s*:?\s*([0-3]?\d[-/ ]\w{3,9}[-/ ]\d{2,4})", re.IGNORECASE
)
_COUNTRY_IN_TITLE = re.compile(r"\(([^)]+)\)\s*$")
# The trailing parenthetical is a country about as often as it is the grant size
# — "Program in the US ($10,000)". Anything that looks like money, a year or a
# round number is rejected so it can't end up in the Country filter.
_NOT_A_COUNTRY = re.compile(
    r"[$€£₹¥%]|\b(usd|eur|gbp|inr|chf|aud|cad|nzd|zar)\b|\d{3,}|"
    r"^\d{4}(\s*[-–/]\s*\d{2,4})?$|^up\s+to\b|"
    r"\b(round|cohort|window|cycle|phase|edition|batch|deadline|apply|open)\b",
    re.IGNORECASE,
)
_PAGE_PARAM = re.compile(r"[?&]page=(\d+)")

PER_PAGE = 50
# A ceiling, not a target. Posts are newest-first and the walk normally ends on
# a short page long before this; the cap only stops a runaway if the API ever
# starts answering out-of-range pages with content instead of an error.
MAX_PAGES = 400

_VERTICAL_MAP = {
    "agriculture-food-nutrition": "Agriculture", "animals-wildlife": "Environment",
    "arts-culture": "Arts & Culture", "arts-culture-2": "Arts & Culture",
    "children": "Children", "civil-society": "Civil Society",
    "community-development": "Community Development",
    "community-development-2": "Community Development",
    "democracy-good-governance": "Governance", "economic-development": "Livelihood",
    "education": "Education", "education-2": "Education", "disability": "Disability",
    "employment-labor": "Livelihood", "environment": "Environment",
    "environment-2": "Environment", "health": "Health", "health-2": "Health",
    "hiv-and-aids": "Health", "housing-shelter": "Housing",
    "humanitarian-relief": "Humanitarian", "humanitarian-relief-2": "Humanitarian",
    "human-rights": "Human Rights", "information-technology": "Technology",
    "livelihood": "Livelihood", "media": "Media", "science": "Research",
    "individuals": "Individuals", "water-sanitation-hygiene-wash": "Water",
    "women-gender": "Women", "women-gender-2": "Women", "youth": "Youth",
}


@register
class FundsForNGOsScraper(BaseScraper):
    name = "fundsforngos"
    display_name = "FundsForNGOs"
    # Listing text names the funder/amount only sometimes; the WordPress post
    # behind each link is plain HTML and usually states both. Only rows still
    # missing one are fetched.
    enrich_details = True
    # 0 = walk to the end of the archive instead of stopping after 3 pages with
    # nothing new. This source is a clean JSON API (?page=N, 50 posts a page) and
    # terminates by itself on an empty page, but the default heuristic meant a
    # re-run quit inside the already-seen prefix and never reached deeper posts —
    # it had never gone past page 36 in 15 runs.
    stale_page_streak_override = 0
    website = "https://www2.fundsforngos.org"
    # per_page=50 keeps each API response ~2.5MB instead of ~5MB — the larger
    # payload intermittently hit the 30s timeout / rate limiter on repeat runs.
    # Pagination (?page=N) still walks every post, so nothing is missed.
    start_url = "https://www2.fundsforngos.org/wp-json/wp/v2/posts?per_page=50&page=1"

    def parse_listing(self, raw: str, page_url: str) -> list[RawOpportunity]:
        try:
            posts = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(posts, list):   # {"code": "rest_post_invalid_page_number"}
            return []

        items: list[RawOpportunity] = []
        for post in posts:
            title = htmllib.unescape(post.get("title", {}).get("rendered", "")).strip()
            link = post.get("link", "")
            if not title or not link:
                continue

            content = post.get("content", {}).get("rendered", "")[:4000]
            excerpt = post.get("excerpt", {}).get("rendered", "")
            deadline = _DEADLINE.search(content) or _DEADLINE.search(excerpt)

            summary = BeautifulSoup(excerpt, "lxml").get_text(" ", strip=True)
            summary = re.sub(r"^Deadline:\s*\S+\s*", "", summary)[:1000]

            country = ""
            m = _COUNTRY_IN_TITLE.search(title)
            if m and len(m.group(1)) < 40 and not _NOT_A_COUNTRY.search(m.group(1)):
                country = m.group(1).strip()

            vertical = ""
            slug = re.search(r"fundsforngos\.org/([a-z0-9-]+)/", link)
            if slug:
                vertical = _VERTICAL_MAP.get(slug.group(1), "")

            items.append(
                RawOpportunity(
                    title=title,
                    deadline_raw=(deadline.group(1) if deadline else "")[:64],
                    summary=summary,
                    country=country,
                    vertical=vertical,
                    opportunity_url=link,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,
                )
            )
        return items

    def next_page(self, raw: str, page_url: str, page_number: int) -> PageRequest | None:
        """API pagination: bump ?page=N. The crawl stops via empty page (API
        error past the end) or the stale-page streak (only old posts left)."""
        m = _PAGE_PARAM.search(page_url)
        current = int(m.group(1)) if m else 1

        # A short page is the end of the data, and saying so here is better than
        # relying on the NEXT request failing. The API answers past-the-end with
        # {"code": "rest_post_invalid_page_number"}, which parse_listing turns
        # into an empty page and BaseScraper reads as "end of listings" — that
        # works, but it spends one guaranteed-failed request per run and logs a
        # fetch error that looks like a fault.
        try:
            posts = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(posts, list):
            # {"code": "rest_post_invalid_page_number"} — a dict, not a list.
            # The first version tested `isinstance(posts, list) and len(...)`,
            # which is False for the error payload and therefore fell through to
            # requesting the NEXT page as well. Walking past the end of the data
            # asking for more of it.
            return None
        if len(posts) < PER_PAGE:
            return None

        if current >= MAX_PAGES:
            return None
        return PageRequest(re.sub(r"([?&])page=\d+", rf"\g<1>page={current + 1}",
                                  page_url))
