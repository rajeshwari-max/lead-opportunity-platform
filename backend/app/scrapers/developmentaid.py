"""DevelopmentAid scraper — one registered source covering Grants, Tenders,
and RFPs together (https://www.developmentaid.org/{grants,tenders}/search).

Angular SPA rendered via Playwright. Both sections share the same underlying
component (`<da-search-card entitytype="grant"|"tender">`), so a single
scrape walks both URLs in one browser session and tells them apart via each
card's own `entitytype` attribute — RFPs aren't a separate site section at
all; they're tender listings whose title says "RFP", so the keyword
classifier (not this scraper) sorts those into Category.RFP vs Category.TENDER.

Each result card publicly shows title, detail link, issuing organization,
locations, and the site's own "Status: Open" label — but the exact deadline
is membership-locked ("Unlock to view"). We therefore keep only cards the
site marks Open, stored with assume_active (no deadline; shown as Ongoing in
the dashboard, exact date on their site).

Pagination is a plain URL query parameter (`?pageNr=N`) — confirmed directly
from the site's own address bar while paging through results manually — so
each page is a normal navigation, not a JS button click. That's both more
reliable than hunting for a "Next" button in the rendered DOM and lets pages
be requested independently rather than one at a time through repeated clicks.
With ~2000+ pages available on a mature aggregator like this one, walking
every single page unconditionally would take a very long time for very
little benefit (the vast majority are old, closed listings) — so each
section stops early once a run of consecutive pages turns up zero "Open"
listings, rather than walking the full page count every time.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register
from app.services.amounts import clean_amount
from app.services.walk_budget import WalkBudget

log = logging.getLogger("scraper")

_VIEW_LINK = re.compile(r"/(?:grants|tenders)/view/\d+", re.IGNORECASE)
_DEADLINE_NEAR = re.compile(
    r"(deadline|closing)[^\d]{0,20}(\d{1,2}\s+\w{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE,
)
_MAX_PAGES = 30000       # ultimate safety cap only. The API reports ~118k grants and
                         # ~1.2M tenders, i.e. ~2,400 and ~24,700 pages of 50, so a
                         # 2,000-page cap silently truncated the archive. The real stop
                         # is "the API stopped returning anything new".
_STALE_OPEN_STREAK = 25  # consecutive pages with zero "Open" cards before stopping —
                         # the site sorts by Modified Date by default, so active
                         # listings cluster toward the front; deep pages are almost
                         # entirely old, closed history.
# Consecutive pages that contribute no listing we haven't already seen *in this
# run* before giving up on deeper pages. Backstop only — with the
# render-confirmation below, pages should genuinely advance.
_NO_NEW_URL_STREAK = 5

# How long to wait for the results grid to actually change after navigating to
# a new page number. Navigation returning HTTP 200 is NOT proof the Angular app
# re-rendered: a capture of one run held 8,400 cards but only 101 distinct
# listings — nearly every page had re-read the previous page's DOM. Pagination
# is only trusted once the rendered cards differ from the page before.
_RENDER_CHANGE_TIMEOUT_MS = 20_000
_RENDER_POLL_MS = 400

_CARD_URL = re.compile(r'href="(/(?:grants|tenders)/view/\d+)', re.IGNORECASE)

# Every section scraped in one run, tagged with a short slug (for log/debug
# file names) — add another (url, slug) pair here for any future section.
# An explicit sort is included so paging is deterministic: without one the
# backend is free to reorder between requests, which makes page N overlap
# page N-1 no matter how well the navigation works.
# The site's own filters do work we were doing badly afterwards:
#
#   languages=92   English only. This is the fix for the Arabic and Russian
#                  listings — filtered at source, they are never fetched,
#                  parsed, classified or stored, rather than being discarded
#                  at the end of the pipeline.
#   sectors=…      the four sectors the team actually bids in, so the crawl
#                  spends its per-search read budget on relevant calls instead
#                  of the whole 1.2M-row tender archive.
#
# Overridable via LOP_DEVAID_GRANTS_URL / LOP_DEVAID_TENDERS_URL for when the
# team's sector interests change, without editing code.
def _devaid_filters(sectors: str) -> str:
    """Query string for one DevelopmentAid section.

    Built from settings rather than hard-coded so the search can be retuned
    from .env without a deploy. `statuses=3` is what restricts the walk to
    currently-open calls — without it the archive of closed listings dominates
    the results and the search budget is spent on calls nobody can bid for.
    """
    parts = ["hiddenAdvancedFilters=0"]
    if sectors.strip():
        parts.append(f"sectors={sectors.strip()}")
    if settings.devaid_statuses.strip():
        parts.append(f"statuses={settings.devaid_statuses.strip()}")
    if settings.devaid_language.strip():
        parts.append(f"languages={settings.devaid_language.strip()}")
    return "&".join(parts)


_TENDER_FILTERS = _devaid_filters(settings.devaid_tender_sectors)
_GRANT_FILTERS = _devaid_filters(settings.devaid_grant_sectors)

_PLAIN_GRANTS = "https://www.developmentaid.org/grants/search"
_PLAIN_TENDERS = "https://www.developmentaid.org/tenders/search"


def _section_url(plain: str, filters: str, override: str) -> str:
    """The URL to walk for one section.

    Plain by default. Requesting the *filtered* search is what stopped this
    scraper working:

        30 Jul, plain URL      -> 27 MB page, 2,463 result cards
        after filters added    -> HTTP 403, title "Just a moment..."

    A long generated query string ("sectors=100,5,95,3,6,7,78,8,29,…") reads as
    machine traffic to a WAF. The same effect was demonstrated on ADB, where the
    bare path returns 200 and the parameterised one is refused outright. Since
    the pipeline already filters to English, currently-open, classified rows,
    narrowing in the request buys little and costs the whole source.

    Set LOP_DEVAID_FILTERED_SEARCH=true to try the filtered form again, or give
    an explicit URL via LOP_DEVAID_GRANTS_URL / LOP_DEVAID_TENDERS_URL.
    """
    if override:
        return override
    return f"{plain}?{filters}" if settings.devaid_filtered_search else plain


_SECTIONS: list[tuple[str, str]] = [
    # Both sections are walked in the same run — they are separate catalogues.
    (_section_url(_PLAIN_GRANTS, _GRANT_FILTERS, settings.devaid_grants_url), "grants"),
    (_section_url(_PLAIN_TENDERS, _TENDER_FILTERS, settings.devaid_tenders_url), "tenders"),
]

_SECTION_URLS: dict[str, str] = {slug: url for url, slug in _SECTIONS}

_ENTITYTYPE_CATEGORY = {
    "grant": Category.GRANT,
    "tender": Category.TENDER,
}


# The API's way of saying "no closing date". A row came back as
# `deadline: 9999-12-31`, which parses perfectly as a real date 7,973 years
# away — so it sails past every expiry check, sorts to the bottom of every
# "closing soon" view, and shows a user a deadline that does not exist.
#
# This is the three-state deadline model doing its job: the row is not
# dated, it is ROLLING, and the two have to be stored differently. Blanking
# the string lets assume_active pick it up as ongoing, which is what the
# source is actually saying.
_SENTINEL_DEADLINES = frozenset({
    "9999-12-31", "9999-12-31t00:00:00", "9999-12-31 00:00:00",
    "0001-01-01", "1970-01-01", "2099-12-31", "3000-01-01",
})


def _clean_sentinel_deadline(value: str) -> str:
    """'' for a placeholder date, the value unchanged otherwise."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    # Compare on the date part alone so 9999-12-31T00:00:00.000Z matches too.
    head = v.split("t")[0].split(" ")[0]
    if v in _SENTINEL_DEADLINES or head in _SENTINEL_DEADLINES:
        return ""
    if head.startswith(("9999-", "0001-", "3000-")):
        return ""
    return value


def _page_url(base_url: str, page_nr: int) -> str:
    """https://.../grants/search?pageNr=N — preserves any other query params."""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs["pageNr"] = [str(page_nr)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))



def _membership_state(page) -> tuple[str, str]:
    """('in' | 'out' | 'unknown', evidence). Delegates to devaid_auth.

    This used to be a second, separate implementation, and having two of them
    is exactly how `devaid_session.py status` came to print "signed in: True"
    in the same minute that `push` refused with "the saved session is no longer
    signed in" — same machine, same profile, two detectors, two answers.

    One detector now, in devaid_auth, so the scraper and the session tooling
    can never disagree about whether a run is signed in.
    """
    from app.scrapers.devaid_auth import membership_state

    return membership_state(page)


class _ApiResp:
    """Just enough of Playwright's APIResponse for the call sites in this file.

    Exists so the switch from `page.request.post` to an in-page fetch is a
    one-line change at each call site instead of a rewrite of five of them.
    """

    __slots__ = ("ok", "status", "_body", "via")

    def __init__(self, ok: bool, status: int, body, via: str = "page fetch"):
        self.ok = bool(ok)
        self.status = int(status or 0)
        self._body = body
        self.via = via

    def json(self):
        return self._body


# Headers a browser sets for itself. Assigning them from fetch() is a no-op by
# spec, so passing them through would be noise that reads like configuration.
_FORBIDDEN_FETCH_HEADERS = frozenset({
    "host", "connection", "content-length", "cookie", "cookie2", "origin",
    "referer", "user-agent", "accept-encoding", "accept-charset", "date",
    "dnt", "expect", "keep-alive", "te", "trailer", "transfer-encoding",
    "upgrade", "via",
})


