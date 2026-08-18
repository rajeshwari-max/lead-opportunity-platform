"""Per-site logged-in browser sessions, for sources that gate their listings.

Generalises what devaid_auth.py does for DevelopmentAid: some boards show
little or nothing to an anonymous visitor, so the scraper has to be signed in.

The model is deliberately the same one, for the same reason
--------------------------------------------------------
A human signs in **once**, in a real browser window, on a machine with a screen.
Playwright keeps that browser's profile directory, and every later scrape opens
the same profile and is already authenticated.

No password is stored, read, or typed by this code. That is not squeamishness:

  * these are shared organisational accounts, and a stored password is one
    repository leak away from being everyone's problem;
  * most of these sites put a CAPTCHA on the login form, so an automated login
    means defeating a bot check — which breaks the moment they change it, and
    is not something worth building on;
  * a session survives password rotation. Credentials in a config file do not.

For a server with no screen, `export_session` / `import_session` move the
session across, exactly as the DevelopmentAid flow already does.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, settings

log = logging.getLogger("scraper")

SESSIONS_DIR = BASE_DIR / "data" / "sessions"


# Sites known to need a login, keyed by the scraper `name`. The URL is where the
# human is sent to sign in; `check` is a substring that only appears once
# authenticated, used to verify rather than assume.
LOGIN_SITES: dict[str, dict[str, str]] = {
    # DevelopmentAid predates this module and has its own auth code
    # (devaid_auth.py), because its scraper drives the browser itself rather
    # than going through BaseScraper. It is listed here so there is ONE panel
    # for site logins instead of two that behave differently — the functions
    # below delegate to devaid_auth so both paths share one profile on disk.
    # Two stores would be worse than none: connecting in one place while the
    # scraper read the other would look connected and scrape as a guest.
    "developmentaid": {
        "display": "DevelopmentAid",
        "login_url": "https://www.developmentaid.org/tenders/search",
        "check": "logout",
    },
    "world_bank": {
        "display": "World Bank",
        "login_url": "https://projects.worldbank.org/en/projects-operations/procurement",
        "check": "sign out",
    },
    "un_partner_portal": {
        "display": "UN Partner Portal",
        "login_url": "https://www.unpartnerportal.org/login",
        "check": "logout",
    },
    "adb_tenders": {
        "display": "ADB Tenders",
        "login_url": "https://www.adb.org/user/login",
        "check": "log out",
    },
}


def _is_devaid(source: str) -> bool:
    return source == "developmentaid"


def profile_dir(source: str) -> Path:
    if _is_devaid(source):
        from app.scrapers.devaid_auth import PROFILE_DIR
        return PROFILE_DIR
    return SESSIONS_DIR / source / "profile"


def session_file(source: str) -> Path:
    if _is_devaid(source):
        from app.scrapers.devaid_auth import SESSION_FILE
        return SESSION_FILE
    return SESSIONS_DIR / source / "session.json"


def has_session(source: str) -> bool:
    """True when this source has either a browser profile or an imported session."""
    p = profile_dir(source)
    return session_file(source).exists() or (p.exists() and any(p.iterdir()))


def status_all() -> list[dict[str, Any]]:
    """Connection state for every site that needs a login, for the dashboard."""
    return [
        {
            "source": name,
            "display": meta["display"],
            "login_url": meta["login_url"],
            "connected": has_session(name),
            "via": ("imported session" if session_file(name).exists()
                    else "browser profile" if has_session(name) else None),
        }
        for name, meta in LOGIN_SITES.items()
    ]


def open_context(pw, source: str, headless: bool = True):
    """A browser context carrying this source's saved session, if any.

    Falls back to an ordinary anonymous context when nothing is saved, so a
    scraper can call this unconditionally: an un-connected site still scrapes
    whatever it shows the public rather than failing outright.
    """
    # launch() and launch_persistent_context() do NOT take the same arguments.
    # `viewport` belongs to a context, not a browser, and passing it to launch()
    # raises TypeError — which is what made ADB fail on every run with
    # "BrowserType.launch() got an unexpected keyword argument 'viewport'"
    # and get misread as a Cloudflare block for two days.
    launch_args = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    context_args = {
        "viewport": {"width": 1400, "height": 900},
        "user_agent": settings.user_agent,
    }

    sfile = session_file(source)
    if sfile.exists():
        browser = pw.chromium.launch(**launch_args)
        return browser.new_context(storage_state=str(sfile), **context_args)

    pdir = profile_dir(source)
    if pdir.exists() and any(pdir.iterdir()):
        # A persistent context is launched, not created, so it takes both sets.
        return pw.chromium.launch_persistent_context(
            str(pdir), **launch_args, **context_args
        )

    browser = pw.chromium.launch(**launch_args)
    return browser.new_context(**context_args)


class NoDisplayError(RuntimeError):
    """Raised when an interactive login is attempted on a machine with no screen."""


def display_available() -> bool:
    import os
    import sys

    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def connect_interactive(source: str, timeout_s: int = 300) -> bool:
    """Open a visible browser so a person can sign in. Returns True if they did.

    Blocking, and only usable where there is a screen. On a server, use
    export_session on a desktop and import_session here instead.
    """
    if source not in LOGIN_SITES:
        raise KeyError(f"{source} is not configured as a login site")
    if _is_devaid(source):
        # Reuse the existing flow, which also verifies the session afterwards
        # rather than assuming a closed window means success.
        from app.scrapers.devaid_auth import connect_interactive_sync
        return connect_interactive_sync()

    if not display_available():
        raise NoDisplayError(
            "This machine has no display, so a login window cannot be shown. "
            "Sign in on your computer, download the session, and upload it here."
        )

    from playwright.sync_api import sync_playwright

    meta = LOGIN_SITES[source]
    pdir = profile_dir(source)
    pdir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(pdir), headless=False, viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )  # persistent_context accepts viewport; plain launch() does not
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(meta["login_url"], timeout=60_000)
        log.info("[%s] waiting for interactive login (up to %ss)", source, timeout_s)
        try:
            # The person closing the window is the signal that they are done.
            page.wait_for_event("close", timeout=timeout_s * 1000)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass

    return has_session(source)


def export_session(source: str) -> dict[str, Any]:
    """storage_state for this source, to move to a server."""
    if _is_devaid(source):
        from app.scrapers.devaid_auth import export_session_state
        return export_session_state()

    from playwright.sync_api import sync_playwright

    pdir = profile_dir(source)
    if not (pdir.exists() and any(pdir.iterdir())):
        raise FileNotFoundError(f"No saved login for {source} on this machine")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(pdir), headless=True)
        try:
            state = ctx.storage_state()
        finally:
            ctx.close()
    return state


def import_session(source: str, state: dict[str, Any]) -> int:
    """Install a session exported elsewhere. Returns the cookie count."""
    if _is_devaid(source):
        from app.scrapers.devaid_auth import import_session_state
        return import_session_state(state)

    if source not in LOGIN_SITES:
        raise KeyError(f"{source} is not configured as a login site")
    cookies = state.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        # An expired or truncated session is still valid JSON, so shape alone
        # is not proof — but zero cookies definitely cannot authenticate.
        raise ValueError("That file contains no cookies, so it cannot sign in.")

    sfile = session_file(source)
    sfile.parent.mkdir(parents=True, exist_ok=True)
    sfile.write_text(json.dumps(state), encoding="utf-8")
    log.info("[%s] imported session with %s cookies", source, len(cookies))
    return len(cookies)


def forget(source: str) -> None:
    """Drop a saved session — how you disconnect an account."""
    import shutil

    shutil.rmtree(SESSIONS_DIR / source, ignore_errors=True)
