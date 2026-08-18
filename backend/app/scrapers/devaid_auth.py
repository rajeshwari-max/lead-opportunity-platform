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

    def _launch():
        try:
            # Real Chrome, real UA — don't override user_agent (a mismatched UA
            # string is itself a bot signal).
            return pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", **common
            )
        except Exception as exc:
            if "already in use" in str(exc).lower():
                raise                    # handled below, not a missing-Chrome case
            log.warning("[devaid] real Chrome not available — using bundled Chromium")
            return pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), user_agent=settings.user_agent, **common
            )

    try:
        return _launch()
    except Exception as exc:
        if "already in use" not in str(exc).lower():
            raise
        # A crashed or killed run leaves Chromium's singleton lock files behind,
        # and every later launch refuses the profile — the error says "another
        # instance of Chromium" even when nothing is running. Clearing the stale
        # locks is the documented recovery; doing it automatically avoids a dead
        # end that can only be escaped by hand on a server.
        _clear_stale_profile_locks()
        log.warning("[devaid] profile was locked by a previous run — cleared and retrying")
        return _launch()


def _clear_stale_profile_locks() -> None:
    """Remove Chromium singleton locks when no browser is actually using them.

    Deliberately conservative: if a live Chromium still has the profile open,
    the locks are left alone and the original error stands. Deleting them under
    a running browser corrupts the profile, which would lose the session this
    whole flow exists to protect.
    """
    import subprocess

    try:
        running = subprocess.run(
            ["pgrep", "-f", str(PROFILE_DIR)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        running = ""                     # pgrep unavailable — assume nothing holds it

    if running:
        log.error(
            "[devaid] the profile is genuinely open in another process (pids: %s). "
            "Stop it before connecting: pkill -f chrome",
            running.replace("\n", ", "),
        )
        return

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (PROFILE_DIR / name).unlink(missing_ok=True)
        except OSError:
            log.warning("[devaid] could not remove stale lock %s", name)


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


# Why the last verification came out the way it did. The connect flow reads
# this so it can say what actually happened instead of guessing.
LAST_VERIFY: dict[str, object] = {}

# Cloudflare interstitials render neither results nor a sign-in link, which is
# indistinguishable from "the page never loaded" unless you look for them.
_CHALLENGE_MARKERS = (
    "just a moment", "attention required", "checking your browser",
    "cf-chl", "challenge-platform", "turnstile", "cf_chl_opt",
)


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
            page.goto("https://www.developmentaid.org/grants/search", timeout=90_000,
                      wait_until="domcontentloaded")

            # A bot check must be identified before anything else. Its page has
            # no cards AND no sign-in link, so every later test reads it as a
            # failed login — which sent the user back to sign in again, over and
            # over, for a problem signing in cannot fix.
            try:
                _t = (page.title() or "").lower()
                _b = (page.content() or "")[:4000].lower()
            except Exception:
                _t = _b = ""
            if any(m in _t or m in _b for m in _CHALLENGE_MARKERS):
                # Give it the chance to clear on its own before deciding.
                page.wait_for_timeout(12_000)
                try:
                    _t = (page.title() or "").lower()
                except Exception:
                    pass
                if any(m in _t for m in _CHALLENGE_MARKERS):
                    LAST_VERIFY.clear()
                    LAST_VERIFY.update({"reason": "challenge", "title": _t})
                    log.error(
                        "[devaid] verify: Cloudflare bot check (page title %r), not a "
                        "login problem. Signing in again will not clear it — the site "
                        "is refusing automated browsing. The session on disk may be "
                        "perfectly valid.", _t,
                    )
                    return False

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

            # "Saw nothing at all" is not the same as "signed in". With no
            # cards, no member signals and no Sign in link, the page simply
            # never rendered — and treating that as success wrote the connected
            # marker on zero evidence, so the dashboard reported a working
            # account while every scrape came back empty.
            nothing_rendered = (
                not member_signals
                and not detail.get("signIn")
                and detail.get("cards", 0) == 0
            )
            if nothing_rendered:
                LAST_VERIFY.clear()
                LAST_VERIFY.update({"reason": "blank", "detail": detail})
                log.error(
                    "[devaid] verify: the search page rendered nothing (no cards, no "
                    "sign-in link, no account menu). The session cannot be confirmed "
                    "— treating it as NOT connected rather than reporting a working "
                    "account that returns no results."
                )
                return False

            LAST_VERIFY.clear()
            LAST_VERIFY.update({"reason": "guest" if clearly_guest else "ok",
                                "detail": detail})
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
            "This server has no screen, so a login window cannot appear here — "
            "and it could never appear on your own computer either, because "
            "clicking this button runs code on the server, not on your machine.\n"
            "\n"
            "Use 'Upload session' just below instead:\n"
            "  1. On your own computer, open the dashboard and click Connect "
            "account — the window appears there, because that machine has a "
            "screen. Log in as usual.\n"
            "  2. Click 'Download session' on that same machine.\n"
            "  3. Come back here and click 'Upload session', choosing the file "
            "you just downloaded.\n"
            "\n"
            "Your password never leaves your own browser; only the resulting "
            "session travels. Every other source scrapes here without a display."
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
        reason = LAST_VERIFY.get("reason")
        if reason == "challenge":
            # Deliberately NOT unlinking the marker: the session may be fine and
            # only the verification was blocked. Throwing it away would discard
            # a good login because a bot check got in the way of checking it.
            log.error(
                "[devaid] could not verify the login — Cloudflare served a bot check "
                "instead of the search page. This is NOT a sign-in problem, and "
                "signing in again will not change it. The saved session has been "
                "kept; try a scrape and see whether it returns cards. If it stays "
                "blocked, DevelopmentAid is refusing automated access and the fix is "
                "an API/data agreement with them rather than more retries."
            )
        elif reason == "blank":
            _CONNECTED_MARKER.unlink(missing_ok=True)
            log.error(
                "[devaid] login NOT confirmed — the search page rendered nothing at "
                "all (no results, no sign-in link). Usually a slow or failed page "
                "load rather than a bad password. Try Connect account again."
            )
        else:
            _CONNECTED_MARKER.unlink(missing_ok=True)
            log.error(
                "[devaid] login NOT completed — the site still shows a 'Sign in' link, "
                "so scrapes would only reach the public first page. Click 'Connect "
                "account' again and finish signing in (email, password, and the "
                "reCAPTCHA) BEFORE closing the window."
            )
    return ok
