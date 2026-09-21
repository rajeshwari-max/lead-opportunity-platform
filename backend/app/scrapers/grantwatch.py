"""GrantWatch International scraper (https://international.grantwatch.com/new-grants.php).

Server-rendered cards (title link /grant/<id>/..., 'Deadline: MM/DD/YY' or
'Ongoing', summary, GrantWatch ID#). Dates are US-format (dayfirst=False).
Detail pages are subscription-gated; the public listing already carries
title/deadline/summary.

Pagination (re-measured 2026-09-21 in a real browser)
-----------------------------------------------------
The pager buttons call setPage('N'), which is a plain navigation to
new-grants.php?pageNum=N — not an AJAX update. So every page is an ordinary
URL, and this walks them by URL.

It used to click the '›' button instead, and that capped the walk at page 4:
the pager only ever renders buttons 1-4, and on page 4 there is no '›'. But
the listing does NOT end there. pageNum=5 .. pageNum=11 each return 14 more
grants (many with deadlines well into 2027), and pageNum=12 returns none.
Measured: 154 unique grants by URL, against at most 56 reachable by clicking.
The pager is lying about the size of the list, so it cannot be the stop rule.

The stop rule is the result rows themselves: BaseScraper.crawl stops on a
page with no grants (pageNum=12+ today) and on a page whose rows repeat one
already walked. _MAX_PAGES is only a backstop against a site change that
makes every page non-empty.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

_GRANT_LINK = re.compile(r"/grant/(\d+)/", re.IGNORECASE)
_DEADLINE = re.compile(r"Deadline\s*:?\s*([0-9/]{6,10}|Ongoing)", re.IGNORECASE)
_MAX_PAGES = 40
# How long to let a JS challenge resolve before calling it a hard block.
# Cloudflare's own interstitial advertises ~5s; 45 allows for a slow round trip
# and a retry, and stops well short of hanging the run.
_CHALLENGE_WAIT_S = 45
_CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                     "verify you are human", "one moment", "please wait")



def page_url_for(url: str, page: int) -> str:
    """The same listing URL with pageNum set, every other parameter kept."""
    parts = urlsplit(url)
    query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    query["pageNum"] = str(page)
    return urlunsplit(parts._replace(query=urlencode(query)))


@register
class GrantWatchScraper(BaseScraper):
    name = "grantwatch"
    display_name = "GrantWatch Intl"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    website = "https://international.grantwatch.com"
    start_url = "https://international.grantwatch.com/new-grants.php"
    # Rendered when Playwright is available, for Cloudflare's JS challenge —
    # not for pagination, which is plain URLs (see the module docstring).
    prefer_js = True
    # ~11 pages. Walk all of them every run: the deeper pages carry grants
    # the pager never exposes, and re-reading a page we already hold is what
    # refreshes a deadline that was missing before.
    stale_page_streak_override = 0

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        if page_number >= _MAX_PAGES:
            log.warning("[grantwatch] stopped at the %s-page backstop — the "
                        "listing has never been this long; check whether "
                        "out-of-range pages stopped coming back empty", _MAX_PAGES)
            return None
        return PageRequest(page_url_for(page_url, page_number + 1))

    @staticmethod
    def _wait_out_challenge(page, seconds: int = _CHALLENGE_WAIT_S) -> bool:
        """Let a JS interstitial resolve. True once the real page is showing.

        Polls the title rather than sleeping a fixed amount, so a page that
        clears in three seconds costs three seconds.
        """
        import time as _time

        limit = _time.monotonic() + seconds
        challenged = False
        while _time.monotonic() < limit:
            try:
                title = (page.title() or "").lower()
            except Exception:
                title = ""
            if not any(t in title for t in _CHALLENGE_TITLES):
                if challenged:
                    log.info("[grantwatch] the challenge cleared after %.0fs",
                             seconds - (limit - _time.monotonic()))
                return True
            challenged = True
            page.wait_for_timeout(2_000)
        return False


    def _fetch_rendered_sync(self, url: str) -> str:
        """Render ONE listing page, waiting out a Cloudflare challenge if shown."""
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        with sync_playwright() as pw:
            # site_auth.open_context, NOT pw.chromium.launch + a hard-coded
            # user_agent. This used to be:
            #
            #     browser = pw.chromium.launch(headless=True)
            #     page = browser.new_page(user_agent=settings.user_agent)
            #
            # settings.user_agent hard-codes "Chrome/126.0.0.0", but a browser
            # also announces its version in the Sec-CH-UA client hints, which
            # come from the real build and cannot be overridden that way. So
            # every request said "I am Chrome 126" in one header and something
            # else in the next — a stock bot signature, and the documented cause
            # of ADB being refused by Cloudflare for two days (see site_auth.py).
            #
            # open_context keeps the browser's own identity, removes only the
            # word "Headless" from it, and drops navigator.webdriver.
            context = site_auth.open_context(pw, self.name, headless=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

                # Cloudflare's JS challenge ("Just a moment...") clears itself
                # in a real browser after a few seconds. Waiting for it is not
                # defeating the check — it is doing exactly what the check asks:
                # load the page, run its JavaScript, wait.
                #
                # BaseScraper._fetch_rendered_sync already does this for every
                # other JS source. This scraper overrides that method and so
                # lost the behaviour, which is why a 22-second run ended on the
                # challenge page and reported zero grants.
                if not self._wait_out_challenge(page):
                    log.error(
                        "[grantwatch] still on Cloudflare's challenge after "
                        "%ss. This is not something a scraper change fixes: the "
                        "challenge is not clearing for this client. Most likely "
                        "the server's datacenter IP is refused outright — the "
                        "same page opens normally in an ordinary browser. "
                        "Options are to run this source from a different "
                        "network, ask GrantWatch for feed access, or drop it.",
                        _CHALLENGE_WAIT_S,
                    )
                    return page.content()   # let parse_listing report it too

                # One page per call. Pagination is done by URL in
                # next_page(); this used to click '›' through the pager here,
                # which could never get past page 4.
                return page.content()
            finally:
                # context.close() left the owning Browser running — see
                # site_auth.close_owned.
                site_auth.close_owned(context)

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        anchors = soup.find_all("a", href=True)

        for a in anchors:
            m = _GRANT_LINK.search(a["href"])
            title = a.get_text(" ", strip=True)
            if not m or len(title) < 15 or title.lower() == "view grant":
                continue
            url = a["href"] if a["href"].startswith("http") else self.website + a["href"]
            if url in seen:
                continue
            seen.add(url)

            # card container: nearest ancestor that includes the Deadline label
            card = a
            card_text = ""
            for _ in range(6):
                card = card.parent
                if card is None:
                    break
                card_text = card.get_text(" ", strip=True)
                if "Deadline" in card_text:
                    break
            deadline = _DEADLINE.search(card_text or "")

            # summary = longest paragraph-ish text minus the title
            summary = ""
            if card is not None:
                for p in card.find_all("p"):
                    text = p.get_text(" ", strip=True)
                    if len(text) > 80 and "GrantWatch" not in text[:20]:
                        summary = text[:1000]
                        break

            items.append(
                RawOpportunity(
                    title=title[:500],
                    deadline_raw=(deadline.group(1) if deadline else "")[:64],
                    summary=summary,
                    country="Global",
                    region="Global",
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,
                    dayfirst=False,   # US site: 09/18/26 = September 18
                )
            )

        if not items:
            # "0 items" is the least useful thing this could report. The page
            # was rendered, the browser presented itself correctly, and 21
            # seconds were spent — so the question is what the HTML actually
            # contained, and only this function can answer it.
            self._report_empty(soup, anchors, html)
        return items

    def _report_empty(self, soup, anchors, html: str) -> None:
        """Say what the page held instead of grants, in one log line.

        Distinguishes the three things that all look like "0 items": the page
        never rendered its listing, the listing is there but the /grant/<id>/
        URL shape changed, or the site served a bot wall.
        """
        title = (soup.title.get_text(strip=True) if soup.title else "")[:80]
        text = soup.get_text(" ", strip=True)
        low = f"{title} {text[:400]}".lower()
        wall = next((w for w in ("just a moment", "attention required",
                                 "verify you are human", "access denied",
                                 "enable javascript", "unusual traffic")
                     if w in low), "")
        if wall:
            log.error("[grantwatch] the page is a bot wall, not a listing "
                      "(matched %r, title=%r). A parser change cannot fix this.",
                      wall, title)
            return

        # What link shapes ARE on the page? The most common second path segment
        # is usually the answer — if grants moved to /grants/<slug>/ this names
        # it immediately.
        from collections import Counter
        shapes = Counter()
        for a in anchors:
            href = a.get("href") or ""
            parts = [p for p in href.split("?")[0].split("/") if p and ":" not in p]
            if parts:
                shapes["/" + parts[0]] += 1
        log.error(
            "[grantwatch] rendered %s characters and %s link(s), none matching "
            "%s. Commonest link prefixes: %s. Page title: %r. If a prefix below "
            "looks like the new home for grants, _GRANT_LINK needs updating; if "
            "the list is all navigation, the listing never rendered.",
            len(html), len(anchors), _GRANT_LINK.pattern,
            shapes.most_common(8), title,
        )
