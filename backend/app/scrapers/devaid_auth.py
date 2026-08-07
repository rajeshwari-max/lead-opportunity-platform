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

# A portable copy of the signed-in session: cookies plus localStorage, the same
# thing Playwright calls a "storage state".
#
# This exists because a headless server cannot show a login window, and that
# window is not automatable — the login is CAPTCHA-protected and scripting it
# is precisely what DevelopmentAid's terms forbid. So the human step stays
# human: you log in yourself, in your own browser, on a machine that has a
# screen. Only the resulting session is carried across, which is the same thing
# copying the profile folder did, minus a few hundred megabytes of Chrome cache.
SESSION_FILE = PROFILE_DIR.parent / "devaid_session.json"


def has_session_file() -> bool:
    return SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 2


def has_profile() -> bool:
    """True when a signed-in session is available by either route.

    Either the user completed the interactive login on this machine, or a
    session exported from such a machine was uploaded here. (Scrapers create
    the profile folder on their own — its mere existence doesn't count.)
    """
    return _CONNECTED_MARKER.exists() or has_session_file()


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

    # An uploaded session takes priority over the local profile, but only when
    # this machine has no interactive login of its own. That ordering matters:
    # on your PC the profile is the live thing and stays authoritative, while
    # on a server the uploaded session is the only thing there is.
    if has_session_file() and not _CONNECTED_MARKER.exists():
        return _context_from_session(pw, common)

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


def _context_from_session(pw, common: dict):
    """Browser context restored from an uploaded session file.

    Not a persistent context: storage_state and launch_persistent_context are
    mutually exclusive in Playwright, so this launches an ordinary browser and
    injects the cookies and localStorage instead. Callers only ever use the
    returned object as a context, so the difference doesn't leak out.
    """
    launch_args = {k: v for k, v in common.items() if k in ("headless", "args")}
    context_args = {
        "viewport": common.get("viewport"),
        "storage_state": str(SESSION_FILE),
        "user_agent": settings.user_agent,
    }
    try:
        browser = pw.chromium.launch(channel="chrome", **launch_args)
    except Exception:
        log.warning("[devaid] real Chrome not available — using bundled Chromium")
        browser = pw.chromium.launch(**launch_args)
    log.info("[devaid] using uploaded session (%s)", SESSION_FILE.name)
    return browser.new_context(**context_args)


def export_session_state() -> dict:
    """Capture the current signed-in session so it can be moved to a server.

    Run on the machine where you logged in. Raises if this machine has no
    session to export, rather than writing an empty file that would fail
    silently once uploaded.
    """
    import json

    from playwright.sync_api import sync_playwright

    if not _CONNECTED_MARKER.exists() and not has_session_file():
        raise RuntimeError(
            "No DevelopmentAid session on this machine to export. Click "
            "'Connect account' and finish signing in first."
        )

    with sync_playwright() as pw:
        context = open_persistent(pw, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # Load a real page first: cookies are set per-origin and a context
            # that has never navigated exports an empty state.
            page.goto("https://www.developmentaid.org/grants/search", timeout=60_000)
            page.wait_for_timeout(2000)
            signed_in = is_signed_in(page)
            state = context.storage_state()
        finally:
            try:
                context.close()
            except Exception:
                pass

    if not signed_in:
        raise RuntimeError(
            "The saved session is no longer signed in, so exporting it would "
            "just move an expired session. Click 'Connect account' and log in "
            "again first."
        )
    if not state.get("cookies"):
        raise RuntimeError("Session captured no cookies — nothing useful to export.")

    log.info("[devaid] exported session: %s cookie(s)", len(state["cookies"]))
    return json.loads(json.dumps(state))     # plain JSON-safe dict


def import_session_state(state: dict) -> int:
    """Install a session exported from another machine. Returns cookie count."""
    import json

    if not isinstance(state, dict) or not state.get("cookies"):
        raise ValueError(
            "That file doesn't look like a DevelopmentAid session — expected "
            "JSON with a 'cookies' list, produced by Download session."
        )
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(state), encoding="utf-8")
    try:
        SESSION_FILE.chmod(0o600)            # it is a live credential
    except OSError:
        pass
    log.info("[devaid] imported session: %s cookie(s)", len(state["cookies"]))
    return len(state["cookies"])


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


class NoDisplayError(RuntimeError):
    """Raised when there is no screen to show the login window on."""


def display_available() -> bool:
    """Whether a visible browser window can actually be shown here.

    Windows and macOS always have a desktop. Linux only does when an X or
    Wayland display is attached, which a headless EC2 instance has not.
    """
    import os
    import sys

    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


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

    # Checked before launching, so a headless server gets an explanation and a
    # route forward instead of a Playwright stack trace about a missing
    # XServer. This step cannot be made headless: its entire purpose is a human
    # typing into the window, and no amount of configuration substitutes for a
    # screen. (Automating the credential entry is not an option either — the
    # login is CAPTCHA-protected and scripting it is what their terms forbid.)
    if not display_available():
        raise NoDisplayError(
            "This server has no display, so the DevelopmentAid login window "
            "cannot be shown. Three ways forward:\n"
            "  1. Copy an already-authenticated profile from a machine where "
            "you have logged in: scp -r backend/data/devaid_profile to this "
            "host's backend/data/.\n"
            "  2. Connect over SSH with X11 forwarding (ssh -Y) and an X "
            "server on your desktop, then click Connect again.\n"
            "  3. Leave DevelopmentAid running on your own machine and let "
            "this server scrape the other sources.\n"
            "Every other source works here without a display."
        )

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
