"""King Baudouin Foundation open calls, via the site's own search API.

The listing a person sees, https://kbs-frb.be/en/search?type_id=call, is a Vue
app: the served HTML has no rows, so the generic link collector found nothing
(PARSE_ZERO in the November 2026 health report). The page fills itself from

    /en/api/v1/elastic-search?...&filters[type_id][]=call
                             &filters[call_status][]=open

which answers plain JSON without cookies or a session. On 2026-11-07 it held
8 open calls out of 143 (the rest are closed). Each record carries its title,
the call's own page, an ISO ``call_deadline`` and a short description, so no
browser and no detail visit is needed.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

LISTING_URL = "https://kbs-frb.be/en/search?type_id=call"
API = "https://kbs-frb.be/en/api/v1/elastic-search"


def api_url(page: int = 1) -> str:
    params = [
        ("search", "null"), ("runtime_id", "0"), ("sort", "recent"),
        ("page", str(page)),
        ("filters[site_id][]", "kbs_be"),
        ("filters[type_id][]", "call"),
        ("filters[language][]", "en"),
        ("filters[call_status][]", "open"),
    ]
    return f"{API}?{urlencode(params)}"


def _page_of(url: str) -> int:
    try:
        return int(dict(parse_qsl(urlparse(url).query)).get("page", "1"))
    except ValueError:
        return 1


def _with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    pairs = [(k, str(page) if k == "page" else v)
             for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunparse(parsed._replace(query=urlencode(pairs)))


@register
class KingBaudouinScraper(BaseScraper):
    # Same key as the sources.json entry, so this bespoke scraper replaces the
    # generic one and the source keeps its run history.
    name = "kbs_frb"
    display_name = "King Baudouin Foundation"
    website = "https://kbs-frb.be"
    start_url = api_url()
    # Every record the API returns under call_status=open is an open call.
    curated = True
    enrich_details = False
    stale_page_streak_override = 0

    def __init__(self) -> None:
        super().__init__()
        self._num_pages = 1

    def parse_listing(self, payload: str, page_url: str) -> list[RawOpportunity]:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            log.error("[%s] KBS search API returned invalid JSON", self.name)
            self._num_pages = 0
            return []
        info = (data.get("info") or {}).get("page") or {}
        try:
            self._num_pages = int(info.get("num_pages") or 1)
        except (TypeError, ValueError):
            self._num_pages = 1
        records = (data.get("records") or {}).get("page") or []
        items: list[RawOpportunity] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if str(rec.get("call_status") or "").lower() != "open":
                continue                     # verify, don't trust the filter
            title = " ".join(str(rec.get("title") or "").split())
            url = str(rec.get("url") or "").strip()
            if not title or not url:
                continue
            items.append(RawOpportunity(
                title=title[:500],
                organization=self.display_name,
                summary=" ".join(str(rec.get("short_description") or "").split())[:1000],
                deadline_raw=str(rec.get("call_deadline") or "")[:10],
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
            ))
        return items

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        current = _page_of(page_url)
        if current < self._num_pages:
            return PageRequest(_with_page(page_url, current + 1))
        return None
