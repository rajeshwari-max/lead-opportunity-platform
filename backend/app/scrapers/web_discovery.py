"""Discover opportunities across the public web through a supported search API.

This is intentionally a discovery source, not a second ingestion system. It
uses Brave's public web index to find candidate pages, reads each candidate's
own page, and yields ordinary ``RawOpportunity`` objects. ScraperManager then
applies the same deadline, scope, category, vertical, brand, spam and duplicate
rules used for every first-party source.

The source is registered only when ``LOP_WEB_DISCOVERY_ENABLED=true``. A search
API key belongs in backend/.env and is never sent to candidate websites.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from collections import Counter
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import (
    BaseScraper,
    PageRequest,
    ProgressCallback,
    _playwright_available,
)
from app.scrapers.registry import register
from app.services.deadline_parser import DeadlineParser
from app.services.opportunity_gate import is_opportunity

log = logging.getLogger("scraper")

_API_URL = "https://api.search.brave.com/res/v1/web/search"
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref",
    "source", "campaign",
}
_BLOCKED_HOST_SUFFIXES = (
    "facebook.com", "instagram.com", "linkedin.com", "reddit.com",
    "tiktok.com", "x.com", "twitter.com", "youtube.com",
)
_DATE_SHAPES = (
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
)
_DEADLINE = re.compile(
    r"(?:application\s+deadline|submission\s+deadline|deadline|closing\s+date|"
    r"closes?(?:\s+on)?|apply\s+by|applications?\s+(?:close|due)|due\s+date|"
    r"last\s+date|expires?(?:\s+on)?)\s*[:\-–]?\s*(" + _DATE_SHAPES + r")",
    re.IGNORECASE,
)
_JSON_DEADLINE = re.compile(
    r'"(?:applicationDeadline|deadline|validThrough|endDate|dateClosing)"\s*:\s*"([^"]{4,80})"',
    re.IGNORECASE,
)
_ROLLING_DEADLINE = re.compile(
    r"(?:applications?|submissions?|deadline)[^.!?]{0,90}"
    r"(?:rolling(?:\s+basis)?|open[\s-]?ended|no\s+(?:fixed\s+)?deadline|"
    r"until\s+filled|continuous)"
    r"|(?:rolling(?:\s+basis)?|open[\s-]?ended|no\s+(?:fixed\s+)?deadline|"
    r"until\s+filled|continuous)[^.!?]{0,90}"
    r"(?:applications?|submissions?|deadline)",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")
_COUNTRIES = (
    "India", "Bangladesh", "Nepal", "Bhutan", "Sri Lanka", "Maldives",
    "Pakistan", "Afghanistan",
)


def _clean(value: str, limit: int = 0) -> str:
    text = _SPACE.sub(" ", value or "").strip()
    return text[:limit] if limit else text


def _normalise_url(raw: str) -> str:
    """Canonicalise a search-result URL enough to deduplicate tracking variants."""
    try:
        parts = urlsplit((raw or "").strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in _TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _safe_public_url(raw: str) -> bool:
    """Reject local/private destinations before EC2 fetches a search result."""
    url = _normalise_url(raw)
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        return False
    if any(host == suffix or host.endswith("." + suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    )


def _category_hint(text: str) -> tuple[Category | None, str]:
    haystack = (text or "").casefold()
    if re.search(r"\b(rfq|request for quotation|quotation request)\b", haystack):
        return Category.RFP, "rfq"
    if re.search(r"\b(rfp|request for proposals?|request for services?)\b", haystack):
        return Category.RFP, "rfp"
    if re.search(r"\b(tender|invitation to bid|procurement notice|\bitb\b)\b", haystack):
        return Category.TENDER, "tender"
    if re.search(r"\b(call for proposals?|\bcfp\b|proposal submissions?)\b", haystack):
        return Category.PROPOSAL, "call_for_proposals"
    if re.search(r"\b(grants?|funding opportunity|call for applications?)\b", haystack):
        return Category.GRANT, "grant"
    return None, ""


def _geography(text: str) -> tuple[str, str]:
    found = [country for country in _COUNTRIES
             if re.search(rf"\b{re.escape(country)}\b", text or "", re.IGNORECASE)]
    if len(found) == 1:
        return found[0], "South Asia"
    if found or re.search(r"\bSouth Asia(n)?\b", text or "", re.IGNORECASE):
        return "", "South Asia"
    if re.search(r"\b(global|worldwide|international applicants?)\b", text or "", re.IGNORECASE):
        return "", "Global"
    return "", ""


def build_queries(today: date | None = None) -> list[str]:
    """Build bounded searches covering all seven brands and CMS Devsol themes."""
    today = today or date.today()
    call_terms = (
        '("call for proposals" OR "request for proposals" OR RFP OR RFQ '
        'OR tender OR "funding opportunity" OR grant)'
    )
    themes = (
        '(health OR nutrition OR WASH OR "health systems")',              # Swasti / CMS Health
        '(livelihood OR agriculture OR smallholder OR "food systems")',    # Vrutti / CMS Livelihood
        '(climate OR sustainability OR ESG OR biodiversity)',              # Green Foundation / CMS ESG
        '(research OR evaluation OR "data collection" OR evidence)',       # CMS E4C
        '("worker wellbeing" OR "social protection" OR labour OR gender)', # Upfront / Setu
        '("innovative finance" OR "social business" OR resilience OR collaboration)',
    )
    geographies = (
        "India",
        '("South Asia" OR Bangladesh OR Nepal OR Bhutan OR "Sri Lanka" OR Maldives OR Pakistan)',
        '(global OR international)',
    )
    # A year term looked precise but hid rolling calls and pages whose title
    # did not repeat their deadline year. Freshness and the detail-page
    # deadline gate are safer controls.
    suffix = '("open" OR deadline OR "apply by") -jobs -careers -awarded -closed -expired'
    broad = [f"{call_terms} {theme} {geo} {suffix}"
             for geo in geographies for theme in themes]
    families = (
        '(grant OR "funding opportunity" OR "call for applications")',
        '("request for proposals" OR RFP OR RFQ OR tender OR procurement)',
        '("call for proposals" OR "expression of interest" OR EOI OR fellowship OR challenge)',
    )
    # Add one narrower, rotated category family for every theme/geography.
    # Rotation avoids spending the whole request budget on one opportunity
    # type or geography before the others receive coverage.
    focused = [
        f"{families[(theme_index + geo_index) % len(families)]} {theme} {geo} {suffix}"
        for geo_index, geo in enumerate(geographies)
        for theme_index, theme in enumerate(themes)
    ]
    # If an operator raises max_queries beyond 36, expose the remaining
    # category-family combinations in a deterministic, balanced order.
    expanded = [
        f"{family} {theme} {geo} {suffix}"
        for family in families for geo in geographies for theme in themes
    ]
    built = [*broad, *focused, *expanded]
    extras = [q.strip() for q in settings.web_discovery_extra_queries.split("|") if q.strip()]
    # Explicit operator-provided searches get first claim on the configured
    # budget; built-ins fill the remainder and keep deterministic order.
    unique: list[str] = []
    seen: set[str] = set()
    for query in [*extras, *built]:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(query)
    return unique[:max(1, settings.web_discovery_max_queries)]


class SearchBudgetExhausted(RuntimeError):
    """The configured per-run or calendar-month Search API cap was reached."""


class WebDiscoveryScraper(BaseScraper):
    name = "web_discovery"
    display_name = "Whole Web Discovery"
    website = "https://search.brave.com/"
    start_url = _API_URL
    curated = False
    # Query batches are independent, not descending pages of one listing; an
    # all-duplicate query must not stop the remaining brand/vertical searches.
    stale_page_streak_override = 0

    def __init__(self) -> None:
        super().__init__()
        self._fetch_limit = asyncio.Semaphore(
            max(1, min(12, settings.web_discovery_fetch_concurrency)))
        self._js_render_lock = asyncio.Semaphore(1)
        self._run_search_requests = 0
        self._js_rendered = 0
        self.last_probe: dict = {
            "fetch_mode": "search_api+http+bounded_playwright",
            "attempts": 0,
            "final_url": _API_URL,
            "response_bytes": 0,
            "body_sample": "",
        }

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """Unused: discovery has a JSON search stage followed by detail pages."""
        return []

    @property
    def _usage_path(self) -> Path:
        return Path(settings.log_dir).parent / "data" / "web_discovery_usage.json"

    def _read_usage(self) -> dict:
        month = date.today().strftime("%Y-%m")
        try:
            data = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        if data.get("month") != month:
            data = {"month": month, "requests": 0}
        data["requests"] = max(0, int(data.get("requests") or 0))
        return data

    def _reserve_search_request(self) -> dict:
        """Count every HTTP attempt before it is sent, including retries."""
        if self._run_search_requests >= max(1, settings.web_discovery_request_budget_per_run):
            raise SearchBudgetExhausted("per-run Search API request budget reached")
        usage = self._read_usage()
        if usage["requests"] >= max(1, settings.web_discovery_monthly_request_limit):
            raise SearchBudgetExhausted("monthly Search API request budget reached")
        self._run_search_requests += 1
        usage["requests"] += 1
        usage["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = self._usage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        temporary.replace(path)
        return usage

    async def _search(
        self, client: httpx.AsyncClient, query: str, offset: int = 0,
    ) -> tuple[list[dict], bool]:
        params = {
            "q": query,
            "count": max(1, min(20, settings.web_discovery_results_per_query)),
            "offset": max(0, min(9, offset)),
            "search_lang": "en",
            "safesearch": "strict",
            "spellcheck": "true",
            "result_filter": "web",
            "extra_snippets": "true",
        }
        freshness = settings.web_discovery_freshness.strip()
        if freshness:
            params["freshness"] = freshness
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": settings.brave_search_api_key,
        }
        last_error: Exception | None = None
        for attempt in range(1, settings.max_retries + 1):
            self._reserve_search_request()
            self.last_probe["attempts"] += 1
            try:
                response = await client.get(_API_URL, params=params, headers=headers)
                status = response.status_code
                self.last_probe.setdefault("first_http_status", status)
                self.last_probe["last_http_status"] = status
                self.last_probe["response_bytes"] += len(response.content)
                response.raise_for_status()
                data = response.json()
                results = list((data.get("web") or {}).get("results") or [])
                more = bool((data.get("query") or {}).get("more_results_available"))
                return results, more
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                wait = max(settings.retry_backoff ** attempt,
                           20.0 * attempt if status == 429 else 0.0)
                log.warning("[%s] search request failed (%s) attempt %s/%s; retrying in %.1fs",
                            self.name, exc, attempt, settings.max_retries, wait)
                if attempt < settings.max_retries:
                    await asyncio.sleep(wait)
        raise RuntimeError(f"Brave Search API failed after {settings.max_retries} attempts: {last_error}")

    @staticmethod
    def _page_parts(html: str, fallback_title: str, fallback_description: str) -> tuple[str, str, str, str]:
        if not html:
            return (_clean(fallback_title, 500), _clean(fallback_description, 1800), "", "")
        soup = BeautifulSoup(html[:2_000_000], "lxml")
        site_name = ""
        descriptions: list[str] = [fallback_description]
        for selector, attr in (
            ('meta[property="og:site_name"]', "content"),
            ('meta[name="application-name"]', "content"),
        ):
            node = soup.select_one(selector)
            if node and node.get(attr):
                site_name = _clean(node.get(attr, ""), 250)
                break
        for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
            node = soup.select_one(selector)
            if node and node.get("content"):
                descriptions.append(node.get("content", ""))
        title = ""
        for node in (soup.select_one('meta[property="og:title"]'), soup.find("h1"), soup.find("title")):
            if node:
                candidate = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
                candidate = _clean(candidate or "", 500)
                if len(candidate) >= 12:
                    title = candidate
                    break
        title = title or _clean(fallback_title, 500)
        structured_text = " ".join(
            script.get_text(" ", strip=True)
            for script in soup.select('script[type="application/ld+json"]')
        )
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
            tag.decompose()
        page_text = _clean(f"{structured_text} {soup.get_text(' ', strip=True)}", 80_000)
        summary = _clean(" ".join(part for part in descriptions if part), 1800)
        return title, summary, page_text, site_name

    @staticmethod
    def _deadline(text: str) -> tuple[str, date | None, bool]:
        parser = DeadlineParser()
        candidates = [match.group(1) for match in _DEADLINE.finditer(text or "")]
        candidates.extend(match.group(1) for match in _JSON_DEADLINE.finditer(text or ""))
        future: list[tuple[date, str]] = []
        for raw in candidates:
            parsed = parser.parse(raw, dayfirst=True)
            if parsed is not None and parsed >= date.today():
                future.append((parsed, raw))
        if future:
            parsed, raw = min(future, key=lambda pair: pair[0])
            return raw, parsed, False
        # A bare "ongoing" elsewhere on a long page can describe a project,
        # partnership or news story. Only accept it when the sentence ties the
        # phrase to applications/submissions/the deadline.
        rolling = bool(_ROLLING_DEADLINE.search(text or ""))
        return ("Rolling" if rolling else ""), None, rolling

    @staticmethod
    async def _fetch_public_page(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, str]:
        """Fetch a candidate while validating every redirect destination.

        ``follow_redirects=True`` is unsafe for URLs supplied by a search
        result: a public URL can redirect EC2 to localhost or instance
        metadata after the initial check. Walk a small redirect chain by hand
        and apply the public-URL gate before every request.
        """
        current = url
        for _ in range(6):
            if not _safe_public_url(current):
                raise ValueError(f"unsafe redirect destination: {current}")
            response = await client.get(
                current,
                headers={
                    "User-Agent": settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location") or ""
                if not location:
                    response.raise_for_status()
                    return response, current
                current = _normalise_url(urljoin(current, location))
                continue
            response.raise_for_status()
            return response, current
        raise httpx.TooManyRedirects("candidate exceeded five redirects")

    async def _candidate(
        self,
        client: httpx.AsyncClient,
        result: dict,
        counters: Counter,
    ) -> RawOpportunity | None:
        raw_url = result.get("url") or ""
        url = _normalise_url(raw_url)
        if not _safe_public_url(url):
            counters["unsafe_or_unsupported_url"] += 1
            return None
        search_title = _clean(result.get("title") or "", 500)
        snippets = [result.get("description") or ""]
        snippets.extend(result.get("extra_snippets") or [])
        search_description = _clean(" ".join(snippets), 1800)

        html = ""
        async with self._fetch_limit:
            try:
                response, url = await self._fetch_public_page(client, url)
                content_type = (response.headers.get("content-type") or "").lower()
                if "html" in content_type or not content_type:
                    html = response.text
                url = _normalise_url(url) or url
            except (httpx.HTTPError, ValueError):
                counters["candidate_fetch_failed"] += 1
                # Search snippets can still be sufficient for a PDF or a site
                # that refuses bots, but the deadline/page gates below remain.

        title, meta_summary, page_text, site_name = self._page_parts(
            html, search_title, search_description)
        combined = _clean(" ".join((title, search_description, meta_summary, page_text)), 90_000)
        deadline_raw, parsed_deadline, rolling = self._deadline(combined)
        # Some public pages return only an empty JavaScript shell to an HTTP
        # client. Render a small, strictly bounded number in Chromium, one at a
        # time, and re-run the same page/deadline gates on the rendered DOM.
        if (
            settings.web_discovery_js_render_enabled
            and parsed_deadline is None
            and not rolling
            and self._js_rendered < max(0, settings.web_discovery_js_render_limit_per_run)
            and _playwright_available()
        ):
            async with self._js_render_lock:
                if self._js_rendered < max(0, settings.web_discovery_js_render_limit_per_run):
                    self._js_rendered += 1
                    counters["js_render_attempted"] += 1
                    rendered = await self._fetch_rendered(PageRequest(url))
                    if rendered:
                        rendered_title, rendered_summary, rendered_text, rendered_site = (
                            self._page_parts(rendered, title, meta_summary)
                        )
                        title = rendered_title or title
                        meta_summary = rendered_summary or meta_summary
                        page_text = rendered_text or page_text
                        site_name = rendered_site or site_name
                        combined = _clean(
                            " ".join((title, search_description, meta_summary, page_text)),
                            90_000,
                        )
                        deadline_raw, parsed_deadline, rolling = self._deadline(combined)
                        if parsed_deadline is not None or rolling:
                            counters["js_render_recovered"] += 1
                        else:
                            counters["js_render_no_deadline"] += 1
                    else:
                        counters["js_render_failed"] += 1
        if settings.web_discovery_require_deadline and parsed_deadline is None and not rolling:
            counters["no_readable_active_deadline"] += 1
            return None

        category, record_type = _category_hint(f"{title} {search_description} {meta_summary}")
        keep, _ = is_opportunity(
            title, _clean(f"{search_description} {meta_summary}", 1800), url,
            str(category.value if category else ""), curated=False,
        )
        if not keep:
            counters["not_an_opportunity"] += 1
            return None

        parts = urlsplit(url)
        hostname = (parts.hostname or "").removeprefix("www.")
        organisation = site_name or hostname
        country, region = _geography(combined)
        summary = _clean(f"{search_description} {meta_summary}", 1800)
        if not summary:
            summary = _clean(page_text, 1800)
        return RawOpportunity(
            title=title,
            organization=organisation,
            country=country,
            region=region,
            deadline_raw=deadline_raw,
            website=f"{parts.scheme}://{parts.netloc}/",
            opportunity_url=url,
            summary=summary,
            location=country or region,
            source_website=self.display_name,
            category_hint=category,
            record_type=record_type,
            source_status="open" if parsed_deadline or rolling else "",
            assume_active=rolling,
            dayfirst=True,
        )

    def _write_report(self, report: dict) -> None:
        try:
            path = Path(settings.log_dir).parent / "data" / "debug" / "web_discovery_last_run.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.warning("[%s] could not write discovery report", self.name, exc_info=True)

    async def crawl(
        self,
        stop_event: asyncio.Event,
        pause_event: asyncio.Event,
        progress: ProgressCallback,
    ) -> AsyncIterator[list[RawOpportunity]]:
        if not settings.brave_search_api_key.strip():
            raise RuntimeError(
                "Whole Web Discovery is enabled but LOP_BRAVE_SEARCH_API_KEY is empty"
            )

        # Scraper objects are registered once and reused, so per-run counters
        # must be reset here rather than only in __init__.
        self._run_search_requests = 0
        self._js_rendered = 0
        queries = build_queries()
        seen_urls: set[str] = set()
        counters: Counter = Counter()
        report = {"queries": queries, "counts": {}, "query_results": []}
        request_page = 0
        budget_exhausted = ""
        max_pages = max(1, min(10, settings.web_discovery_max_pages_per_query))
        planned_requests = min(
            len(queries) * max_pages,
            max(1, settings.web_discovery_request_budget_per_run),
        )
        log.info(
            "[%s] high-coverage run: %s complementary queries, up to %s pages "
            "each (%s API requests planned); full public index=%s",
            self.name, len(queries), max_pages, planned_requests,
            not bool(settings.web_discovery_freshness.strip()),
        )
        report["coverage_plan"] = {
            "query_count": len(queries),
            "max_pages_per_query": max_pages,
            "planned_request_ceiling": planned_requests,
            "freshness": settings.web_discovery_freshness.strip() or "full_index",
        }
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            for query_index, query in enumerate(queries, start=1):
                if stop_event.is_set():
                    break
                for offset in range(max_pages):
                    if stop_event.is_set():
                        break
                    await pause_event.wait()
                    request_page += 1
                    await progress("page_start", {
                        "source": self.name, "page": request_page, "url": _API_URL,
                    })
                    try:
                        results, more_available = await self._search(client, query, offset)
                    except SearchBudgetExhausted as exc:
                        budget_exhausted = str(exc)
                        counters["budget_exhausted"] += 1
                        log.info("[%s] %s; discovery stopped cleanly", self.name, exc)
                        await progress("page_done", {
                            "source": self.name, "page": request_page, "found": 0,
                        })
                        break
                    except RuntimeError:
                        await progress("page_error", {"source": self.name, "page": request_page})
                        raise
                    counters["search_results"] += len(results)
                    fresh_results: list[dict] = []
                    for result in results:
                        url = _normalise_url(result.get("url") or "")
                        if not url or url in seen_urls:
                            counters["duplicate_search_url"] += 1
                            continue
                        seen_urls.add(url)
                        fresh_results.append(result)

                    resolved = await asyncio.gather(
                        *(self._candidate(client, result, counters) for result in fresh_results),
                        return_exceptions=True,
                    )
                    items: list[RawOpportunity] = []
                    for candidate in resolved:
                        if isinstance(candidate, Exception):
                            counters["candidate_parse_failed"] += 1
                            log.warning("[%s] candidate failed: %s", self.name, candidate)
                        elif candidate is not None:
                            items.append(candidate)
                    counters["yielded_candidates"] += len(items)
                    report["query_results"].append({
                        "query": query,
                        "query_index": query_index,
                        "offset": offset,
                        "returned": len(results),
                        "new_urls": len(fresh_results),
                        "accepted": len(items),
                        "more_results_available": more_available,
                    })
                    log.info(
                        "[%s] query %s/%s offset %s: %s results, %s new URLs, "
                        "%s active opportunities",
                        self.name, query_index, len(queries), offset, len(results),
                        len(fresh_results), len(items),
                    )
                    if items:
                        sample = " | ".join(item.title[:120] for item in items[:5])
                        self.last_probe["body_sample"] = _clean(
                            f"{self.last_probe.get('body_sample', '')} {sample}", 4000)
                        yield items
                    await progress("page_done", {
                        "source": self.name, "page": request_page, "found": len(items),
                    })
                    if not results or not more_available:
                        break
                if budget_exhausted:
                    break

        if request_page:
            await progress("pages_end", {"source": self.name, "page": request_page})
        usage = self._read_usage()
        counters["api_requests"] = self._run_search_requests
        counters["js_rendered"] = self._js_rendered
        report["budget"] = {
            "per_run_limit": settings.web_discovery_request_budget_per_run,
            "monthly_limit": settings.web_discovery_monthly_request_limit,
            "run_requests": self._run_search_requests,
            "month": usage["month"],
            "monthly_requests": usage["requests"],
            "stopped_reason": budget_exhausted,
        }
        report["counts"] = dict(sorted(counters.items()))
        self._write_report(report)
        log.info("[%s] discovery summary: %s", self.name, dict(counters))


if settings.web_discovery_enabled and settings.brave_search_api_key.strip():
    register(WebDiscoveryScraper)
