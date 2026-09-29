"""NIH grant opportunities from the current Explore NIH Opportunities tool.

NIH stopped publishing NOFOs through the old NIH Guide in FY2026.  The page
the public now uses is a React search tool:

    https://grants.nih.gov/funding/explore-nih-opportunities

Its served HTML has no opportunity rows.  The tool requests the NIH search API
below and pages it with ``from`` offsets.  Scraping ``/funding`` with the
generic link collector therefore harvested navigation, while rendering the
new page would still stop at its first 20 rows because its pager is made of
JavaScript buttons with no URLs.

The blank Funding Category selection shown in the UI is represented by
``activitycodes=all``.  It covers all five categories currently exposed by the
tool (Construction and Modernization; Research and Development; Research and
Development, Small Business; Research Training and Career Development; Small
Business), so they must not be crawled as separate, overlapping result sets.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from datetime import date
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

LISTING_URL = "https://grants.nih.gov/funding/explore-nih-opportunities"
API_BASE = "https://vtbgfslh0k.execute-api.us-east-1.amazonaws.com/prod/data"
PAGE_SIZE = 100

# Kept explicit so a future NIH UI change can be compared with the source
# contract.  Runtime coverage comes from activitycodes=all, not one request per
# category (which would overlap and create avoidable duplicates).
FUNDING_CATEGORIES = (
    "Construction and Modernization",
    "Research and Development",
    "Research and Development, Small Business",
    "Research Training and Career Development",
    "Small Business",
)


def api_url(offset: int = 0, page_size: int = PAGE_SIZE, today: date | None = None) -> str:
    """Build the same all-category, active-only query used by the NIH page."""
    end = (today or date.today()).strftime("%m%d%Y")
    params = (
        ("perpage", str(page_size)),
        ("sort", "reldate:desc"),
        ("from", str(offset)),
        ("type", "active"),
        ("parentic", "all"),
        ("primaryic", "all"),
        ("activitycodes", "all"),
        ("doctype", "all"),
        ("parentfoa", "all"),
        ("daterange", f"01011991-{end}"),
        ("clinicaltrials", "all"),
        ("fields", "all"),
        ("spons", "true"),
        ("query", ""),
        ("apptype", "all"),
        ("noticeSubject", "all"),
        ("category", "all"),
    )
    return f"{API_BASE}?{urlencode(params)}"


def _with_offset(url: str, offset: int) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = urlencode([
        (key, str(offset) if key.lower() == "from" else value)
        for key, value in pairs
    ])
    return urlunparse(parsed._replace(query=query))


def _clean(value: object) -> str:
    """Readable text from the API's plain or HTML-encoded values."""
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    return " ".join(BeautifulSoup(text, "lxml").get_text(" ", strip=True).split())


def _iso_date(value: object) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    return raw[:64]


def _detail_url(source: dict) -> str:
    grant_id = str(source.get("ggid") or "").strip()
    if grant_id:
        return f"https://simpler.grants.gov/opportunity/{grant_id}"
    # Some transition-era records have no Grants.gov numeric id.  Search the
    # official NIH tool by notice number: this remains on the requested page,
    # produces a usable result, and gives each record a distinct stable link.
    notice_number = str(source.get("docnum") or "").strip()
    if notice_number:
        return f"{LISTING_URL}?query={quote(notice_number)}&fields=title"
    return LISTING_URL


