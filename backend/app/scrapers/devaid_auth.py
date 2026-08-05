"""DevelopmentAid session management.

Their login is protected by reCAPTCHA, so automated credential login is
unreliable (and defeating CAPTCHAs is off the table). Instead:

  1. The user clicks "Connect account" in the dashboard → a REAL Chrome window
     opens on their screen at the DevelopmentAid login page (persistent profile).
  2. They log in manually — human, legitimate — and close the window.
  3. All later scrapes/expert counts run headless WITH that saved session.

The profile (cookies included) lives in backend/data/devaid_profile/.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.config import settings

log = logging.getLogger("scraper")

LOGIN_URL = "https://www.developmentaid.org/authentication/login"
PROFILE_DIR = Path(__file__).resolve().parents[2] / "data" / "devaid_profile"


_CONNECTED_MARKER = PROFILE_DIR / ".connected"


def has_profile() -> bool:
    """True only after the user completed the interactive login at least once
    (scrapers create the profile folder on their own — that doesn't count)."""
    return _CONNECTED_MARKER.exists()


def open_persistent(pw, headless: bool = True):
    """Launch a browser bound to the saved DevelopmentAid profile.

    Prefers the real installed Google Chrome (channel="chrome") — the site
    returns 403 to Playwright's bundled Chromium (bot detection). Falls back
    to bundled Chromium if Chrome isn't installed.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    common = dict(
        headless=headless,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        # Real Chrome, real UA — don't override user_agent (a mismatched UA
        # string is itself a bot signal).
        return pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", **common
        )
    except Exception:
        log.warning("[devaid] real Chrome not available — using bundled Chromium")
        return pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), user_agent=settings.user_agent, **common
        )


def is_signed_in(page) -> bool:
    """True when the loaded page shows a signed-in session (no visible Sign in)."""
    try:
        return not page.evaluate(
            """() => {
                const el = Array.from(document.querySelectorAll('a, button')).find(
                    e => (e.textContent || '').trim().toLowerCase() === 'sign in'
                );
                if (!el) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
            }"""
        )
    except Exception:
        return False


def verify_session() -> bool:
    """Load a real search page headlessly and report whether we're signed in.

    Logs what it actually saw, so a failure is diagnosable instead of a bare
    "not connected" — the check runs in a different browser process from the
    window the user logged into, and the profile has to carry the session across.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = open_persistent(pw, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.developmentaid.org/grants/search", timeout=60_000)
            try:
                page.wait_for_selector("da-search-card", timeout=45_000)
            except Exception:
                log.warning("[devaid] verify: no result cards rendered")
            try:
                detail = page.evaluate(
                    """() => {
                      const all = Array.from(document.querySelectorAll('a,button'));
                      const vis = e => {
                        const s = window.getComputedStyle(e);
                        return s.display !== 'none' && s.visibility !== 'hidden'
                               && e.offsetParent !== null;
                      };
                      const has = t => all.some(e =>
                        (e.textContent||'').trim().toLowerCase() === t && vis(e));
                      return {
                        signIn:     has('sign in') || has('log in') || has('login'),
                        logout:     has('log out') || has('logout') || has('sign out'),
                        accountMenu: !!document.querySelector(
                          '[class*="avatar" i],[class*="user-menu" i],[class*="my-account" i],'
                          + 'a[href*="/dashboard"],a[href*="/profile"],a[href*="/membership"]'),
                        cards: document.querySelectorAll('da-search-card').length,
                        pagination: document.querySelectorAll(
                          '.pagination a, a[aria-label^="Page"], [aria-label="Next page"],'
                          + 'button.mat-paginator-navigate-next').length,
                      };
                    }"""
                )
            except Exception:
                detail = {}

            # Judge on POSITIVE evidence of a member session. Relying on the
            # absence of a "Sign in" link produced false negatives: this site
            # keeps one in collapsed menus even when signed in, so a genuinely
            # logged-in Premium account was being reported as not connected.
            member_signals = (
                bool(detail.get("logout"))
                or bool(detail.get("accountMenu"))
                or detail.get("pagination", 0) > 0
            )
            clearly_guest = bool(detail.get("signIn")) and not member_signals
            signed_in = not clearly_guest
            log.info("[devaid] verify: signed_in=%s (member_signals=%s) details=%s",
                     signed_in, member_signals, detail)
            if not detail:
                # Couldn't inspect the page at all — don't block the user on
                # that; the scraper reports guest mode on its own if it applies.
                log.warning("[devaid] verify: page could not be inspected — "
                            "assuming the session is usable")
                return True
            return signed_in
        finally:
            try:
                context.close()
            except Exception:
                pass


def connect_interactive_sync() -> bool:
    """Open a VISIBLE browser for the user to log in.

    Returns True only if the saved profile is genuinely signed in afterwards.
    Previously the "connected" marker was written the moment the window closed,
    regardless of what happened inside it — so closing the window after seven
    seconds without logging in still reported success, and every later scrape
    silently ran as a guest (one page of results) while the dashboard claimed
    the account was connected.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = open_persistent(pw, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, timeout=60_000)
        log.info("[devaid] login window opened — waiting for the user to finish")
        try:
            # Block until the user closes the window (their signal that login is done)
            page.wait_for_event("close", timeout=900_000)  # up to 15 minutes
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

    # Chrome writes its cookie store asynchronously; verifying in a fresh
    # browser process immediately after close can read a half-flushed profile.
    import time as _time
    _time.sleep(3)

    ok = False
    try:
        ok = verify_session()
    except Exception:
        log.exception("[devaid] could not verify the session after login")

    if ok:
        _CONNECTED_MARKER.write_text("connected", encoding="utf-8")
        log.info("[devaid] login verified — session saved and usable for scraping")
    else:
        _CONNECTED_MARKER.unlink(missing_ok=True)
        log.error(
            "[devaid] login NOT completed — the site still shows a 'Sign in' link, so "
            "scrapes would only reach the public first page. Click 'Connect account' "
            "again and finish signing in (email, password, and the reCAPTCHA) BEFORE "
            "closing the window."
        )
    return ok
