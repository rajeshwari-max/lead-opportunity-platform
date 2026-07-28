"""BaseScraper — the plugin contract every website scraper implements.

Adding a new website (FundsForNGOs, UNDP, UNICEF, World Bank, ...) requires ONLY:
    1. subclass BaseScraper, implement parse_listing() (+ optionally parse_detail(),
       next_page())
    2. decorate with @register
No existing code changes (Open/Closed Principle).
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.schemas.opportunity import RawOpportunity

log = logging.getLogger("scraper")
perf = logging.getLogger("performance")

ProgressCallback = Callable[[str, dict], Awaitable[None] | None]

_PLAYWRIGHT_AVAILABLE: bool | None = None


def _playwright_available() -> bool:
    """Cached check — lets prefer_js scrapers auto-upgrade to browser fetching."""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        import importlib.util
        _PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None
    return _PLAYWRIGHT_AVAILABLE


class PageResult:
    """One listing page's parse output + the request needed for the next page."""

    __slots__ = ("items", "next_request")

    def __init__(self, items: list[RawOpportunity], next_request: "PageRequest | None"):
        self.items = items
        self.next_request = next_request


class PageRequest:
    """A pending fetch: URL + method + payload.

    data: form-encoded body (ASP.NET postbacks) — json: JSON body (AJAX endpoints
    like FacetWP). Set at most one of the two.
    """

    __slots__ = ("url", "method", "data", "json")

    def __init__(
        self,
        url: str,
        method: str = "GET",
        data: dict | None = None,
        json: dict | None = None,
    ):
        self.url = url
        self.method = method
        self.data = data
        self.json = json