@register
class NIHOpportunitiesScraper(BaseScraper):
    name = "national_institutes_of_health"
    display_name = "National Institutes of Health"
    website = LISTING_URL
    # The human-facing URL is kept in ``website`` and the source manifest.  The
    # crawl starts at the data request that page itself makes so no browser is
    # needed and one scrape cannot leave Chrome processes behind.
    start_url = api_url()
    curated = True
    enrich_details = False
    stale_page_streak_override = 0

    def __init__(self) -> None:
        super().__init__()
        # Rebuild at run time so a long-lived API process does not retain the
        # date on which this module was first imported.
        self.start_url = api_url()
        self._total: int | None = None
        self._last_count = 0
        self._page_size = PAGE_SIZE

    def parse_listing(self, payload_text: str, page_url: str) -> list[RawOpportunity]:
        try:
            envelope = json.loads(payload_text)
        except (TypeError, ValueError):
            log.error("[%s] NIH API returned invalid JSON", self.name)
            self._last_count = 0
            return []

        if not isinstance(envelope, dict) or envelope.get("statusCode", 200) != 200:
            log.error("[%s] NIH API returned an unsuccessful envelope", self.name)
            self._last_count = 0
            return []

        data = envelope.get("data", envelope)
        hits_block = data.get("hits", {}) if isinstance(data, dict) else {}
        rows = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
        if not isinstance(rows, list):
            log.error("[%s] NIH API response has no hits.hits list", self.name)
            self._last_count = 0
            return []

        total = hits_block.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        self._total = (
            int(total)
            if isinstance(total, (int, str)) and str(total).isdigit()
            else None
        )
        self._last_count = len(rows)

        query = dict(parse_qsl(urlparse(page_url).query, keep_blank_values=True))
        try:
            self._page_size = max(1, int(query.get("perpage", PAGE_SIZE)))
            offset = int(query.get("from", 0))
        except (TypeError, ValueError):
            self._page_size, offset = PAGE_SIZE, 0

        items: list[RawOpportunity] = []
        rejected = 0
        for row in rows:
            source = row.get("_source", {}) if isinstance(row, dict) else {}
            if not isinstance(source, dict):
                rejected += 1
                continue

            source_status = str(source.get("type") or "").strip().lower()
            # The query is active-only, and this verifies the response rather
            # than assuming the server honoured it.
            if source_status != "active":
                rejected += 1
                continue

            title = _clean(source.get("title"))
            if not title:
                rejected += 1
                continue

            primary = _clean(source.get("primaryIC"))
            organization = self.display_name
            if primary and primary.upper() != "NIH":
                organization = f"{self.display_name} ({primary})"

            purpose = _clean(source.get("purpose"))
            activity_codes = [str(v).strip() for v in (source.get("ac") or []) if str(v).strip()]
            sponsors = [str(v).strip() for v in (source.get("sponsors") or [])
                        if str(v).strip() and str(v).strip().lower() != "none"]
            context = []
            if activity_codes:
                context.append(f"Activity codes: {', '.join(activity_codes)}.")
            if sponsors:
                context.append(f"Participating organizations: {', '.join(sponsors)}.")
            clinical = _clean(source.get("clinicaltrials")).replace("_", " ")
            if clinical:
                context.append(f"Clinical trials: {clinical}.")
            summary = " ".join([purpose, *context]).strip()

            items.append(RawOpportunity(
                title=title[:500],
                organization=organization,
                funding_type=_clean(source.get("doctype")) or "Grant",
                deadline_raw=_iso_date(source.get("expdate")),
                website=LISTING_URL,
                opportunity_url=_detail_url(source),
                summary=summary[:5000],
                source_website=self.display_name,
                category_hint=Category.GRANT,
                dayfirst=False,
                record_type="grant",
                source_status=source_status,
            ))

        log.info(
            "[%s] offset=%s kept=%s rejected=%s API total=%s; funding categories=%s",
            self.name, offset, len(items), rejected, self._total,
            len(FUNDING_CATEGORIES),
        )
        return items

    def next_page(self, payload_text: str, page_url: str, page_number: int) -> PageRequest | None:
        query = dict(parse_qsl(urlparse(page_url).query, keep_blank_values=True))
        try:
            offset = int(query.get("from", 0))
        except (TypeError, ValueError):
            offset = (page_number - 1) * self._page_size

        next_offset = offset + self._page_size
        if self._total is not None and next_offset >= self._total:
            return None
        if self._last_count < self._page_size:
            return None
        return PageRequest(_with_offset(page_url, next_offset))
