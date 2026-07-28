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

import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

log = logging.getLogger("scraper")

_VIEW_LINK = re.compile(r"/(?:grants|tenders)/view/\d+", re.IGNORECASE)
_DEADLINE_NEAR = re.compile(
    r"(deadline|closing)[^\d]{0,20}(\d{1,2}\s+\w{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE,
)
_MAX_PAGES = 2000        # ultimate safety cap only — the stale-streak stop below
                         # should always end the walk well before this in practice.
_STALE_OPEN_STREAK = 25  # consecutive pages with zero "Open" cards before stopping —
                         # the site sorts by Modified Date by default, so active
                         # listings cluster toward the front; deep pages are almost
                         # entirely old, closed history.

# Every section scraped in one run, tagged with a short slug (for log/debug
# file names) — add another (url, slug) pair here for any future section.
_SECTIONS: list[tuple[str, str]] = [
    ("https://www.developmentaid.org/grants/search", "grants"),
    ("https://www.developmentaid.org/tenders/search", "tenders"),
]

_ENTITYTYPE_CATEGORY = {
    "grant": Category.GRANT,
    "tender": Category.TENDER,
}


def _page_url(base_url: str, page_nr: int) -> str:
    """https://.../grants/search?pageNr=N — preserves any other query params."""
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query)
    qs["pageNr"] = [str(page_nr)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


@register
class DevelopmentAidScraper(BaseScraper):
    name = "developmentaid"
    display_name = "DevelopmentAid"
    website = "https://www.developmentaid.org"
    start_url = _SECTIONS[0][0]   # BaseScraper needs one; the real walk covers all sections
    requires_js = True   # Angular SPA — always rendered via Playwright

    # All sections/pages are accumulated in one rendered session (URL pagination).
    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def _fetch_rendered_sync(self, url: str) -> str:
        from playwright.sync_api import sync_playwright

        from app.scrapers.devaid_auth import open_persistent

        with sync_playwright() as pw:
            # persistent profile = the session the user saved via "Connect account"
            browser = open_persistent(pw, headless=True)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                all_cards: list[str] = []
                session_checked = False

                for section_url, slug in _SECTIONS:
                    resp = page.goto(section_url, timeout=int(settings.request_timeout * 1000))
                    log.info("[developmentaid] %s page 1 HTTP status: %s",
                             slug, resp.status if resp else "no response (cached/redirect)")

                    # Dismiss the cookie-consent banner (harmless if absent).
                    try:
                        page.get_by_role("button", name="I Accept").click(timeout=5_000)
                    except Exception:
                        pass

                    try:
                        # First page's Angular bootstrap has been taking 20-30s+
                        # lately — give it generous room to hydrate.
                        page.wait_for_selector("da-search-card", timeout=45_000)
                    except Exception:
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
                    checked_this_section = False
                    signed_out_detected = False
                    if not session_checked:
                        session_checked = True
                        checked_this_section = True
                        try:
                            signed_out_detected = page.evaluate(
                                """() => {
                                    const el = Array.from(document.querySelectorAll('a, button')).find(
                                        e => (e.textContent || '').trim().toLowerCase() === 'sign in'
                                    );
                                    if (!el) return false;
                                    const style = window.getComputedStyle(el);
                                    const visible = style.display !== 'none'
                                        && style.visibility !== 'hidden'
                                        && el.offsetParent !== null;
                                    return visible;
                                }"""
                            )
                        except Exception:
                            signed_out_detected = False

                    section_cards: list[str] = []
                    stale_open_streak = 0
                    pages_reached = 0
                    hit_safety_cap = True   # flips False on any organic stop below

                    for page_nr in range(1, _MAX_PAGES + 1):
                        if page_nr > 1:
                            try:
                                page.goto(
                                    _page_url(section_url, page_nr),
                                    timeout=int(settings.request_timeout * 1000),
                                )
                                page.wait_for_selector("da-search-card", timeout=15_000)
                            except Exception:
                                log.info(
                                    "[developmentaid] %s: page %s empty/unreachable — "
                                    "end of listings", slug, page_nr,
                                )
                                hit_safety_cap = False
                                break

                        cards = page.evaluate(
                            "Array.from(document.querySelectorAll('da-search-card'))"
                            ".map(e => e.outerHTML)"
                        )
                        if not cards:
                            log.info("[developmentaid] %s: page %s returned no cards "
                                      "— stopping", slug, page_nr)
                            hit_safety_cap = False
                            break

                        section_cards.extend(cards)
                        pages_reached = page_nr

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

                    log.info("[developmentaid] %s: walked %s pages, %s cards collected",
                             slug, pages_reached, len(section_cards))
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

                    all_cards.extend(section_cards)

                return "<html><body>" + "".join(all_cards) + "</body></html>"
            finally:
                browser.close()

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
            deadline_raw = fields.get("deadline", "")
            if not deadline_raw:
                m = _DEADLINE_NEAR.search(card.get_text(" ", strip=True))
                if m:
                    deadline_raw = m.group(2)

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

            items.append(
                RawOpportunity(
                    title=title[:500],
                    organization=organization[:512],
                    location=fields.get("locations", fields.get("location", ""))[:512],
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
        log.info(
            "[developmentaid] %s cards on site → %s open kept, %s closed/expired, %s duplicates",
            n_cards, len(items), n_closed, n_dupes,
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