class BaseScraper(ABC):
    """Template-method crawler: fetch -> parse -> follow pagination until exhausted.

    Provides for every subclass:
      * retrying HTTP client with exponential backoff
      * per-source rate limiting (polite delay + concurrency semaphore)
      * automatic pagination loop with duplicate/empty-page stop conditions
      * pause/stop cooperation via asyncio events
    """

    name: str = "base"                 # unique registry key, e.g. "ngobox"
    display_name: str = "Base"
    website: str = ""                  # human-facing site URL
    start_url: str = ""                # first listing page
    requires_js: bool = False          # MUST render with a browser (skipped w/o Playwright)
    prefer_js: bool = False            # browser-render IF Playwright is installed,
                                       # else fall back to plain HTTP (for sites that
                                       # serve stale/partial pages to non-browsers)

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.concurrency_per_source)
        self._last_request = 0.0

    # ------------------------------------------------------------------ hooks
    @abstractmethod
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """Extract raw opportunities from one listing page."""

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        """Detect pagination from the page itself. Return None when done.
        Default: look for a rel=next link or an anchor whose text is 'next'/'»'."""
        def usable(href: str) -> bool:
            # ignore same-page anchors (carousel arrows) and JS pseudo-links
            return bool(href) and not href.startswith("#") and not href.lower().startswith("javascript")

        soup = BeautifulSoup(html, "lxml")
        link = soup.find("a", rel="next")
        if not (link and usable(link.get("href", ""))):
            link = None
            for a in soup.find_all("a", href=True):
                if a.get_text(strip=True).lower() in {"next", "next »", "»", ">", "next>"}                         and usable(a["href"]):
                    link = a
                    break
        if link:
            return PageRequest(httpx.URL(page_url).join(link["href"]).__str__())
        return None

    async def parse_detail(self, item: RawOpportunity, client: httpx.AsyncClient) -> RawOpportunity:
        """Optionally enrich an item from its detail page. Default: no-op."""
        return item

    # ------------------------------------------------------------------ engine
    async def crawl(
        self,
        stop_event: asyncio.Event,
        pause_event: asyncio.Event,
        progress: ProgressCallback,
    ) -> AsyncIterator[list[RawOpportunity]]:
        """Yield batches of RawOpportunity per page until pagination is exhausted."""
        request: PageRequest | None = PageRequest(self.start_url)
        page_number = 0
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",   # ask intermediaries for fresh content
                "Pragma": "no-cache",
            },
            timeout=settings.request_timeout,
            follow_redirects=True,
        ) as client:
            while request and page_number < settings.max_pages_safety_cap:
                if stop_event.is_set():
                    log.info("[%s] stop requested — aborting crawl", self.name)
                    return
                await pause_event.wait()  # cleared == paused

                page_number += 1
                fingerprint = f"{request.method}:{request.url}:{request.data}:{request.json}"
                if fingerprint in seen_urls:  # pagination loop guard
                    log.info("[%s] pagination loop detected at page %s — stopping", self.name, page_number)
                    return
                seen_urls.add(fingerprint)

                await progress("page_start", {"source": self.name, "page": page_number, "url": request.url})
                started = time.monotonic()
                html = await self._fetch(client, request)
                if html is None:  # retries exhausted — skip page, keep crawling
                    await progress("page_error", {"source": self.name, "page": page_number})
                    request = None
                    continue
                perf.info("%s page=%s fetched in %.2fs", self.name, page_number, time.monotonic() - started)

                if page_number == 1:  # keep a copy of page 1 for parser debugging
                    try:
                        debug_dir = settings.log_dir.parent / "data" / "debug"
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        (debug_dir / f"{self.name}_page1.html").write_text(html, encoding="utf-8")
                    except OSError:
                        pass

                try:
                    items = self.parse_listing(html, str(request.url))
                except Exception:
                    log.exception("[%s] parse error on page %s — skipping page", self.name, page_number)
                    items = []

                if not items:
                    try:  # keep the raw body — shows WHY it parsed to nothing
                        debug_dir = settings.log_dir.parent / "data" / "debug"
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        (debug_dir / f"{self.name}_page{page_number}_empty.txt").write_text(
                            html[:200_000], encoding="utf-8"
                        )
                    except OSError:
                        pass
                    log.info("[%s] empty page %s — assuming end of listings", self.name, page_number)
                    await progress("page_done", {"source": self.name, "page": page_number, "found": 0})
                    return

                yield items
                await progress("page_done", {"source": self.name, "page": page_number, "found": len(items)})
                request = self.next_page(html, str(request.url), page_number)
                if request is None:
                    await progress("pages_end", {"source": self.name, "page": page_number})

    async def _fetch(self, client: httpx.AsyncClient, req: PageRequest) -> str | None:
        """Rate-limited fetch with retry + exponential backoff. Never raises."""
        if self.requires_js or (self.prefer_js and _playwright_available()):
            return await self._fetch_rendered(req)
        if self.prefer_js:
            log.warning(
                "[%s] works best with browser rendering — install Playwright "
                "(pip install playwright && playwright install chromium) for fresh content",
                self.name,
            )
        async with self._semaphore:
            for attempt in range(1, settings.max_retries + 1):
                # polite spacing between requests to the same site
                elapsed = time.monotonic() - self._last_request
                if elapsed < settings.rate_limit_delay:
                    await asyncio.sleep(settings.rate_limit_delay - elapsed)
                self._last_request = time.monotonic()
                try:
                    if req.method == "POST":
                        if req.json is not None:
                            resp = await client.post(req.url, json=req.json)
                        else:
                            resp = await client.post(req.url, data=req.data)
                    else:
                        resp = await client.get(req.url)
                    resp.raise_for_status()
                    text = resp.text
                    head = text[:3000].lower()
                    if req.method == "GET" and (
                        'http-equiv="refresh"' in head and "please wait" in head
                    ):
                        # bot-check interstitial: wait it out once, then refetch
                        log.info("[%s] interstitial page detected — waiting it out", self.name)
                        await asyncio.sleep(8)
                        resp = await client.get(req.url)
                        resp.raise_for_status()
                        text = resp.text
                    return text
                except httpx.HTTPError as exc:
                    wait = settings.retry_backoff ** attempt
                    # Rate-limited (HTTP 429): back off much harder — the site is
                    # telling us to slow down, not that the page is broken.
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status == 429:
                        wait = max(wait, 20.0 * attempt)
                    log.warning(
                        "[%s] fetch failed (%s%s) attempt %s/%s on %s — retrying in %.1fs",
                        self.name, exc.__class__.__name__,
                        f" HTTP {status}" if status else "",
                        attempt, settings.max_retries, req.url, wait,
                    )
                    await asyncio.sleep(wait)
        log.error("[%s] giving up on %s after %s attempts", self.name, req.url, settings.max_retries)
        return None

    async def _fetch_rendered(self, req: PageRequest) -> str | None:
        """Fetch via headless Chromium for JavaScript-rendered sites (Playwright).

        Uses the SYNC Playwright API inside a worker thread: on Windows,
        uvicorn's selector event loop cannot spawn subprocesses (silent
        NotImplementedError), but a fresh thread gets a proactor loop that can.

        Playwright is optional: install with
            pip install playwright && playwright install chromium
        """
        try:
            import playwright  # noqa: F401  (lazy availability check)
        except ImportError:
            log.error(
                "[%s] requires JavaScript rendering but Playwright is not installed. "
                "Run: pip install playwright && playwright install chromium",
                self.name,
            )
            return None
        try:
            return await asyncio.to_thread(self._fetch_rendered_sync, req.url)
        except Exception as exc:
            log.error(
                "[%s] Playwright fetch failed on %s: %s: %s",
                self.name, req.url, type(exc).__name__, exc or "(no message)",
            )
            return None

    def _fetch_rendered_sync(self, url: str) -> str:
        """Blocking Playwright fetch — runs in its own thread (see above)."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=settings.user_agent)
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass  # slow trackers shouldn't fail the page — take what rendered

                # Bot-check interstitials ("Please Wait" + meta refresh) navigate
                # by themselves after ~5s — give them time, then settle again.
                for _ in range(2):
                    head = page.content()[:3000].lower()
                    if "http-equiv=\"refresh\"" in head or "please wait" in head:
                        page.wait_for_timeout(8_000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=15_000)
                        except Exception:
                            pass
                    else:
                        break
                return page.content()
            finally:
                browser.close()