@register
class DevelopmentAidScraper(BaseScraper):
    # Set once per run by the session check in _walk_sections. Read by the
    # pagination-restriction message, which means something completely
    # different depending on whether we were a member at the time.
    _session_state = "unknown"
    # True once any JSON API call has returned a usable body this run. A run
    # where this stays False has measured nothing about what the account may
    # read, so no message is allowed to describe its limits as a plan limit.
    _api_reached = False

    name = "developmentaid"
    display_name = "DevelopmentAid"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    website = "https://www.developmentaid.org"
    start_url = _SECTIONS[0][0]   # BaseScraper needs one; the real walk covers all sections
    requires_js = True   # Angular SPA — always rendered via Playwright

    # 0 = never stop early. A re-run re-reads the same early pages (the site
    # sorts by Modified Date), so any "N pages with nothing new -> stop" rule
    # ends the crawl in the already-seen prefix and never reaches the listings
    # further in: a run with a streak of 25 stopped at page 55 of 811, saving
    # 27 rows out of 2,188 seen. This is the archive source — it walks every
    # page and lets duplicate detection do the filtering.
    stale_page_streak_override = 0

    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    async def crawl(self, stop_event, pause_event, progress):
        """Stream one page at a time instead of returning every card at once.

        The previous design walked all pages inside a single fetch and returned
        one enormous HTML blob (a real run collected 40,550 cards ≈ 124 MB of
        markup, which BeautifulSoup expanded into a ~1.5 GB parse tree). It
        died there, so a 40-minute scrape saved *nothing* — everything was
        held until the very end.

        Now the browser walk runs in a worker thread and hands each page's
        cards straight to the consumer through a bounded queue. Memory stays
        flat regardless of page count, every page is parsed and committed as it
        arrives, and a failure at page 700 keeps the 699 pages already saved.
        The bounded queue also provides natural backpressure, which is what
        makes pause work: the consumer stops draining and the walker blocks.
        """
        import queue as _queue
        import threading

        q: _queue.Queue = _queue.Queue(maxsize=8)
        stop_flag = threading.Event()

        def _producer() -> None:
            try:
                self._walk_sections(q, stop_flag)
            except Exception as exc:                      # noqa: BLE001
                q.put(("error", exc))
            finally:
                q.put(("done", None))

        worker = threading.Thread(target=_producer, name="devaid-walk", daemon=True)
        worker.start()

        page_number = 0
        try:
            while True:
                if stop_event.is_set():
                    log.info("[%s] stop requested — ending crawl", self.name)
                    return
                await pause_event.wait()   # cleared == paused

                try:
                    kind, payload = await asyncio.to_thread(q.get, True, 1.0)
                except _queue.Empty:
                    continue               # nothing ready yet — re-check stop/pause
                if kind == "done":
                    break
                if kind == "error":
                    # Re-raise rather than break. Swallowing it reported
                    # "completed — 0 found" for a run that never reached the site
                    # at all, which reads identically to "the site had nothing
                    # new" in the dashboard and in the digest. The manager
                    # catches this and marks the source failed instead.
                    log.error("[%s] browser walk failed: %s: %s",
                              self.name, type(payload).__name__, payload)
                    raise payload

                slug, page_nr, body = payload
                page_number += 1
                await progress("page_start", {"source": self.name, "page": page_number,
                                              "url": _page_url(_SECTION_URLS[slug], page_nr)})
                if kind == "items":
                    # Came straight from the JSON API — already structured, so
                    # there's no HTML to parse.
                    items = body
                else:
                    # One page's worth of markup (~50 cards, a few hundred KB) —
                    # small enough to parse and discard immediately.
                    html = "<html><body>" + "".join(body) + "</body></html>"
                    try:
                        items = self.parse_listing(html, _page_url(_SECTION_URLS[slug], page_nr))
                    except Exception:
                        log.exception("[%s] parse error on %s page %s — skipping",
                                      self.name, slug, page_nr)
                        items = []
                yield items
                await progress("page_done", {"source": self.name, "page": page_number,
                                             "found": len(items)})
            await progress("pages_end", {"source": self.name, "page": page_number})
        finally:
            # Always release the browser thread, including on stop/exception.
            stop_flag.set()

    def _walk_sections(self, out_queue, stop_flag) -> None:
        """Browser-side walk. Pushes ('page', (slug, page_nr, cards)) per page."""
        from playwright.sync_api import sync_playwright

        from app.scrapers.devaid_auth import open_persistent

        with sync_playwright() as pw:
            # persistent profile = the session the user saved via "Connect account"
            #
            # headless is configurable because it is itself a bot signal: real
            # Chrome in headless mode puts "HeadlessChrome" in its own user
            # agent, and the UA is deliberately not overridden here (a UA that
            # disagrees with the browser is a worse signal than an honest one).
            # Set LOP_DEVAID_HEADLESS=false on a desktop to run a visible
            # window, which is an ordinary browser doing ordinary browsing.
            headless = settings.devaid_headless
            log.info("[developmentaid] opening browser (headless=%s)", headless)
            browser = open_persistent(pw, headless=headless)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                session_checked = False
                # Log the app's own JSON search endpoint once per run — the
                # route to replacing UI-driven paging with direct API paging.
                api_logged: set[str] = set()

                # Filter options the app loads (sectors, locations, donors…).
                # These become the slice keys: each is a *different* search with
                # its own reachable pages, which is the only way past the ~100
                # record depth limit on a single unfiltered query.
                taxonomies: dict[str, list[dict]] = {}

                def _on_response(resp):
                    key = resp.url.split("?")[0]
                    if key not in api_logged:
                        api_logged.add(key)
                        self._log_search_api(resp)
                    try:
                        if "/api/" not in resp.url or "/search" in resp.url:
                            return
                        # Only read bodies that could plausibly BE a filter option
                        # list. resp.json() blocks the driver thread, and doing it
                        # for every JSON response on this app's bootstrap added
                        # tens of seconds to hydration — long enough for the
                        # card-render wait below to time out and the section to be
                        # abandoned as "no cards".
                        if not any(n in resp.url.lower() for n in (
                            "dictionary", "sector", "location", "countr", "donor",
                            "status", "language", "purpose", "applicanttype", "type",
                        )):
                            return
                        if "json" not in (resp.headers or {}).get("content-type", "").lower():
                            return
                        body = resp.json()
                        rows = body if isinstance(body, list) else (
                            body.get("items") if isinstance(body, dict) else None)
                        if not isinstance(rows, list) or len(rows) < 3:
                            return
                        if all(isinstance(r, dict) and "id" in r for r in rows[:5]):
                            taxonomies[key] = rows
                    except Exception:
                        pass

                page.on("response", _on_response)

                # Capture the exact request body the app posts to its search
                # endpoint. That payload (with its page/offset field) is what a
                # future version needs to page the API directly instead of
                # driving the UI — far faster and immune to DOM timing.
                captured: dict[str, dict] = {}   # slug -> {url, headers, body}

                def _on_request(req):
                    try:
                        if "/api/frontend/" not in req.url or "/search" not in req.url:
                            return
                        body = req.post_data
                        if not body:
                            return
                        kind = "grants" if "/grant/" in req.url else (
                            "tenders" if "/tender/" in req.url else "")
                        if kind and kind not in captured:
                            captured[kind] = {"url": req.url, "body": body,
                                              "headers": dict(req.headers)}
                            log.info("[developmentaid] captured %s search request -> %s",
                                     kind, body[:300])
                    except Exception:
                        pass

                page.on("request", _on_request)

                wanted = {s.strip().lower() for s in settings.devaid_sections.split(",")
                          if s.strip()} or {"grants", "tenders"}
                sections = [(u, s) for u, s in _SECTIONS if s in wanted]
                if len(sections) < len(_SECTIONS):
                    skipped = [s for _, s in _SECTIONS if s not in wanted]
                    # Loud, because this is silent data loss by configuration:
                    # the run reports success while an entire catalogue is never
                    # visited. It read as "the scraper is broken" for weeks.
                    log.warning(
                        "[developmentaid] ONLY walking %s — %s will NOT be scraped. "
                        "This comes from LOP_DEVAID_SECTIONS in backend/.env; set it "
                        "to 'grants,tenders' (or remove the line) to cover both.",
                        ", ".join(s for _, s in sections), ", ".join(skipped),
                    )

                # Sections that never rendered a card because the site served a
                # bot challenge instead. Counted so the run can end in a real
                # failure rather than a cheerful "0 found" — see the raise after
                # the loop.
                blocked: list[str] = []

                for section_url, slug in sections:
                    if stop_flag.is_set():
                        return
                    # A transient failure here (DNS blip, timeout, etc.) must not
                    # take down the whole run — without this guard, one bad
                    # request on the *second* section discarded every card
                    # already collected from the first (seen in production:
                    # 575 pages / 28,750 grants cards lost to a single
                    # ERR_NAME_NOT_RESOLVED loading the tenders section).
                    try:
                        # Not settings.request_timeout (30s): this site answers
                        # with a Cloudflare interstitial that takes several
                        # seconds to evaluate and then navigates on by itself.
                        # Thirty seconds timed out *during* that wait, so the
                        # run failed at the one moment it might have succeeded.
                        # Waiting longer is not working around the check — it
                        # either clears on its own or it does not.
                        resp = page.goto(section_url, timeout=90_000,
                                         wait_until="domcontentloaded")
                        if resp is not None and resp.status in (403, 429, 503):
                            # A fixed 12s sleep here was both too short and the
                            # wrong shape: Cloudflare's non-interactive check
                            # navigates the page itself when it passes, so what
                            # matters is whether the title stops being a
                            # challenge, not how long we waited.
                            log.info("[developmentaid] %s: HTTP %s on arrival — "
                                     "waiting for the interstitial to clear",
                                     slug, resp.status)
                            if self._wait_out_challenge(page, slug):
                                log.info("[developmentaid] %s: challenge cleared "
                                         "on its own", slug)
                    except Exception as exc:
                        log.warning(
                            "[developmentaid] %s: failed to load section start page "
                            "(%s: %s) — skipping this section, keeping results "
                            "already collected from other section(s)",
                            slug, type(exc).__name__, exc or "(no message)",
                        )
                        continue
                    log.info("[developmentaid] %s page 1 HTTP status: %s",
                             slug, resp.status if resp else "no response (cached/redirect)")

                    # Dismiss the cookie-consent banner (harmless if absent).
                    try:
                        page.get_by_role("button", name="I Accept").click(timeout=5_000)
                    except Exception:
                        pass

                    def _cards_rendered(timeout_ms: int) -> bool:
                        try:
                            page.wait_for_selector("da-search-card", timeout=timeout_ms)
                            return True
                        except Exception:
                            return False

                    # First page's Angular bootstrap has been taking 20-30s+
                    # lately — give it generous room to hydrate.
                    rendered = _cards_rendered(45_000)
                    if not rendered:
                        # One reload before giving up. Evidence for this: a
                        # failing capture of the GRANTS section was 11 KB with
                        # the correct page title and zero cards — the shell
                        # arrived and the Angular app never bootstrapped. That
                        # is a transient front-end failure, not a block, and a
                        # second attempt usually renders. (The tenders capture
                        # from the same run was a Cloudflare interstitial
                        # instead, which a reload will not fix — hence the
                        # separate challenge check above.)
                        try:
                            _size = len(page.content() or "")
                        except Exception:
                            _size = 0
                        log.info("[developmentaid] %s: no cards after first load "
                                 "(%s bytes) — reloading once", slug, _size)
                        try:
                            page.reload(timeout=90_000, wait_until="domcontentloaded")
                        except Exception:
                            pass
                        rendered = _cards_rendered(60_000)

                    if not rendered:
                        _title = ""
                        try:
                            _title = (page.title() or "").strip()
                        except Exception:
                            pass
                        if self._is_challenge_title(_title):
                            blocked.append(slug)
                            log.error(
                                "[developmentaid] %s: BLOCKED BY CLOUDFLARE (page title %r). "
                                "This is a bot check, not a login problem — a session "
                                "will not get past it. Reconnect via 'Connect account' "
                                "so the run uses real Chrome, and if it persists the site "
                                "is refusing automated access and needs an API/data "
                                "agreement with DevelopmentAid.",
                                slug, _title,
                            )
                        log.warning(
                            "[developmentaid] %s: no cards on page 1 — landed on "
                            "url=%s title=%r (if this is a login/captcha page, "
                            "reconnect the account via the dashboard's Connect "
                            "account button)",
                            slug, page.url, page.title(),
                        )
                        try:  # save what the browser actually sees for debugging
                            from pathlib import Path
                            dbg = Path(__file__).resolve().parents[2] / "logs"
                            page.screenshot(path=str(dbg / f"devaid_{slug}_debug.png"), full_page=False)
                            (dbg / f"devaid_{slug}_debug.html").write_text(page.content(), encoding="utf-8")
                            log.warning("[developmentaid] saved logs/devaid_%s_debug.png and .html", slug)
                        except Exception:
                            log.exception("[developmentaid] %s: debug capture failed", slug)
                        continue   # try the other section anyway

                    # The saved "Connect account" session can silently expire (cookie
                    # lifetime, manual logout elsewhere, etc), in which case the site
                    # falls back to an anonymous guest view. Checked once per run
                    # (login state doesn't change between sections/pages in the same
                    # browser session) — decided together with how far pagination
                    # actually gets below, since a lone "Sign in" DOM sighting isn't
                    # reliable on its own (the header can briefly show it as a
                    # placeholder even while genuinely logged in).
                    # A member sees pagination controls; a logged-out guest gets a
                    # single preview page with none. That's a far more reliable
                    # signal than pagination depth, because when signed out this
                    # site answers ?pageNr=N by re-serving page 1 — which is what
                    # made earlier runs look like they were walking 474 pages
                    # while only ever seeing the first 50 listings.
                    checked_this_section = False
                    signed_out_detected = False
                    if not session_checked:
                        session_checked = True
                        checked_this_section = True
                        # Positive member signals decide this, not the mere
                        # presence of a "Sign in" link — the site keeps one in a
                        # collapsed menu even for signed-in members, and trusting
                        # it made this wrongly declare a live Premium session
                        # expired and delete its marker.
                        state, evidence = _membership_state(page)
                        self._session_state = state
                        signed_out_detected = (state == "out")
                        log.info(
                            "[developmentaid] %s: session check -> %s (%s)",
                            slug,
                            {"in": "SIGNED IN", "out": "SIGNED OUT",
                             "unknown": "UNCLEAR"}[state],
                            evidence,
                        )
                        if state == "unknown":
                            # Deliberately loud. Every later verdict in this run
                            # — including "the plan restricts pagination" —
                            # depends on knowing whether we are a member, and
                            # this is the one case where we do not.
                            log.warning(
                                "[developmentaid] %s: could not tell whether this "
                                "run is signed in, so treat any pagination limit "
                                "below as unattributed — it may be the plan, or "
                                "it may be that the session is gone.", slug,
                            )
                        if signed_out_detected:
                            # Say it up front rather than after the walk: as a
                            # guest there is nothing beyond page 1 to walk.
                            log.error(
                                "[developmentaid] NOT LOGGED IN — the saved session has "
                                "expired, so only the public preview page (~50 listings "
                                "per section) is reachable and pagination is unavailable. "
                                "Click 'Connect account' on the dashboard and sign in "
                                "again, then re-run."
                            )
                            # Clear the stale "connected" marker so the dashboard
                            # stops claiming the account is linked and shows the
                            # Connect button again. Without this the UI hides the
                            # only control that can fix the problem.
                            try:
                                from app.scrapers.devaid_auth import _CONNECTED_MARKER
                                _CONNECTED_MARKER.unlink(missing_ok=True)
                                log.info("[developmentaid] cleared the stale 'connected' "
                                         "marker — the dashboard will offer Connect again")
                            except Exception:
                                log.debug("[developmentaid] could not clear connected marker")

                    # Preferred path: replay the app's own search request with an
                    # incremented page number. Falls through to the UI walk below
                    # if the request wasn't captured or the API doesn't cooperate.
                    if slug in captured and self._walk_via_api(
                        page, captured[slug], slug, out_queue, stop_flag,
                        taxonomies=taxonomies,
                    ):
                        continue

                    # Bigger pages before walking: fewer clicks, same data.
                    self._set_per_page(page, slug)

                    cards_sent = 0
                    stale_open_streak = 0
                    pages_reached = 0
                    hit_safety_cap = True   # flips False on any organic stop below
                    seen_urls: set[str] = set()   # distinct listings seen this section
                    no_new_streak = 0
                    prev_signature: frozenset[str] = frozenset()

                    for page_nr in range(1, _MAX_PAGES + 1):
                        if stop_flag.is_set():
                            log.info("[developmentaid] %s: stop requested at page %s",
                                     slug, page_nr)
                            hit_safety_cap = False
                            break
                        if page_nr > 1:
                            # Navigating to ?pageNr=N does NOT drive this app:
                            # the URL updates, the grid doesn't (proved by a run
                            # where 474 "pages" held ~100 distinct listings).
                            # Pagination has to be driven the way the site does
                            # it — through its own control, in the same session.
                            if not self._go_to_page(page, page_nr, slug):
                                log.info(
                                    "[developmentaid] %s: no control to reach page %s "
                                    "— end of listings", slug, page_nr,
                                )
                                hit_safety_cap = False
                                break
                            try:
                                page.wait_for_selector("da-search-card", timeout=15_000)
                            except Exception:
                                log.info(
                                    "[developmentaid] %s: page %s rendered no cards — "
                                    "end of listings", slug, page_nr,
                                )
                                hit_safety_cap = False
                                break

                        # wait_for_selector is satisfied by the *previous* page's
                        # cards still in the DOM, so poll until the rendered set
                        # actually differs before reading it.
                        cards, changed = self._read_when_changed(page, prev_signature)
                        if not cards:
                            log.info("[developmentaid] %s: page %s returned no cards "
                                      "— stopping", slug, page_nr)
                            hit_safety_cap = False
                            break
                        if page_nr > 1 and not changed:
                            log.warning(
                                "[developmentaid] %s: page %s still shows page %s's "
                                "listings after %.0fs — the site did not advance. "
                                "Stopping this section rather than re-reading the "
                                "same results (this is what previously produced "
                                "thousands of duplicate 'found' rows).",
                                slug, page_nr, page_nr - 1,
                                _RENDER_CHANGE_TIMEOUT_MS / 1000,
                            )
                            hit_safety_cap = False
                            break
                        prev_signature = self._signature(cards)

                        # How much of this page is genuinely new to the run?
                        page_urls = {m for c in cards for m in _CARD_URL.findall(c)}
                        fresh = page_urls - seen_urls
                        seen_urls |= page_urls

                        # Hand this page straight over to be parsed and saved.
                        # Blocks once the queue is full, which both caps memory
                        # and lets a paused consumer throttle the walk.
                        out_queue.put(("page", (slug, page_nr, cards)))
                        cards_sent += len(cards)
                        pages_reached = page_nr

                        no_new_streak = no_new_streak + 1 if not fresh else 0
                        if no_new_streak >= _NO_NEW_URL_STREAK:
                            log.info(
                                "[developmentaid] %s: %s consecutive pages served "
                                "nothing new — end of the site's real result set at "
                                "page %s (%s distinct listings). Deeper pages just "
                                "re-serve these, so stopping here.",
                                slug, no_new_streak, page_nr, len(seen_urls),
                            )
                            hit_safety_cap = False
                            break

                        # This walk can legitimately take many minutes (hundreds
                        # of pages) with nothing else logged in between, which
                        # looks identical to a hang from the dashboard. A
                        # periodic heartbeat makes it visible that it's still
                        # working and roughly how far along it is.
                        if page_nr % 25 == 0:
                            log.info(
                                "[developmentaid] %s: still walking — page %s, "
                                "%s cards handed off, %s distinct listings so far",
                                slug, page_nr, cards_sent, len(seen_urls),
                            )

                        open_here = sum(1 for c in cards if self._card_is_open(c))
                        stale_open_streak = stale_open_streak + 1 if open_here == 0 else 0
                        if stale_open_streak >= _STALE_OPEN_STREAK:
                            log.info(
                                "[developmentaid] %s: %s consecutive pages with no "
                                "Open listings — stopping at page %s (assuming "
                                "everything deeper is older/closed history)",
                                slug, stale_open_streak, page_nr,
                            )
                            hit_safety_cap = False
                            break

                    log.info(
                        "[developmentaid] %s: walked %s pages, %s cards handed off, "
                        "%s distinct listings (%.0f%% of what was served was a repeat)",
                        slug, pages_reached, cards_sent, len(seen_urls),
                        (1 - len(seen_urls) / cards_sent) * 100 if cards_sent else 0,
                    )
                    if hit_safety_cap and pages_reached >= _MAX_PAGES:
                        log.warning(
                            "[developmentaid] %s: hit the %s-page safety cap — "
                            "results may be TRUNCATED. Raise _MAX_PAGES in "
                            "developmentaid.py if this keeps happening.",
                            slug, _MAX_PAGES,
                        )

                    # A real guest account can't get more than a page or two deep
                    # (confirmed separately) — so a "Sign in" sighting alongside a
                    # short walk is a real session-expired signal, not noise.
                    if checked_this_section and signed_out_detected and pages_reached <= 2:
                        log.error(
                            "[developmentaid] SESSION EXPIRED — scraping as a "
                            "logged-out guest (page shows a 'Sign in' link and "
                            "pagination stopped after %s page(s)). Results will be "
                            "limited to the public preview page; reconnect the "
                            "account via the dashboard's Connect account button.",
                            pages_reached,
                        )
                        try:
                            from app.services import email_service
                            email_service.send_alert(
                                subject="DevelopmentAid session expired — reconnect needed",
                                body=(
                                    "The DevelopmentAid scraper detected it is running "
                                    "as a logged-out guest instead of your connected "
                                    "account.\n\n"
                                    "Effect: results are limited to the public preview "
                                    "page (no pagination past page 1, deadlines stay "
                                    "locked).\n\n"
                                    "Fix: open the dashboard and click 'Connect account' "
                                    "under DevelopmentAid, then log in again in the "
                                    "browser window that opens."
                                ),
                            )
                        except Exception:
                            log.exception("[developmentaid] failed to send session-expired alert email")

                # Every section the site refused = a run that never saw a single
                # listing. Raising turns it into a FAILED source in the dashboard
                # instead of "completed — 0 found", which is what let this sit
                # broken across several runs while looking like a quiet week.
                if blocked and len(blocked) == len(sections):
                    self._alert_blocked(blocked)
                    raise RuntimeError(
                        "DevelopmentAid served a Cloudflare bot challenge for every "
                        f"section ({', '.join(blocked)}) — nothing was scraped. See "
                        "logs/devaid_<section>_debug.html for the page the browser got."
                    )
            finally:
                browser.close()

    # ---------------------------------------------------------- bot challenge
    @staticmethod
    def _is_challenge_title(title: str) -> bool:
        """True for Cloudflare's interstitial titles."""
        t = (title or "").strip().lower()
        return any(m in t for m in (
            "just a moment", "attention required", "access denied",
            "checking your browser", "verifying you are human",
        ))

    def _wait_out_challenge(self, page, slug: str, timeout_ms: int = 45_000) -> bool:
        """Wait for Cloudflare's non-interactive check to pass itself.

        It clears by navigating the page, so the signal is the title changing —
        polling for that is both faster when it passes and honest when it never
        does. Returns True if the page is no longer a challenge.
        """
        waited = 0
        while waited < timeout_ms:
            try:
                if not self._is_challenge_title(page.title()):
                    return True
            except Exception:
                pass                       # mid-navigation — try again shortly
            page.wait_for_timeout(1_500)
            waited += 1_500
        log.info("[developmentaid] %s: the interstitial did not clear in %ss",
                 slug, timeout_ms // 1000)
        return False

    @staticmethod
    def _alert_blocked(blocked: list[str]) -> None:
        try:
            from app.services import email_service
            email_service.send_alert(
                subject="DevelopmentAid blocked the scraper — no listings collected",
                body=(
                    "DevelopmentAid answered with a Cloudflare bot challenge "
                    "(\"Just a moment...\") instead of the search results for: "
                    f"{', '.join(blocked)}.\n\n"
                    "Effect: this run collected nothing from DevelopmentAid. Other "
                    "sources are unaffected.\n\n"
                    "What to try, in order:\n"
                    "  1. Open the dashboard and click 'Connect account' under "
                    "DevelopmentAid, and sign in. That runs a visible Chrome window, "
                    "which is what earns the cf_clearance cookie the headless runs "
                    "reuse.\n"
                    "  2. If it still fails, set LOP_DEVAID_HEADLESS=false on a "
                    "machine with a screen and run once to refresh the clearance.\n"
                    "  3. If it persists from the server's IP, the address itself is "
                    "likely rate-limited or blocked, and the durable fix is a data "
                    "agreement with DevelopmentAid rather than a scraper change.\n"
                ),
            )
        except Exception:
            log.exception("[developmentaid] failed to send the blocked-by-Cloudflare alert")

    # ------------------------------------------------------------- JSON API
    # The Angular app fetches results from POST /api/frontend/{grant,tender}/search.
    # Paging that endpoint is strictly better than driving the UI: no DOM timing,
    # no pagination controls to find, and no way for a stale render to be mistaken
    # for a new page. The request the app makes is captured live rather than
    # hard-coded, so filters/sort stay exactly as the site set them.
    _PAGE_KEYS = ("pageNr", "pageNumber", "page", "pageIndex", "offset", "from", "start", "skip")

    @staticmethod
    def _find_page_key(payload: dict) -> tuple[str, bool] | None:
        """Locate a top-level pagination field. Returns (key, is_offset_based)."""
        for k in DevelopmentAidScraper._PAGE_KEYS:
            for actual in payload:
                if actual.lower() == k.lower() and isinstance(payload[actual], int):
                    return actual, actual.lower() in ("offset", "from", "start", "skip")
        return None

    @staticmethod
    def _page_paths(payload) -> list[tuple[tuple, int]]:
        """Every integer field anywhere in the payload that looks like paging.

        Searches nested objects too: this API's request wraps most state in a
        "filter" object, and a top-level `pageNr` alone did not drive it past
        page 2, so the control may sit deeper.
        """
        out: list[tuple[tuple, int]] = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, int) and not isinstance(v, bool) and any(
                        p.lower() in k.lower() for p in
                        ("page", "offset", "from", "start", "skip", "index")
                    ):
                        out.append((path + (k,), v))
                    walk(v, path + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path + (i,))

        walk(payload, ())
        return out

    @staticmethod
    def _set_path(payload, path: tuple, value: int) -> None:
        node = payload
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value

    @staticmethod
    def _items_from_json(data):
        """The listing array in an unknown response shape: the longest list of
        dicts that look like records (have an id and some text field)."""
        best: list = []

        def walk(node):
            nonlocal best
            if isinstance(node, list):
                if node and all(isinstance(x, dict) for x in node):
                    keys = set().union(*(set(x) for x in node[:5]))
                    lowered = {k.lower() for k in keys}
                    if ("id" in lowered or any("id" == k.lower() for k in keys)) and len(node) > len(best):
                        best = node
                for x in node:
                    walk(x)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)

        walk(data)
        return best

    # Keys that hold a database identifier rather than something displayable.
    # `donorIds` matching the "donor" needle put raw numbers like "118391" into
    # the Organization column for every tender.
    _ID_KEY = re.compile(r"(^|[^a-z])ids?$", re.IGNORECASE)

    @staticmethod
    def _pick(item: dict, *needles: str, allow_numeric: bool = False) -> str:
        """First value whose key contains one of `needles`.

        Identifier fields are skipped, and by default a purely numeric value is
        rejected — an ID is never the answer to "what is this organisation
        called?", but it is a perfectly good answer for a budget.
        """
        for k, v in item.items():
            kl = k.lower()
            if not any(n in kl for n in needles):
                continue
            if DevelopmentAidScraper._ID_KEY.search(k) and not allow_numeric:
                continue
            if isinstance(v, str) and v.strip():
                if not allow_numeric and v.strip().isdigit():
                    continue
                return v.strip()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if not allow_numeric:
                    continue
                return str(v)
            if isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, str) and vv.strip():
                        return vv.strip()
            if isinstance(v, list) and v:
                parts = [x if isinstance(x, str) else
                         (x.get("name") or x.get("title") or "") if isinstance(x, dict) else ""
                         for x in v]
                joined = ", ".join(p for p in parts if p and not str(p).isdigit())
                if joined:
                    return joined
        return ""

    def _raw_from_item(self, item: dict, slug: str) -> RawOpportunity | None:
        title = self._pick(item, "title", "name", "subject")
        if not title or len(title) < 8:
            return None
        # The record's OWN id — an exact key match. Using a "contains id" match
        # picked up donorIds and produced links like /tenders/view/118345,118364,
        # which 111 rows shared and none of which open the opportunity.
        # Case-insensitive, because _items_from_json accepts a record set on a
        # case-insensitive "id" match. A payload keyed "Id" therefore passed that
        # check and then failed here, leaving every row with an empty URL — which
        # collapses the whole run to one entry in the dedupe set and makes every
        # row unsaveable. The two lookups have to agree.
        raw_id = next((v for k, v in item.items() if k.lower() == "id"), None)
        ident = str(raw_id) if isinstance(raw_id, (int, str)) and str(raw_id).isdigit() else ""
        url = self._pick(item, "url", "link", "slug")
        if url.startswith("/"):
            url = self.website + url
        elif not url.startswith("http") and ident:
            url = f"{self.website}/{slug}/view/{ident}"
        elif not url.startswith("http"):
            url = ""      # no usable id — better empty than a link that misleads
        status = self._pick(item, "status").lower()
        deadline = _clean_sentinel_deadline(
            self._pick(item, "deadline", "closing", "expir"))
        where = self._pick(item, "location", "countr", "region")
        return RawOpportunity(
            title=title[:500],
            # Name-bearing keys first (abbreviatedDonorNames, donors) so a bare
            # id list can never win the race.
            organization=(self._pick(item, "donorname", "fundingagency", "agencyname")
                          or self._pick(item, "funding", "donor", "agency", "authority",
                                        "client", "organization", "organisation"))[:512],
            funding_amount=clean_amount(
                self._pick(item, "budget", "amount", "value", allow_numeric=True)),
            location=where[:512],
            # Feed the same text to `country` so normalize_geo can resolve a
            # country and derive a region. Without this the Country filter and the
            # By Region chart were empty for every one of these rows.
            country=where[:128],
            vertical=self._pick(item, "sector", "vertical")[:256],
            deadline_raw=deadline[:64],
            opportunity_url=url,
            website=self.website,
            source_website=self.display_name,
            category_hint=_ENTITYTYPE_CATEGORY.get("grant" if slug == "grants" else "tender"),
            assume_active=bool(status in ("", "open") and not deadline),
        )

    def _api_json(self, page, url, slug, payload=None, headers=None, timeout_ms=None):
        """Call the site's API from INSIDE the page, the way the site itself does.

        Why this is not `page.request.post`
        -----------------------------------
        The 2026-08-26 run recorded both halves of the contradiction, four lines
        apart, in one log:

            search API seen: POST .../api/frontend/tender/search (200)
            tenders: API page 1 -> HTTP 403, stopping

        Same endpoint, same session, same browser, seconds apart. 200 when the
        site's own JavaScript called it; 403 when we replayed it.

        `page.request` shares the browser context's COOKIES but not its network
        stack. It is Playwright's own HTTP client, with its own TLS handshake,
        header order, HTTP/2 settings frame and client-hint set. Cloudflare
        fingerprints precisely those. So the replay looked like a different
        client presenting a valid session — which is the shape of a stolen
        cookie, and it is answered with 403. Nothing was wrong with the session,
        the account or the credentials.

        `fetch()` evaluated in the page has no such gap: Chromium's own network
        stack, same origin, same cookie jar, same client hints. The request is
        indistinguishable from the SPA's because it *is* the SPA's.

        This one fault produced five separate misleading findings downstream,
        each of which read as a real measurement:

            page size 300 not honoured (returned 0)     <- 403
            pageSize (1-based) failed after 0 probe pages   <- 403
            could not fetch any filter option lists        <- 403
            no status taxonomy available                   <- 403
            this account reads one page per search         <- inferred from the above

        None of that was measured. It was one 403, reported five ways, and it is
        what led to the conclusion that the subscription tier was the ceiling.
        """
        import json as _json

        safe = {k: v for k, v in (headers or {}).items()
                if k.lower() not in _FORBIDDEN_FETCH_HEADERS}
        try:
            res = page.evaluate(
                """async ({url, payload, headers, hasBody}) => {
                    const h = Object.assign({}, headers || {});
                    const opts = {
                        method: hasBody ? 'POST' : 'GET',
                        credentials: 'include',
                        // The SPA is same-origin, so this is the default the
                        // browser would use anyway; stated so a redirect to a
                        // login page surfaces as a status rather than silently
                        // returning that page's HTML as if it were data.
                        redirect: 'follow',
                    };
                    if (hasBody) {
                        opts.body = JSON.stringify(payload);
                        if (!h['content-type']) h['content-type'] = 'application/json';
                    }
                    if (!h['accept']) h['accept'] = 'application/json, text/plain, */*';
                    opts.headers = h;
                    let r;
                    try { r = await fetch(url, opts); }
                    catch (e) {
                        return {ok: false, status: 0, text: '', err: String(e)};
                    }
                    let t = '';
                    try { t = await r.text(); } catch (e) { t = ''; }
                    return {ok: r.ok, status: r.status, text: t, err: ''};
                }""",
                {"url": url, "payload": payload, "headers": safe,
                 "hasBody": payload is not None},
            ) or {}
        except Exception as exc:                                # noqa: BLE001
            # The page can be closed, navigating, or blocked by a CSP that
            # forbids evaluate. Falling back keeps the run alive; the log says
            # which path answered so a 403 can be attributed correctly.
            log.debug("[developmentaid] %s: in-page fetch unavailable (%s) — "
                      "falling back to the out-of-page client",
                      slug, type(exc).__name__)
            return self._api_json_via_request(page, url, slug, payload, headers,
                                              timeout_ms)

        if res.get("err"):
            log.debug("[developmentaid] %s: in-page fetch error: %s",
                      slug, str(res["err"])[:200])
            return _ApiResp(False, 0, None)

        text = res.get("text") or ""
        body = None
        if text:
            try:
                body = _json.loads(text)
            except ValueError:
                # A 200 that is not JSON is the site answering with a page —
                # a challenge or a login wall — not with data. Say so, because
                # "ok but unparseable" and "refused" need different fixes.
                log.debug("[developmentaid] %s: %s returned HTTP %s with "
                          "non-JSON body (%s bytes, starts %r)",
                          slug, url.rsplit("/", 1)[-1], res.get("status"),
                          len(text), text[:80])
                return _ApiResp(False, res.get("status") or 0, None)
        if res.get("ok"):
            # Latched for the whole run. Read by the pagination-restriction
            # message, which must not blame the account's plan on a run where
            # the API never answered at all.
            self._api_reached = True
        return _ApiResp(res.get("ok"), res.get("status") or 0, body)

    def _api_json_via_request(self, page, url, slug, payload=None, headers=None,
                              timeout_ms=None):
        """The old out-of-page path. Kept only as a fallback — see _api_json."""
        import json as _json

        timeout = timeout_ms or int(settings.request_timeout * 1000)
        try:
            if payload is None:
                resp = page.request.get(url, timeout=timeout)
            else:
                resp = page.request.post(
                    url, data=_json.dumps(payload),
                    headers=headers or {}, timeout=timeout,
                )
            if not resp.ok:
                return _ApiResp(False, resp.status, None, via="out-of-page client")
            self._api_reached = True
            return _ApiResp(True, resp.status, resp.json(), via="out-of-page client")
        except Exception:                                       # noqa: BLE001
            return _ApiResp(False, 0, None, via="out-of-page client")

    def _count_items(self, page, captured, payload, slug) -> int:
        """How many listings one request returns — used to test a bigger page size."""
        import json as _json
        import time as _time

        _time.sleep(max(settings.rate_limit_delay * 0.5, 0.3))
        try:
            resp = self._api_json(page, captured["url"], slug, payload=payload,
                                  headers=self._api_headers(captured, slug))
            if not resp.ok:
                # Distinguish "the server refused" from "the server honestly
                # returned nothing", because the caller logs this as
                # "page size N not honoured" — which is a lie if it was a 403.
                log.info("[developmentaid] %s: page-size probe got HTTP %s "
                         "(not a size limit — the request was refused)",
                         slug, resp.status)
                return 0
            return len(self._items_from_json(resp.json()))
        except Exception:      # noqa: BLE001
            return 0

    @staticmethod
    def _api_headers(captured: dict, slug: str) -> dict:
        """Replay the app's headers, plus the Origin/Referer a browser would send.

        Some endpoints quietly fall back to the first page when a request looks
        like it didn't come from the site itself.
        """
        headers = {k: v for k, v in captured.get("headers", {}).items()
                   if k.lower() not in ("content-length", "host")}
        headers.setdefault("content-type", "application/json")
        headers.setdefault("accept", "application/json, text/plain, */*")
        headers["origin"] = "https://www.developmentaid.org"
        headers["referer"] = _SECTION_URLS.get(slug, "https://www.developmentaid.org")
        return headers

    def _probe_pagination(self, page, captured, payload, page_size, slug):
        """Find a payload field that genuinely advances this API.

        Sending the app's own `pageNr` returned page 2 correctly and then reset
        to page 1 from page 3 onward, so the field can't be assumed. Each
        candidate (every paging-ish integer in the payload, tried 1-based,
        0-based and as an offset) is verified by fetching three pages and
        requiring all three to be different. Costs ~9 requests, once per run.
        """
        import json as _json

        import time as _time

        # If every probe came back refused rather than empty, the conclusion
        # "this API cannot be paged" is unsupported — we never reached the API.
        # Recorded here so the summary below can say which of the two happened.
        refused: dict[str, int] = {}

        def fetch(candidate_payload):
            """Full id set for a probe page — a set, not the first few ids, so
            windows that overlap by all-but-one are recognised as overlapping."""
            # Space the probes out. Firing ~9 identical POSTs in four seconds is
            # exactly the pattern an API throttles by serving a cached first
            # page, which would look like "pagination doesn't work".
            _time.sleep(max(settings.rate_limit_delay, 0.8))
            try:
                resp = self._api_json(page, captured["url"], slug,
                                      payload=candidate_payload,
                                      headers=self._api_headers(captured, slug))
                if not resp.ok:
                    refused["status"] = resp.status
                    log.debug("[developmentaid] %s: probe HTTP %s", slug, resp.status)
                    return None
                body = resp.json()
                items = self._items_from_json(body)
                if items and isinstance(body, dict) and body.get("meta") is not None:
                    log.debug("[developmentaid] %s: probe meta=%s",
                              slug, str(body.get("meta"))[:160])
                return frozenset(str(i.get("id", "")) for i in items) if items else None
            except Exception:      # noqa: BLE001
                return None

        candidates = []
        for path, base in self._page_paths(payload):
            label = ".".join(str(p) for p in path)
            name = str(path[-1]).lower()
            offset_like = any(t in name for t in ("offset", "from", "start", "skip"))
            variants = [
                (f"{label} (offset x{page_size})",
                 lambda p, n, _p=path: self._set_path(p, _p, (n - 1) * page_size)),
                (f"{label} (1-based)", lambda p, n, _p=path: self._set_path(p, _p, n)),
                (f"{label} (0-based)", lambda p, n, _p=path: self._set_path(p, _p, n - 1)),
            ]
            # An "offset"-named field almost certainly counts records, not pages;
            # trying page numbers on it first yields windows that overlap by 49
            # of 50 rows, which looks "different" but is nearly all duplicates.
            candidates.extend(variants if offset_like else variants[1:] + variants[:1])

        if not candidates:
            log.info("[developmentaid] %s: payload has no paging-like field", slug)
            return None

        for describe, apply_page in candidates:
            probe = _json.loads(_json.dumps(payload))   # fresh copy per attempt
            seen = []
            for n in (1, 2, 3):
                apply_page(probe, n)
                sig = fetch(probe)
                if sig is None:
                    break
                seen.append(sig)
            # Require three genuinely disjoint pages. Merely "not identical"
            # isn't enough: paging an offset field by 1 shifts the window a
            # single row, which is 98% duplicates dressed up as progress.
            # Two disjoint pages is enough to adopt a field. Requiring three
            # rejected a field that demonstrably advanced once, and the
            # fallback (driving the UI) reached only a single page — so a
            # partially working API beat the alternative outright. The walk's
            # own "nothing new" detector handles it if it stalls later.
            if len(seen) >= 2:
                union = set().union(*seen)
                total = sum(len(s) for s in seen)
                if len(union) == total:
                    log.info("[developmentaid] %s: pagination confirmed via %s "
                             "(%s probe pages, %s listings, no overlap)",
                             slug, describe, len(seen), total)
                    return describe, apply_page
                if len(seen[0] | seen[1]) == len(seen[0]) + len(seen[1]):
                    log.info("[developmentaid] %s: pagination via %s advances at least "
                             "one page (probe 3 repeated) — using it and stopping when "
                             "it stalls", slug, describe)
                    return describe, apply_page
                log.info("[developmentaid] %s: %s overlaps (%s unique of %s)",
                         slug, describe, len(union), total)
            else:
                log.info("[developmentaid] %s: %s failed after %s probe pages",
                         slug, describe, len(seen))
        # Only meaningful when the API answered NOTHING all run. The first
        # version of this fired on any refusal at all, and on 2026-08-26 it
        # announced "the API was never reached" in a run where the taxonomies
        # had just been fetched and a 100-item page had been accepted — the
        # refusals were HTTP 400s from probe variants the API rejects by
        # design (pageNr=0, an offset where a page number belongs). Warning
        # about a normal negative result is how a diagnostic loses its
        # credibility, so it now has to clear both bars.
        last = refused.get("status") or 0
        if last in (401, 403) and not getattr(self, "_api_reached", False):
            log.error(
                "[developmentaid] %s: EVERY pagination probe was REFUSED (last "
                "HTTP %s) and no API call succeeded this run, so nothing above "
                "is a measurement of what this account is allowed to page. Do "
                "not read it as a plan limit. The site's own JavaScript calls "
                "this same endpoint successfully, so the refusal is about how "
                "the request was made, not who made it.",
                slug, last,
            )
        elif last:
            log.info(
                "[developmentaid] %s: no paging field found; last probe rejection "
                "was HTTP %s. The API did answer this run, so this is a real "
                "result — %s.", slug, last,
                "400 means the probe payloads themselves were invalid, which is "
                "expected for the variants this tries" if last == 400
                else "the endpoint declined those payloads",
            )
        return None

    @staticmethod
    def _find_list_path(payload, *names) -> tuple | None:
        """Path to a list-valued field with one of these names (e.g. 'sectors')."""
        hit: list[tuple] = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, list) and k.lower() in names:
                        hit.append(path + (k,))
                    walk(v, path + (k,))

        walk(payload, ())
        return hit[0] if hit else None

    @staticmethod
    def _pick_taxonomy(taxonomies: dict, *needles) -> list[dict]:
        """The captured option list whose endpoint mentions one of `needles`."""
        for url, rows in taxonomies.items():
            if any(n in url.lower() for n in needles):
                return rows
        return []

    def _fetch_taxonomies(self, page, slug: str) -> dict[str, list[dict]]:
        """Ask the API for its filter option lists directly.

        Passively listening for them didn't work — the app only requests these
        when a filter panel is opened, so a headless run never sees them and the
        crawl ends up with no axis to split on.
        """
        base = "https://www.developmentaid.org/api/frontend"
        kind = "grant" if slug == "grants" else "tender"
        candidates = [
            f"{base}/dictionary/sectors", f"{base}/sectors",
            f"{base}/{kind}/sectors", f"{base}/dictionary/statuses",
            f"{base}/{kind}/statuses", f"{base}/dictionary/locations",
            f"{base}/locations", f"{base}/dictionary/donors",
            f"{base}/dictionary/grant-purposes", f"{base}/dictionary/applicant-types",
        ]
        found: dict[str, list[dict]] = {}
        for url in candidates:
            try:
                resp = self._api_json(page, url, slug, timeout_ms=15_000)
                if not resp.ok:
                    continue
                body = resp.json()
                rows = body if isinstance(body, list) else (
                    body.get("items") if isinstance(body, dict) else None)
                if isinstance(rows, list) and len(rows) >= 2 and \
                        all(isinstance(r, dict) and "id" in r for r in rows[:5]):
                    found[url] = rows
                    log.info("[developmentaid] %s: fetched %s options from %s",
                             slug, len(rows), url.rsplit("/", 1)[-1])
            except Exception:
                continue
        if not found:
            log.warning("[developmentaid] %s: could not fetch any filter option lists "
                        "— slicing is unavailable this run", slug)
        return found

    def _probe_total(self, page, captured, payload, slug) -> int:
        """The server's own result count for a filter set — the signal that tells
        us whether a search is truncated and must be split further."""
        import json as _json
        import time as _time

        _time.sleep(max(settings.rate_limit_delay * 0.3, 0.2))
        try:
            resp = self._api_json(page, captured["url"], slug, payload=payload,
                                  headers=self._api_headers(captured, slug))
            if not resp.ok:
                return -1
            body = resp.json()
            if isinstance(body, dict):
                for k in ("total", "totalCount", "totalItems", "count"):
                    if isinstance(body.get(k), int):
                        return body[k]
            return len(self._items_from_json(body))
        except Exception:      # noqa: BLE001
            return -1

    def _dimensions(self, payload, taxonomies, slug) -> list[tuple[str, tuple, list]]:
        """Every filter axis we can split on, widest first.

        Returns (label, path_in_payload, list_of_values). Splitting is the only
        route to full coverage: a single search exposes ~2 pages regardless of
        paging, so the archive has to be partitioned into searches that each fit
        inside that depth.
        """
        dims: list[tuple[str, tuple, list]] = []
        for names, needles, label in (
            (("sectors",), ("sector",), "sector"),
            (("locations",), ("location", "countr", "geograph"), "location"),
            (("donors",), ("donor",), "donor"),
            (("applicanttypes",), ("applicanttype",), "applicantType"),
            (("grantpurposes",), ("purpose",), "purpose"),
            (("languages",), ("language",), "language"),
            (("tendertypes",), ("tendertype", "type"), "tenderType"),
        ):
            path = self._find_list_path(payload, *names)
            rows = self._pick_taxonomy(taxonomies, *needles)
            if not path or not rows:
                continue
            values = [r["id"] for r in rows if r.get("id") is not None]
            labels = {r["id"]: (r.get("name") or r.get("title") or str(r["id"]))[:40]
                      for r in rows if r.get("id") is not None}
            if values:
                dims.append((label, path, [(v, labels[v]) for v in values]))
        if dims:
            log.info("[developmentaid] %s: candidate split axes -> %s", slug,
                     ", ".join(f"{d[0]}({len(d[2])})" for d in dims))
        return dims

    def _validate_axes(self, page, captured, payload, dims, baseline, slug):
        """Drop axes the server ignores.

        A filter that isn't honoured returns the full result set, so splitting on
        it produces identical "slices" forever: one run made 291 searches that all
        came back with the same 113 records. An axis is only kept if applying one
        of its values actually reduces the reported total.
        """
        import json as _json

        keep = []
        for label, path, values in dims:
            probe = _json.loads(_json.dumps(payload))
            try:
                self._set_path(probe, path, [values[0][0]])
            except Exception:
                continue
            got = self._probe_total(page, captured, probe, slug)
            if got < 0:
                continue
            if baseline > 0 and got >= baseline:
                log.warning("[developmentaid] %s: ignoring axis %r — filtering by "
                            "%s still returns %s of %s, so the server isn't applying "
                            "it", slug, label, values[0][1], f"{got:,}", f"{baseline:,}")
                continue
            log.info("[developmentaid] %s: axis %r works (%s -> %s listings)",
                     slug, label, f"{baseline:,}", f"{got:,}")
            keep.append((label, path, values))
        return keep

    def _budget_splits(self, payload) -> list[tuple[int, int]] | None:
        """Budget sub-ranges, as a last resort axis when the taxonomies run out.

        Numeric and always divisible, so it keeps refining after the categorical
        axes are exhausted. Not exhaustive on its own (a listing with no budget
        may be excluded by any explicit range), which is why it comes last.
        """
        path = None
        for key in ("budgetineurorange", "budgetrange", "budget"):
            p = self._find_dict_path(payload, key)
            if p:
                path = p
                break
        if not path:
            return None
        return path


    @staticmethod
    def _find_dict_path(payload, name) -> tuple | None:
        hit: list[tuple] = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, dict) and k.lower() == name and \
                            {"min", "max"} <= {kk.lower() for kk in v}:
                        hit.append(path + (k,))
                    walk(v, path + (k,))

        walk(payload, ())
        return hit[0] if hit else None

    def _slice_plan(self, payload, taxonomies, slug) -> list[tuple[str, tuple, list]]:
        """Build the list of searches to run: (label, path_to_filter, value).

        Slicing is what gets past the depth limit. One unfiltered search only
        exposes its first ~100 records however it's paged, but each filtered
        search is a distinct query with its own reachable pages, so the union of
        many narrow searches covers far more of the archive than one broad one.
        """
        plan: list[tuple[str, tuple, list]] = []
        for names, needles, label in (
            (("sectors",), ("sector",), "sector"),
            (("locations",), ("location", "countr"), "location"),
            (("donors",), ("donor",), "donor"),
        ):
            path = self._find_list_path(payload, *names)
            rows = self._pick_taxonomy(taxonomies, *needles)
            if not path or not rows:
                continue
            for row in rows:
                ident = row.get("id")
                name = (row.get("name") or row.get("title") or str(ident))[:60]
                if ident is None:
                    continue
                plan.append((f"{label}={name}", path, [ident]))
            if plan:
                log.info("[developmentaid] %s: slicing by %s — %s values available",
                         slug, label, len(plan))
                break        # one dimension is enough; more would multiply runtime
        if not plan:
            log.info("[developmentaid] %s: no filter taxonomy captured, so the run is "
                     "limited to the depth of a single search", slug)
        return plan[:settings.devaid_max_slices]

    def _walk_via_api(self, page, captured: dict, slug: str, out_queue, stop_flag,
                      taxonomies: dict | None = None) -> bool:
        """Page the site's JSON search endpoint. True if it carried the section.

        Emits ("items", (slug, page_nr, [RawOpportunity])) so the consumer skips
        HTML parsing entirely. Every page is verified to contain listings we
        haven't already seen — the same guard as the UI walk, because an API can
        echo a page just as easily as a rendered grid.
        """
        import json as _json

        try:
            payload = _json.loads(captured["body"])
        except (ValueError, TypeError):
            log.info("[developmentaid] %s: search body isn't JSON — using the UI walk", slug)
            return False
        if not isinstance(payload, dict):
            return False
        log.info("[developmentaid] %s: full search payload -> %s",
                 slug, _json.dumps(payload)[:2000])

        size_key = next((k for k in payload
                         if k.lower() in ("size", "limit", "pagesize", "perpage")
                         and isinstance(payload[k], int) and payload[k] > 0), None)
        page_size = payload[size_key] if size_key else 50

        # The site's own UI offers 50/100/300 results per page. Paging depth is
        # limited here — page 3 onward always came back as page 1, i.e. only
        # ~100 records are reachable per search — so a larger page size is the
        # difference between 100 and 600 records from the same two pages.
        if size_key:
            for candidate in (300, 200, 100):
                if candidate <= page_size:
                    break
                probe = _json.loads(_json.dumps(payload))
                probe[size_key] = candidate
                got = self._count_items(page, captured, probe, slug)
                if got and got > page_size:
                    log.info("[developmentaid] %s: page size %s accepted (%s items in "
                             "one request, was %s)", slug, candidate, got, page_size)
                    payload[size_key] = candidate
                    page_size = candidate
                    break
                log.info("[developmentaid] %s: page size %s not honoured (returned %s)",
                         slug, candidate, got)

        setter = self._probe_pagination(page, captured, payload, page_size, slug)
        single_page_only = setter is None
        if single_page_only:
            # No field pages this API — on a plan that only exposes one page per
            # search, that's expected rather than a failure. Coverage then comes
            # entirely from running many narrower searches, so keep going with a
            # no-op pager instead of abandoning the API path (which previously
            # dropped straight to a UI walk that reads a single page and stops).
            log.info(
                "[developmentaid] %s: this account reads one page per search, so "
                "coverage will come from narrowing the search rather than paging",
                slug,
            )
            describe, apply_page = "single page per search", (lambda _p, _n: None)
        else:
            describe, apply_page = setter

        seen: set[str] = set()
        stats = {"sent": 0, "searches": 0, "first_search": True}

        def run_search(search_payload: dict, label: str) -> int:
            """Walk one search as deep as it allows. Returns new listings found."""
            before = len(seen)
            no_new = 0
            for page_nr in range(1, _MAX_PAGES + 1):
                if stop_flag.is_set():
                    break
                if not self._api_page(page, captured, search_payload, slug, apply_page,
                                      page_nr, describe, page_size, seen, out_queue,
                                      stats, label):
                    break
                if single_page_only:
                    break          # one page is all this search will ever yield
                if stats.pop("_no_fresh", False):
                    no_new += 1
                    if no_new >= _NO_NEW_URL_STREAK:
                        break
                else:
                    no_new = 0
            gained = len(seen) - before
            stats["searches"] += 1
            return gained

        # Restrict to live opportunities before anything else. The app sends
        # statuses:[] — every status ever — so its reported total (118k grants,
        # 1.2M tenders) is mostly calls that closed years ago. Filtering to open
        # ones shrinks the target to what can actually be bid on, and that set is
        # small enough to cover properly within the plan's read limits.
        # Build an "open listings only" variant of the search. It is covered
        # FIRST, before the historical archive: the archive is ~6x larger, so a
        # single pass over everything spends its whole search budget on calls
        # that closed years ago and never finishes covering the live ones — which
        # are the only listings anyone can actually bid on.
        open_payload = None
        if settings.devaid_open_first:
            st_path = self._find_list_path(payload, "statuses")
            st_rows = self._pick_taxonomy(
                {**(taxonomies or {}), **self._fetch_taxonomies(page, slug)}, "status")
            open_ids = [r["id"] for r in st_rows
                        if str(r.get("name", "")).strip().lower() in
                        ("open", "active", "forecast", "published")
                        and r.get("id") is not None]
            if st_path and open_ids:
                before = self._probe_total(page, captured, payload, slug)
                candidate = _json.loads(_json.dumps(payload))
                self._set_path(candidate, st_path, open_ids)
                after = self._probe_total(page, captured, candidate, slug)
                if 0 < after < before:
                    log.info("[developmentaid] %s: %s of %s listings are open — those "
                             "are covered first", slug, f"{after:,}", f"{before:,}")
                    open_payload = candidate
                else:
                    log.info("[developmentaid] %s: status filter didn't narrow the "
                             "search (%s -> %s), covering everything in one pass",
                             slug, f"{before:,}", f"{after:,}")
            elif st_path:
                log.info("[developmentaid] %s: no status taxonomy available — cannot "
                         "prioritise open listings this run", slug)

        # ---- adaptive partitioning for full coverage -------------------------
        # A search only ever exposes `reachable` records, so any search whose
        # server-reported total exceeds that is truncated and gets split along
        # the next filter axis. Recursing until every leaf fits is what turns
        # "the first 600" into the whole archive.
        # Measured, not assumed: the probe showed page 2 returning the same records
        # as page 1, so exactly one page is readable per search on this plan.
        reachable = page_size
        grand_total = self._probe_total(page, captured, payload, slug)
        tax = dict(taxonomies or {})
        tax.update(self._fetch_taxonomies(page, slug))
        dims = self._validate_axes(
            page, captured, payload,
            self._dimensions(payload, tax, slug), grand_total, slug,
        )
        budget_path = self._budget_splits(payload)
        # Same rule for the budget fallback: prove it narrows before recursing on
        # it, or the run burns its whole search budget on identical results.
        if budget_path and grand_total > 0:
            probe = _json.loads(_json.dumps(payload))
            try:
                node = probe
                for step in budget_path[:-1]:
                    node = node[step]
                rng = dict(node[budget_path[-1]])
                rng["min"], rng["max"] = 0, 1000
                node[budget_path[-1]] = rng
                if self._probe_total(page, captured, probe, slug) >= grand_total:
                    log.warning("[developmentaid] %s: ignoring budget axis — narrowing "
                                "the range doesn't change the result count", slug)
                    budget_path = None
            except Exception:
                budget_path = None
        if not dims and not budget_path:
            log.warning(
                "[developmentaid] %s: no working filter axis, so only the first %s "
                "listings of this search are reachable. Coverage cannot be improved "
                "without filters the server honours.", slug, reachable,
            )
        log.info("[developmentaid] %s: %s listings to cover; each search exposes "
                 "%s, so it will be partitioned until every part fits",
                 slug, f"{grand_total:,}" if grand_total >= 0 else "?", reachable)

        # Whichever of the three binds first ends the walk cleanly, naming
        # itself so the run's coverage number can be read as a bound rather
        # than as a failure. See services/walk_budget.py.
        budget = WalkBudget(
            searches_cap=settings.devaid_max_slices,
            duration_s=settings.devaid_max_duration_s,
            record_cap=settings.devaid_max_records,
        )

        def over_budget() -> str:
            return budget.exceeded(len(seen))

        # How deep budget bisection may go. 0-20,000,000 halved to width 1 is
        # 25 levels, and the 2026-08-26 run went all the way down — producing
        # labels like "budget 0-1", "budget 1-2", each re-reading the SAME 102
        # rows. A cap is a backstop; the inert-axis test below is the real fix.
        MAX_BUDGET_DEPTH = 8
        # None = untested, True = it finds new rows, False = proven useless.
        sort_reversal = {"works": None}

        def cover(base: dict, dim_idx: int, label: str, lo: int = 0, hi: int = 0,
                  parent_total: int | None = None, budget_depth: int = 0) -> None:
            if stop_flag.is_set():
                return
            if over_budget():
                return
            total = self._probe_total(page, captured, base, slug)
            budget.spend()
            if total == 0:
                return
            # Did narrowing the budget range actually narrow anything?
            #
            # This is the guard whose absence turned a 390-row leak into an
            # 839-row loss. Carrying lo/hi into the leaves made budget
            # bisection reachable — correct — but nothing checked that the
            # server HONOURS the filter down there. It does not: every level
            # came back holding 102, so the recursion split 0-20,000,000 down
            # to 0-1 and read the same 102 rows at every one of 800 nodes.
            # Coverage fell from 1,214 distinct to 375 because the search
            # budget was spent on an exponential tree of identical queries.
            #
            # A split that returns its parent's count is not a split. One
            # probe is enough to know, and the answer costs nothing extra —
            # this run had to make the probe anyway.
            budget_inert = (
                budget_depth > 0
                and parent_total is not None
                and total >= parent_total
            )
            if budget_inert and budget_depth == 1:
                log.info("[developmentaid] %s: budget filter has no effect inside "
                         "%s (still %s after halving the range) — not bisecting it "
                         "here", slug, label, f"{total:,}")
            if total < 0:
                run_search(base, label)
                return
            if total <= reachable:
                gained = run_search(base, label)
                if gained:
                    log.info("[developmentaid] %s: %s -> %s listings, +%s new "
                             "(%s distinct so far)", slug, label, total, gained, len(seen))
                return
            # Too big to page through — split it.
            if dim_idx < len(dims):
                dname, dpath, dvalues = dims[dim_idx]
                log.info("[developmentaid] %s: %s holds %s (> %s) — splitting by %s "
                         "into %s parts", slug, label, f"{total:,}", reachable,
                         dname, len(dvalues))
                for value, vname in dvalues:
                    if stop_flag.is_set() or over_budget():
                        break
                    child = _json.loads(_json.dumps(base))
                    try:
                        self._set_path(child, dpath, [value])
                    except Exception:
                        continue
                    # lo/hi MUST be carried down. They were not, and the
                    # defaults are 0/0, so every leaf reached through a
                    # categorical split arrived with hi=0 — and the budget
                    # bisection below is guarded by `hi > lo + 1`, which 0 > 1
                    # never satisfies. The budget axis therefore existed, was
                    # validated, and could never fire anywhere except the root.
                    # That is the whole explanation for the four
                    # "no split axis remains" leaks in the 2026-08-26 run.
                    cover(child, dim_idx + 1,
                          f"{label}+{dname}={vname}" if label != "all" else f"{dname}={vname}",
                          lo, hi)
                return
            if budget_inert or budget_depth >= MAX_BUDGET_DEPTH:
                # Fall through to the leaf handling below rather than
                # bisecting a filter this subtree has just proved it ignores.
                pass
            elif budget_path and hi > lo + 1:
                mid = (lo + hi) // 2
                for a, b in ((lo, mid), (mid, hi)):
                    if stop_flag.is_set() or over_budget():
                        break
                    child = _json.loads(_json.dumps(base))
                    try:
                        node = child
                        for step in budget_path[:-1]:
                            node = node[step]
                        rng = dict(node[budget_path[-1]])
                        rng["min"], rng["max"] = a, b
                        node[budget_path[-1]] = rng
                    except Exception:
                        continue
                    # parent_total is what makes the inert-axis test at the top
                    # possible: the child compares its own count against the
                    # count this node just measured, and stops the moment
                    # narrowing stops narrowing.
                    cover(child, dim_idx, f"{label}+budget {a}-{b}", a, b,
                          parent_total=total, budget_depth=budget_depth + 1)
                return
            # No axis left. Before giving up, try reading from the other end.
            #
            # The theory: a truncated search returns the FIRST `reachable` rows
            # of some ordering, so reversing the ordering returns the LAST ones.
            # On this API the theory is wrong — the 2026-08-26 run reversed 800
            # leaves and got `0 new` from every single one, because the endpoint
            # ignores `sort` the same way it ignores the budget range.
            #
            # So it is now measured, once, and switched off when it fails. An
            # idea that costs one request per leaf has to prove itself on the
            # first leaf or stop costing anything. `None` = untested,
            # False = tried and yielded nothing.
            gained = run_search(base, label)
            flipped = 0
            sort_val = base.get("sort")
            if (sort_reversal["works"] is not False
                    and isinstance(sort_val, str) and "." in sort_val
                    and total > reachable):
                field, _, direction = sort_val.rpartition(".")
                other = "asc" if direction.lower() == "desc" else "desc"
                mirror = _json.loads(_json.dumps(base))
                mirror["sort"] = f"{field}.{other}"
                flipped = run_search(mirror, f"{label} [reversed]")
                budget.spend()
                if sort_reversal["works"] is None:
                    sort_reversal["works"] = flipped > 0
                    if not sort_reversal["works"]:
                        log.info("[developmentaid] %s: reversing the sort order "
                                 "returns the same rows — this endpoint ignores "
                                 "`sort`, so it will not be tried again this run",
                                 slug)
            if total > reachable * 2:
                log.warning(
                    "[developmentaid] %s: %s holds %s but no split axis remains — "
                    "captured %s reading forward and %s reading backward, so about "
                    "%s are unreachable without another filter axis.",
                    slug, label, f"{total:,}", gained, flipped,
                    f"{max(total - reachable * 2, 0):,}",
                )
            else:
                log.info("[developmentaid] %s: %s holds %s, no split axis left — "
                         "read both ends for %s + %s new",
                         slug, label, f"{total:,}", gained, flipped)

        hi_budget = 0
        if budget_path:
            try:
                node = payload
                for step in budget_path[:-1]:
                    node = node[step]
                hi_budget = int(node[budget_path[-1]].get("max") or 0)
            except Exception:
                hi_budget = 0
        if open_payload is not None:
            log.info("[developmentaid] %s: PASS 1 — covering open listings", slug)
            cover(_json.loads(_json.dumps(open_payload)), 0, "open", 0, hi_budget)
            log.info("[developmentaid] %s: PASS 1 done — %s open listings captured "
                     "using %s searches", slug, f"{len(seen):,}", budget.searches)

            # PASS 2 — the historical archive. OFF unless explicitly asked for.
            #
            # This is what a scheduled production run was doing every time, and
            # the 2026-08-29 baseline measured the cost: DevelopmentAid found
            # 779,856 records to save 55,013 — a 93% discard rate — and 85% of
            # the whole database (90,551 of 106,854 rows) is expired as a
            # result. The platform's job is opportunities someone can still
            # respond to; the archive is, by definition, the opposite.
            #
            # It remains available as an explicitly invoked maintenance
            # operation via LOP_DEVAID_INCLUDE_ARCHIVE=true, which is what the
            # brief asks for: separate, and disabled by default.
            if not settings.devaid_include_archive:
                log.info(
                    "[developmentaid] %s: skipping the historical archive — "
                    "production runs collect open/current listings only. "
                    "%s open listing(s) captured in %s searches. Set "
                    "LOP_DEVAID_INCLUDE_ARCHIVE=true for a one-off backfill.",
                    slug, f"{len(seen):,}", budget.searches,
                )
                return True

            log.warning(
                "[developmentaid] %s: PASS 2 — walking the HISTORICAL ARCHIVE "
                "because LOP_DEVAID_INCLUDE_ARCHIVE is enabled. This is a "
                "maintenance operation: it is long, and most of what it finds "
                "has already closed.", slug,
            )
        elif not settings.devaid_include_archive:
            # No open-only filter was available (the status taxonomy could not
            # be fetched), so the single pass below is unavoidably over
            # everything. Say so rather than letting an archive walk happen
            # under the name of a normal run.
            log.warning(
                "[developmentaid] %s: no status filter is available this run, so "
                "the only possible pass covers closed listings too. Bounded by "
                "the caps below; coverage of OPEN listings will be partial.", slug,
            )
        cover(_json.loads(_json.dumps(payload)), 0, "all", 0, hi_budget)

        if grand_total > 0:
            pct = len(seen) / grand_total * 100
            log.info("[developmentaid] %s: COVERAGE %s of %s listings (%.1f%%) from %s "
                     "searches", slug, f"{len(seen):,}", f"{grand_total:,}", pct,
                     stats["searches"])
            if budget.reason:
                # Name the cap AND the gap. "15.5% covered" is a fact someone
                # can act on; "completed" for the same run is not.
                missing = max(grand_total - len(seen), 0)
                log.warning(
                    "[developmentaid] %s: PARTIAL COVERAGE — stopped at %s. "
                    "%s of %s listings collected (%.1f%%); roughly %s were not "
                    "reached this run. This is a bound, not a failure: the run "
                    "did what it was allowed to do. Raise LOP_DEVAID_MAX_SLICES "
                    "/ _MAX_DURATION_S / _MAX_RECORDS to go further, or accept "
                    "partial coverage of a source whose plan limits reads.",
                    slug, budget.reason, f"{len(seen):,}",
                    f"{grand_total:,}", pct, f"{missing:,}",
                )
            elif pct < 95:
                log.warning(
                    "[developmentaid] %s: %.1f%% coverage without hitting any cap — "
                    "the remaining listings are unreachable with the filters this "
                    "account can apply, not merely unvisited.", slug, pct,
                )

        total_sent = stats["sent"]
        if total_sent == 0:
            return False
        log.info("[developmentaid] %s: API walk done — %s listings handed off across %s "
                 "searches, %s distinct", slug, total_sent, stats["searches"], len(seen))
        return True

    def _api_page(self, page, captured, payload, slug, apply_page, page_nr, describe,
                  page_size, seen, out_queue, stats, label) -> bool:
        """Fetch and emit one API page. False when the walk should stop."""
        import json as _json

        if True:
            apply_page(payload, page_nr)
            try:
                if page_nr > 1:
                    import time as _t
                    _t.sleep(max(settings.rate_limit_delay * 0.5, 0.3))
                resp = self._api_json(page, captured["url"], slug, payload=payload,
                                      headers=self._api_headers(captured, slug))
                if not resp.ok:
                    if resp.status in (401, 403):
                        # Named separately from a 5xx or a timeout: a refusal on
                        # an endpoint the page itself calls successfully is a
                        # statement about the REQUEST, not about the account.
                        log.error(
                            "[developmentaid] %s: API page %s REFUSED with HTTP %s "
                            "even though this ran inside the page. The session or "
                            "the challenge cookie has expired mid-run — reload the "
                            "listing page and retry, or push a fresh session. This "
                            "is not evidence of a subscription limit.",
                            slug, page_nr, resp.status,
                        )
                    else:
                        log.info("[developmentaid] %s: API page %s -> HTTP %s, stopping",
                                 slug, page_nr, resp.status)
                    return False
                data = resp.json()
            except Exception as exc:            # noqa: BLE001
                log.info("[developmentaid] %s: API page %s failed (%s) — stopping",
                         slug, page_nr, exc)
                return False

            raw_items = self._items_from_json(data)
            if not raw_items:
                return False
            if page_nr == 1 and stats.get("first_search"):
                stats["first_search"] = False
                log.info("[developmentaid] %s: paging the JSON API via %s, %s per page. "
                         "Sample keys: %s", slug, describe, page_size,
                         sorted(raw_items[0])[:14])
                if isinstance(data, dict):
                    total = data.get("total")
                    log.info("[developmentaid] %s: envelope=%s meta=%s total=%s", slug,
                             sorted(data)[:20], str(data.get("meta"))[:300], total)
                    if isinstance(total, int) and total > 0:
                        est_pages = -(-total // max(page_size, 1))
                        log.info(
                            "[developmentaid] %s: server reports %s listings ≈ %s pages. "
                            "At ~0.7s/page that is roughly %.0f minutes for this section "
                            "— most will be long-expired and skipped on save.",
                            slug, f"{total:,}", f"{est_pages:,}", est_pages * 0.7 / 60,
                        )
            # Is the server actually advancing? Log what we asked for against
            # what came back, so a non-advancing endpoint is provable rather
            # than inferred from duplicate counts.
            if page_nr <= 3 and label == "all":
                ids = [str(r.get("id", "?")) for r in raw_items[:2]] + \
                      [str(raw_items[-1].get("id", "?"))]
                log.info("[developmentaid] %s: page %s via %s -> %s items, "
                         "ids[first,2nd,last]=%s", slug, page_nr, describe,
                         len(raw_items), ids)

            items = [it for it in (self._raw_from_item(r, slug) for r in raw_items) if it]
            fresh = {i.opportunity_url for i in items} - seen
            seen |= {i.opportunity_url for i in items}
            if items:
                out_queue.put(("items", (slug, page_nr, items)))
                stats["sent"] += len(items)
            if not fresh:
                stats["_no_fresh"] = True
            return True

    def _set_per_page(self, page, slug: str) -> int:
        """Click the highest available "results per page" option.

        The app exposes it as ul.per-page__list > li.per-page__item (50/100/300).
        Going from 50 to 300 cuts the number of pages to click by six, which on a
        ~118k-listing archive is the difference between 2,400 pages and 400.
        """
        for wanted in ("300", "200", "100"):
            for sel in (f'li.per-page__item:text-is("{wanted}")',
                        f'ul.per-page__list li:text-is("{wanted}")',
                        f'.per-page__list button:text-is("{wanted}")'):
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0 or not loc.is_visible():
                        continue
                    loc.scroll_into_view_if_needed(timeout=3_000)
                    loc.click(timeout=5_000)
                    page.wait_for_timeout(2_500)
                    n = len(page.evaluate(
                        "Array.from(document.querySelectorAll('da-search-card')).map(e=>1)"))
                    log.info("[developmentaid] %s: set results-per-page to %s "
                             "(%s cards rendered)", slug, wanted, n)
                    return n
                except Exception:
                    continue
        log.info("[developmentaid] %s: results-per-page control not found — staying at "
                 "the default page size", slug)
        return 0

    def _go_to_page(self, page, page_nr: int, slug: str) -> bool:
        """Advance the results grid using the site's own pagination control.

        Tries, in order: the numbered link for this page, then a "next" arrow.
        Returns False when no usable control exists (genuine end of results).
        Clicking is what the Angular app listens to — a URL change alone leaves
        the grid untouched, which is the bug this replaces.
        """
        # Selectors confirmed from this app's own markup (da-pagination):
        #   nav.pagination > ul.pagination__list
        #     li.pagination__list-item  > button.pagination__btn      (page numbers)
        #     li.pagination__arrow-right > button.pagination__btn--arrow  (next)
        # They are BUTTONS, not links — matching on <a> found nothing, which is
        # why this reported "no control to reach page 2" on every run.
        selectors = [
            f'li.pagination__list-item button.pagination__btn:text-is("{page_nr}")',
            f'ul.pagination__list button:text-is("{page_nr}")',
            f'button[aria-label="Page {page_nr}"]',
            f'a[aria-label="Page {page_nr}"]',
        ]
        next_selectors = [
            'li.pagination__arrow-right button.pagination__btn--arrow:not([disabled])',
            'li.pagination__arrow-right button:not([disabled])',
            'da-pagination li.pagination__arrow-right button',
            'button[aria-label="Next page"]', 'button[aria-label="Next"]',
            'a[aria-label="Next page"]', 'a[rel="next"]',
            'button.mat-paginator-navigate-next:not([disabled])',
        ]
        for sel in selectors + next_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                loc.scroll_into_view_if_needed(timeout=3_000)
                loc.click(timeout=5_000)
                return True
            except Exception:
                continue
        # Last resort: some builds expose an explicit page-number input.
        try:
            box = page.locator('input[type="number"], input[aria-label*="page" i]').first
            if box.count() and box.is_visible():
                box.fill(str(page_nr), timeout=3_000)
                box.press("Enter")
                return True
        except Exception:
            pass
        # The site shows <da-pagination-restriction-modal> when the account's plan
        # does not permit paging further. That's a subscription limit, not a
        # scraping problem, and it needs saying plainly rather than being reported
        # as "no pagination control found".
        try:
            restricted = page.evaluate(
                """() => {
                    const m = document.querySelector('da-pagination-restriction-modal');
                    if (!m) return null;
                    return (m.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
                }"""
            )
        except Exception:
            restricted = None
        if restricted is not None:
            state = getattr(self, "_session_state", "unknown")
            api_ok = bool(getattr(self, "_api_reached", False))
            if state == "in" and api_ok:
                # Both conditions matter, and the 2026-08-26 run had neither
                # while claiming the first. Declaring "your plan is the ceiling"
                # sends the user to buy a subscription, so it now requires that
                # (a) membership was proved by a signed-in-only control, and
                # (b) the JSON API actually answered this run, so the DOM dialog
                # is the only thing standing in the way rather than the visible
                # symptom of a request that was refused everywhere.
                log.error(
                    "[developmentaid] %s: PAGINATION RESTRICTED BY PLAN — signed in "
                    "as a member, the API answered this run, and the site still "
                    "opened its pagination-restriction dialog instead of page %s. "
                    "Message: %r. This is a limit on the ACCOUNT'S TIER, not on the "
                    "scraper and not on the session: no code change reaches the rest "
                    "of the archive, only a higher plan or a data agreement with "
                    "DevelopmentAid.",
                    slug, page_nr, restricted or "(no text)",
                )
            elif state == "in" and not api_ok:
                log.error(
                    "[developmentaid] %s: pagination dialog at page %s while the "
                    "session looks signed in — but NOT ONE API call succeeded this "
                    "run, so the dialog is the visible half of a request that was "
                    "refused, not proof of a plan limit. Message: %r. Fix the "
                    "refusal first; only if paging still stops here once the API "
                    "answers is the subscription the ceiling.",
                    slug, page_nr, restricted or "(no text)",
                )
            else:
                log.error(
                    "[developmentaid] %s: pagination refused at page %s and this run "
                    "is %s — Message: %r. Do NOT read this as a subscription limit "
                    "yet. A logged-out visitor gets the same dialog. Restore the "
                    "session (scripts/devaid_session.py push) and re-run; if it "
                    "still appears while SIGNED IN, then it is the plan.",
                    slug, page_nr,
                    "NOT signed in" if state == "out" else "of unknown session state",
                    restricted or "(no text)",
                )
            return False

        if page_nr == 2:
            try:
                found = page.evaluate(
                    """() => {
                      const out = [];
                      document.querySelectorAll('*').forEach(e => {
                        const c = (e.className && e.className.toString ? e.className.toString() : '');
                        const t = e.tagName.toLowerCase();
                        if (/pag/i.test(c) || /pag/i.test(t) || /paginat/i.test(e.id || '')) {
                          out.push((t + ' class=' + c).slice(0, 140));
                        }
                      });
                      return Array.from(new Set(out)).slice(0, 25);
                    }"""
                )
                log.warning("[developmentaid] %s: pagination markup on the page: %s",
                            slug, found or "NONE FOUND — the list may use infinite scroll")
            except Exception:
                pass
        log.debug("[developmentaid] %s: no pagination control found for page %s",
                  slug, page_nr)
        return False

    @staticmethod
    def _log_search_api(response) -> None:
        """One-off diagnostic: record the JSON endpoint the app queries.

        Knowing the real search API (and its page parameter) is what would let
        this scraper page through data directly instead of driving the UI, so
        it's worth capturing even though nothing depends on it yet.
        """
        try:
            url = response.url
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            if not re.search(r"search|grant|tender|list", url, re.IGNORECASE):
                return
            log.info("[developmentaid] search API seen: %s %s (%s)",
                     response.request.method, url[:220], response.status)
        except Exception:
            pass

    @staticmethod
    def _signature(cards: list[str]) -> frozenset[str]:
        """Which listings a rendered page is showing — used to tell a genuinely
        new page from the previous one still sitting in the DOM."""
        return frozenset(m for c in cards for m in _CARD_URL.findall(c))

    def _read_when_changed(self, page, prev_signature):
        """Read the rendered cards, waiting for them to differ from the last page.

        Returns (cards, changed). Angular swaps the results grid asynchronously
        after navigation, so reading immediately gives back the previous page's
        DOM — which is exactly how a 474-page walk produced only ~100 distinct
        listings. Polling for a changed signature makes each page's content
        provably its own.
        """
        js = ("Array.from(document.querySelectorAll('da-search-card'))"
              ".map(e => e.outerHTML)")
        waited = 0
        cards = page.evaluate(js)
        if not prev_signature:
            return cards, True          # first page — nothing to compare against
        while waited < _RENDER_CHANGE_TIMEOUT_MS:
            if cards and self._signature(cards) != prev_signature:
                return cards, True
            page.wait_for_timeout(_RENDER_POLL_MS)
            waited += _RENDER_POLL_MS
            cards = page.evaluate(js)
        return cards, cards and self._signature(cards) != prev_signature

    @staticmethod
    def _card_is_open(card_html: str) -> bool:
        """Quick per-card status check used only to decide when to stop paging —
        the real, authoritative filtering happens in parse_listing below. Cards
        with no readable status don't count against the stale streak (better to
        keep looking than stop early on a parsing miss)."""
        soup = BeautifulSoup(card_html, "lxml")
        status = DevelopmentAidScraper._label_map(soup).get("status", "").lower()
        return not status or status == "open"

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        n_cards = n_dupes = n_closed = 0

        for card in soup.select("da-search-card, div.search-card"):
            n_cards += 1
            a = card.select_one("a.search-card__title[href]") or next(
                (x for x in card.find_all("a", href=True) if _VIEW_LINK.search(x["href"])), None
            )
            if a is None:
                continue
            url = a["href"].split("?")[0]
            if url.startswith("/"):
                url = self.website + url
            title = (a.get("title") or a.get_text(" ", strip=True)).strip()
            if url in seen or len(title) < 8:
                n_dupes += 1
                continue
            seen.add(url)

            fields = self._label_map(card)
            status = fields.get("status", "").lower()
            if status and status != "open":
                n_closed += 1
                continue   # respect the site's own status label

            # With a membership login the deadline value is unlocked in the card
            deadline_raw = _clean_sentinel_deadline(fields.get("deadline", ""))
            if not deadline_raw:
                m = _DEADLINE_NEAR.search(card.get_text(" ", strip=True))
                if m:
                    deadline_raw = _clean_sentinel_deadline(m.group(2))

            # Grants label the issuer "Funding Agency"; tenders commonly use
            # "Contracting Authority" or "Client" instead — try all.
            organization = (
                fields.get("funding agency")
                or fields.get("contracting authority")
                or fields.get("client")
                or ""
            )

            # entitytype="grant"/"tender" on the card tells us which section this
            # came from; it's only a hint (2 points) — the keyword classifier
            # still reads the title itself and will promote an "RFP - ..." tender
            # to Category.RFP rather than leaving it as a generic Tender.
            entity_type = (card.get("entitytype") or "").strip().lower()
            category_hint = _ENTITYTYPE_CATEGORY.get(entity_type)

            # Cards carry a "Budget" label (e.g. "EUR 130,000", "USD 1,800,000")
            # — real figures on roughly a quarter of listings, "N/A" on the rest.
            # clean_amount drops the N/A ones.
            budget = clean_amount(fields.get("budget", ""))

            items.append(
                RawOpportunity(
                    title=title[:500],
                    organization=organization[:512],
                    funding_amount=budget,
                    location=fields.get("locations", fields.get("location", ""))[:512],
                    country=fields.get("locations", fields.get("location", ""))[:128],
                    vertical=fields.get("sectors", fields.get("sector", ""))[:256],
                    deadline_raw=deadline_raw[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=category_hint,
                    # only assume-active when the deadline is still locked
                    assume_active=bool(status == "open" and not deadline_raw),
                )
            )
        # Debug level: parse_listing now runs once per page (hundreds of times
        # per run), so this would drown the log at INFO. The manager already
        # reports per-page counts.
        log.debug(
            "[developmentaid] %s cards on page → %s open kept, %s closed/expired, %s duplicates",
            n_cards, len(items), n_closed, n_dupes,
        )
        # Cards on the page but nothing extracted means the markup moved, not
        # that the page was empty — the two are indistinguishable from the
        # per-page counts alone, and at DEBUG this went unseen for entire runs.
        if n_cards and not items and not n_closed and not n_dupes:
            log.warning(
                "[developmentaid] %s cards on %s but none parsed — the card markup "
                "has probably changed (expected a link matching %s)",
                n_cards, page_url, _VIEW_LINK.pattern,
            )
        return items

    @staticmethod
    def _label_map(card) -> dict[str, str]:
        """Cards render '<span>Label:</span><span>Value</span>' pairs."""
        fields: dict[str, str] = {}
        for span in card.find_all("span"):
            label = span.get_text(" ", strip=True)
            if label.endswith(":"):
                value_el = span.find_next_sibling("span")
                if value_el is not None:
                    value = value_el.get("title") or value_el.get_text(" ", strip=True)
                    fields[label.rstrip(":").strip().lower()] = value.strip()
        return fields
