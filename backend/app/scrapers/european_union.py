"""EU Funding & Tenders calls for proposals and calls for tenders.

The two public listing pages are React applications; their initial HTML does
not contain the result rows or usable pagination links.  The European
Commission documents the public SEDIA Search API that powers both pages, so
this scraper uses that first-party API instead of opening Chromium:

* calls for proposals: SEDIA types 1, 2 and 8
* calls for tenders: SEDIA type 0
* forthcoming/open only: status codes 31094501 and 31094502

The API is multilingual.  ``languages=["en"]`` is deliberately sent as a
JSON multipart part; omitting it returns one translated copy of the same
opportunity per language and creates apparent duplicates.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

PROPOSALS_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
    "opportunities/calls-for-proposals"
)
TENDERS_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
    "opportunities/calls-for-tenders?isExactMatch=true&order=DESC&"
    "pageNumber=1&pageSize=50&sortBy=startDate"
)
API_BASE = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
PAGE_SIZE = 100

PROPOSAL_TYPES = frozenset({"1", "2", "8"})
TENDER_TYPE = "0"
ACTIVE_STATUS_CODES = frozenset({"31094501", "31094502"})
STATUS_LABELS = {"31094501": "forthcoming", "31094502": "open"}


def api_url(page: int = 1, page_size: int = PAGE_SIZE) -> str:
    params = (
        ("apiKey", "SEDIA"),
        ("text", "***"),
        ("pageSize", str(page_size)),
        ("pageNumber", str(page)),
    )
    return f"{API_BASE}?{urlencode(params)}"


def _with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = urlencode([
        (key, str(page) if key.lower() == "pagenumber" else value)
        for key, value in pairs
    ])
    return urlunparse(parsed._replace(query=query))


def _first(metadata: dict, key: str) -> object:
    value = metadata.get(key)
    if isinstance(value, list):
        return value[0] if value else ""
    return value if value is not None else ""


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return " ".join(text.split())


def _iso_date(value: object) -> str:
    """Normalize SEDIA's ISO and DD-MM-YYYY timestamps to YYYY-MM-DD."""
    raw = str(value or "").strip()
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return "-".join(iso.groups())
    day_first = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})(?:\s|$)", raw)
    if day_first:
        day, month, year = day_first.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def _json_object(value: object) -> dict:
    raw = _first({"value": value}, "value")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: object) -> list:
    raw = _first({"value": value}, "value")
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def _tender_deadline(metadata: dict) -> str:
    """Latest lot submission deadline, then the explicit EXA registration date.

    A multi-lot notice remains actionable while any lot remains open, so the
    latest lot deadline is the correct notice-level closing date.  Planned
    publication dates are intentionally not treated as application deadlines.
    """
    dates: list[str] = []
    lots = _json_object(_first(metadata, "lots"))
    for lot in lots.get("procurementProjectLots", []):
        if not isinstance(lot, dict):
            continue
        process = lot.get("tenderingTerms", {}).get("tenderingProcess", {})
        if not isinstance(process, dict):
            continue
        period = process.get("tenderSubmissionDeadlinePeriod", {})
        if isinstance(period, dict):
            parsed = _iso_date(period.get("endDate"))
            if parsed:
                dates.append(parsed)
    if dates:
        return max(dates)
    return _iso_date(_first(metadata, "cftEXARegistrationDeadline"))


def _authority(metadata: dict) -> str:
    authorities = _json_list(_first(metadata, "cftLeadContractingAuthorityCode"))
    for authority in authorities:
        if isinstance(authority, dict) and _clean(authority.get("name")):
            return _clean(authority["name"])
    return "European Commission"


def _tender_summary(metadata: dict) -> str:
    parts: list[str] = []
    direct = _clean(_first(metadata, "description"))
    if direct:
        parts.append(direct)
    lots = _json_object(_first(metadata, "lots"))
    for lot in lots.get("procurementProjectLots", []):
        if not isinstance(lot, dict):
            continue
        project = lot.get("procurementProject", {})
        if not isinstance(project, dict):
            continue
        description = _clean(project.get("description"))
        if description and description not in parts:
            parts.append(description)
    return " ".join(parts)[:5000]


def _detail_url(metadata: dict, type_code: str, reference: str) -> str:
    if type_code in PROPOSAL_TYPES:
        identifier = _clean(_first(metadata, "identifier"))
        if identifier:
            return (
                "https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
                f"screen/opportunities/topic-details/{quote(identifier, safe='')}"
            )
    direct = _clean(_first(metadata, "url"))
    if direct and not direct.endswith(".json"):
        return direct
    if type_code == TENDER_TYPE and reference:
        return (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
            f"screen/opportunities/tender-details/{quote(reference, safe='')}"
        )
    return PROPOSALS_URL if type_code in PROPOSAL_TYPES else TENDERS_URL


