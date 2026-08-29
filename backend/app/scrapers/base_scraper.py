"""BaseScraper — the plugin contract every website scraper implements.

Adding a new website (FundsForNGOs, UNDP, UNICEF, World Bank, ...) requires ONLY:
    1. subclass BaseScraper, implement parse_listing() (+ optionally parse_detail(),
       next_page())
    2. decorate with @register
No existing code changes (Open/Closed Principle).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.schemas.opportunity import RawOpportunity
from app.services.amounts import extract_amount
from app.services.organization import extract_organization

log = logging.getLogger("scraper")
perf = logging.getLogger("performance")

ProgressCallback = Callable[[str, dict], Awaitable[None] | None]

# Deadline as stated on an opportunity's own page.
_DETAIL_DEADLINE = re.compile(
    r"(?:deadline|closing date|closes?(?:\s+on)?|apply by|applications? (?:close|due)|"
    r"submission deadline|due date|expires?(?:\s+on)?|last date)\s*[:\-–]?\s*"
    r"(\d{1,2}\s+\w{3,9}\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE)

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
    # Consecutive pages with nothing new before the manager abandons this source.
    #   None = use settings.stale_page_streak
    #   0    = never stop early — walk to the end of pagination (full archive)
    # Raise or zero it for large archives where already-seen pages sit in front
    # of pages that still hold listings the database has never seen.
    stale_page_streak_override: int | None = None
    # Visit a listing's own page to fill a missing amount/organisation. Costs one
    # request per gap, so it's opt-in per source and only enabled where the
    # detail page is plain HTML and actually carries the missing fields.
    enrich_details: bool = False
    # "The page I read contains opportunities and nothing else."
    #
    # Set True only for a dedicated call/tender/procurement board — UN Partner
    # Portal's /cfei/open, ADB's tender search, UNDP's procurement notices.
    # Those rows skip the vocabulary test in services/opportunity_gate.py,
    # because they are opportunities by construction and their titles often say
    # so nowhere: "Disability Inclusion Assessment" is a real UNPP call and
    # contains not one funding word.
    #
    # Leave False (the default) for anything scraped by harvesting links off a
    # general website, where a page mixes calls with news, programme pages and
    # navigation. Claiming curated for one of those disables the only check
    # that keeps those rows out.
    curated: bool = False

    # For sources whose results arrive by XHR *after* the page loads. networkidle
    # is not enough on its own: a page with analytics beacons and a search API
    # can report idle before the results come back, and the parser then sees a
    # navigation-only shell and concludes the listing is empty. ADB's tenders
    # page is exactly this — its HTML contains no tenders at all until
    # SearchStax responds. Set either of these from sources.json to wait for
    # proof that real content rendered.
    render_wait_selector: str = ""     # CSS selector that only exists with results
    render_wait_text: str = ""         # text that only appears with results

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
        """Enrich one item from its detail page.

        Default implementation is generic: fetch the listing's own URL and try
        to read a funding amount / organisation out of the page text. Subclasses
        override this when the site needs specific handling.
        """
        text = await self._detail_text(item.opportunity_url, client)
        if not text:
            return item
        # A deadline stated on the opportunity's own page is far more reliable
        # than one guessed from a listing row, and funder pages very often show
        # it only here. Without this, every rolling-looking listing is stored as
        # permanently open and never expires.
        if not item.deadline_raw:
            m = _DETAIL_DEADLINE.search(text)
            if m:
                item.deadline_raw = m.group(1)[:64]
                item.assume_active = False
        if not item.funding_amount:
            item.funding_amount = extract_amount(text)
        if not item.organization:
            item.organization = extract_organization(text, item.title)
        if not item.summary and len(text) > 120:
            item.summary = text[:1000]
        return item

    async def _detail_text(self, url: str, client: httpx.AsyncClient) -> str:
        """Readable text of a detail page, or '' if it can't be fetched."""
        if not url or not url.startswith("http"):
            return ""
        try:
            async with self._semaphore:
                elapsed = time.monotonic() - self._last_request
                if elapsed < settings.rate_limit_delay:
                    await asyncio.sleep(settings.rate_limit_delay - elapsed)
                self._last_request = time.monotonic()
                resp = await client.get(url)
                resp.raise_for_status()
        except (httpx.HTTPError, ValueError):
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:20_000]

    # ------------------------------------------------------- detail enrichment
    def _needs_detail(self, item: RawOpportunity) -> bool:
        """Only worth a request when something the dashboard shows is missing.

        A missing deadline counts: it decides whether the opportunity is shown as
        live or archived, so it matters more than the other two.
        """
        return not (item.funding_amount and item.organization and item.deadline_raw)

    async def _enrich_batch(
        self, items: list[RawOpportunity], client: httpx.AsyncClient, budget: list[int]
    ) -> None:
        """Fill gaps in-place by visiting detail pages, within a request budget.

        Deliberately targeted rather than exhaustive: a full crawl can be tens
        of thousands of listings, and fetching every one would take many hours
        and hammer the source site. Only rows actually missing a field are
        fetched, and `budget` caps how many per run.
        """
        if not self.enrich_details or budget[0] <= 0:
            return
        targets = [i for i in items if self._needs_detail(i)][: budget[0]]
        if not targets:
            return
        budget[0] -= len(targets)
        results = await asyncio.gather(
            *(self.parse_detail(i, client) for i in targets), return_exceptions=True
        )
        for original, result in zip(targets, results):
            if isinstance(result, Exception):
                log.debug("[%s] detail enrichment failed for %s: %s",
                          self.name, original.opportunity_url, result)

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
        # Fingerprint of the ITEMS on each page, not the URL. Several boards
        # answer an out-of-range page by re-serving the last (or first) one
        # rather than an empty result, so ?os=490 is a brand-new URL carrying
        # content already seen. Without this, disabling the "nothing new"
        # stop rule let World Bank walk to page 490 of a 32-page list,
        # re-fetching the same 34 rows every time.
        seen_content: set[str] = set()
        detail_budget = [settings.detail_fetch_limit]   # shared, decremented per page

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

                # End-of-list detection that survives "everything is already in
                # the database". A page whose items exactly repeat a page we
                # have already walked means the listing has run out — which is
                # a different thing from a page of rows we happen to have
                # stored already, and only the former should stop the crawl.
                content_sig = hashlib.sha256(
                    "|".join(sorted(
                        (i.opportunity_url or i.title or "") for i in items
                    )).encode("utf-8")
                ).hexdigest()
                if content_sig in seen_content:
                    log.info(
                        "[%s] page %s repeats listings already seen — end of the "
                        "list, stopping", self.name, page_number,
                    )
                    await progress("pages_end", {"source": self.name, "page": page_number})
                    return
                seen_content.add(content_sig)

                await self._enrich_batch(items, client, detail_budget)
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
        """Blocking Playwright fetch — runs in its own thread (see above).

        When this source has a saved login (see scrapers/site_auth.py) the
        browser is opened with that session, so gated listings are visible.
        Sources with no saved session are unaffected — open_context falls back
        to an ordinary anonymous browser, so an unconnected site still scrapes
        whatever it shows the public rather than failing.
        """
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        needs_login = self.name in site_auth.LOGIN_SITES

        with sync_playwright() as pw:
            # Both branches now hand back a CONTEXT, and teardown goes through
            # site_auth.close_owned, which closes the context AND the browser
            # behind it.
            #
            # The previous shape was:
            #
            #     context = site_auth.open_context(...)
            #     browser = context          # <- a BrowserContext named "browser"
            #     ...
            #     finally: browser.close()   # <- closes the context only
            #
            # open_context's storage-state and anonymous paths launch a Browser
            # and return one of its contexts without keeping a reference, so
            # that `close()` left the Chromium process running every time. The
            # name is what made it survive review: the line reads exactly like
            # correct cleanup. See site_auth.close_owned for the full account.
            context = None
            if needs_login:
                context = site_auth.open_context(pw, self.name, headless=True)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                # Was pw.chromium.launch() + browser.new_page(), which has the
                # same ownership split. Creating an explicit context keeps one
                # teardown path for both branches instead of two that drift.
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=settings.user_agent)
                page = context.new_page()
            try:
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass  # slow trackers shouldn't fail the page — take what rendered

                # Wait for evidence the results themselves rendered, not just
                # that the shell finished loading.
                if self.render_wait_selector:
                    try:
                        page.wait_for_selector(self.render_wait_selector, timeout=30_000)
                    except Exception:
                        log.warning("[%s] %r never appeared — the page may have "
                                    "rendered without results",
                                    self.name, self.render_wait_selector)
                if self.render_wait_text:
                    try:
                        page.wait_for_function(
                            "t => document.body && document.body.innerText.includes(t)",
                            arg=self.render_wait_text, timeout=30_000,
                        )
                    except Exception:
                        log.warning("[%s] text %r never appeared — the page may have "
                                    "rendered without results",
                                    self.name, self.render_wait_text)

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
                site_auth.close_owned(context)
