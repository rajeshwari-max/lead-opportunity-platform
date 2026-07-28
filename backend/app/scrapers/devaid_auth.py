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


def connect_interactive_sync() -> bool:
    """Open a VISIBLE browser for the user to log in; returns after they close it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = open_persistent(pw, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, timeout=60_000)
        log.info("[devaid] login window opened — waiting for the user to finish")
        try:
            # Block until the user closes the window (their signal that login is done)
            page.wait_for_event("close", timeout=600_000)  # up to 10 minutes
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
    _CONNECTED_MARKER.write_text("connected", encoding="utf-8")
    log.info("[devaid] login window closed — session saved")
    return True