@register
class EuropeanUnionScraper(BaseScraper):
    name = "european_union"
    display_name = "European Union"
    website = PROPOSALS_URL
    start_url = api_url()
    curated = True
    enrich_details = False
    stale_page_streak_override = 0

    _query = {
        "bool": {
            "must": [
                {"terms": {"type": ["0", "1", "2", "8"]}},
                {"terms": {"status": ["31094501", "31094502"]}},
            ]
        }
    }
    _languages = ["en"]
    _sort = {"field": "startDate", "order": "DESC"}

    def __init__(self) -> None:
        super().__init__()
        self.start_url = api_url()
        self._total: int | None = None
        self._last_count = 0
        self._page_size = PAGE_SIZE
        self._seen_record_keys: set[str] = set()

    async def _fetch(self, client: httpx.AsyncClient, req: PageRequest) -> str | None:
        """POST the API's required typed multipart fields with normal retries."""
        files = {
            "query": (None, json.dumps(self._query, separators=(",", ":")), "application/json"),
            "languages": (None, json.dumps(self._languages), "application/json"),
            "sort": (None, json.dumps(self._sort, separators=(",", ":")), "application/json"),
        }
        async with self._semaphore:
            for attempt in range(1, settings.max_retries + 1):
                elapsed = time.monotonic() - self._last_request
                if elapsed < settings.rate_limit_delay:
                    await asyncio.sleep(settings.rate_limit_delay - elapsed)
                self._last_request = time.monotonic()
                try:
                    response = await client.post(
                        req.url,
                        files=files,
                        headers={"Accept": "application/json"},
                    )
                    response.raise_for_status()
                    return response.text
                except httpx.HTTPError as exc:
                    wait = settings.retry_backoff ** attempt
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 429:
                        wait = max(wait, 20.0 * attempt)
                    log.warning(
                        "[%s] EU API fetch failed (%s%s) attempt %s/%s on %s "
                        "— retrying in %.1fs",
                        self.name,
                        exc.__class__.__name__,
                        f" HTTP {status}" if status else "",
                        attempt,
                        settings.max_retries,
                        req.url,
                        wait,
                    )
                    await asyncio.sleep(wait)
        log.error("[%s] giving up on %s after %s attempts", self.name, req.url, settings.max_retries)
        return None

    def parse_listing(self, payload_text: str, page_url: str) -> list[RawOpportunity]:
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            log.error("[%s] SEDIA API returned invalid JSON", self.name)
            self._last_count = 0
            return []

        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            log.error("[%s] SEDIA API response has no results list", self.name)
            self._last_count = 0
            return []

        total = payload.get("totalResults")
        self._total = int(total) if str(total).isdigit() else None
        self._last_count = len(rows)
        query = dict(parse_qsl(urlparse(page_url).query, keep_blank_values=True))
        try:
            self._page_size = max(1, int(query.get("pageSize", PAGE_SIZE)))
            current_page = max(1, int(query.get("pageNumber", 1)))
        except (TypeError, ValueError):
            self._page_size, current_page = PAGE_SIZE, 1
        if current_page == 1:
            # The API can return the same notice more than once (for example a
            # tender record and a language/index variant sharing one public
            # detail URL), including across page boundaries.  Reset per crawl,
            # then deduplicate on both source identity and final public URL.
            self._seen_record_keys.clear()

        items: list[RawOpportunity] = []
        rejected = 0
        for row in rows:
            if not isinstance(row, dict):
                rejected += 1
                continue
            metadata = row.get("metadata", {})
            if not isinstance(metadata, dict):
                rejected += 1
                continue

            language = _clean(row.get("language") or _first(metadata, "language")).lower()
            type_code = _clean(_first(metadata, "type"))
            status_code = _clean(_first(metadata, "status"))
            if language != "en" or status_code not in ACTIVE_STATUS_CODES:
                rejected += 1
                continue
            if type_code not in PROPOSAL_TYPES and type_code != TENDER_TYPE:
                rejected += 1
                continue

            title = _clean(_first(metadata, "title"))
            reference = _clean(row.get("reference") or _first(metadata, "REFERENCE"))
            identifier = _clean(_first(metadata, "identifier")) or reference
            detail_url = _detail_url(metadata, type_code, reference)
            identity_keys = {
                f"id:{type_code}:{identifier}".lower(),
                f"url:{detail_url}".lower(),
            }
            if (not title or not identifier
                    or bool(identity_keys & self._seen_record_keys)):
                rejected += 1
                continue
            self._seen_record_keys.update(identity_keys)

            is_tender = type_code == TENDER_TYPE
            if is_tender:
                deadline = _tender_deadline(metadata)
                organization = _authority(metadata)
                summary = _tender_summary(metadata)
                amount = _clean(_first(metadata, "cftEstimatedTotalProcedureValue"))
                record_type = "tender"
                category = Category.TENDER
                listing = TENDERS_URL
            else:
                deadline = _iso_date(_first(metadata, "deadlineDate"))
                organization = "European Commission"
                summary = _clean(
                    _first(metadata, "description")
                    or _first(metadata, "descriptionByte")
                    or _first(metadata, "callTitle")
                )[:5000]
                amount = _clean(_first(metadata, "budgetOverview"))
                record_type = "call_for_proposals"
                category = Category.PROPOSAL
                listing = PROPOSALS_URL

            items.append(RawOpportunity(
                title=title[:500],
                organization=organization[:512],
                funding_type="Tender" if is_tender else "Call for Proposals",
                deadline_raw=deadline,
                website=listing,
                opportunity_url=detail_url,
                summary=summary,
                funding_amount=amount[:256],
                source_website=self.display_name,
                category_hint=category,
                dayfirst=False,
                record_type=record_type,
                source_status=status_code,
            ))

        log.info(
            "[%s] page=%s kept=%s rejected=%s API total=%s; English only, "
            "proposals+tenders, forthcoming/open only",
            self.name,
            current_page,
            len(items),
            rejected,
            self._total,
        )
        return items

    def next_page(self, payload_text: str, page_url: str, page_number: int) -> PageRequest | None:
        query = dict(parse_qsl(urlparse(page_url).query, keep_blank_values=True))
        try:
            current_page = max(1, int(query.get("pageNumber", page_number)))
        except (TypeError, ValueError):
            current_page = page_number
        if self._last_count < self._page_size:
            return None
        if self._total is not None and current_page * self._page_size >= self._total:
            return None
        return PageRequest(_with_page(page_url, current_page + 1))
