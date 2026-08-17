"""Browser-free access to DevelopmentAid using an existing signed-in session.

Why this exists
---------------
The scraper currently drives a headless Chromium. That works, but it is heavy
on a small EC2 instance and slow: every page of results costs a full render.
A session is only cookies, so those same requests can be made with plain HTTP.

What this is NOT
----------------
This does not log in. It reuses a session a human created by signing in
normally, exported from a machine with a screen. There is no credential
submission and no CAPTCHA handling here, deliberately: a login automated past a
bot check breaks the moment the challenge changes, and the session route is both
more durable and less adversarial.

The open question this module answers
-------------------------------------
Whether a non-browser client is *allowed through at all* from a datacentre IP.
A headless browser at least presents a plausible TLS and header fingerprint;
raw httpx presents less. `probe()` reports exactly what happens rather than
guessing, so the decision to switch is made on evidence.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.scrapers.devaid_auth import SESSION_FILE, PROFILE_DIR

log = logging.getLogger("scraper")

BASE = "https://www.developmentaid.org"

# Written by scripts/devaid_capture_api.py once the real endpoint is observed.
API_SPEC_FILE = Path(SESSION_FILE).parent / "devaid_api.json"


def _cookies_from_session() -> dict[str, str]:
    """Cookie name -> value from the exported Playwright storage_state.

    storage_state stores cookies as a list of dicts with name/value/domain.
    Only developmentaid.org cookies are taken; a session file can carry
    third-party cookies (analytics, consent) that are noise at best.
    """
    try:
        state = json.loads(Path(SESSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    jar: dict[str, str] = {}
    for c in state.get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if "developmentaid.org" not in domain:
            continue
        name, value = c.get("name"), c.get("value")
        if name and value:
            jar[name] = value
    return jar


def has_session() -> bool:
    return bool(_cookies_from_session())


def build_client(timeout: float | None = None) -> httpx.Client:
    """An httpx client carrying the saved session and browser-like headers.

    The headers matter. A request with no Accept-Language, no Referer and a
    library default User-Agent is trivially identifiable and is what most
    reputation systems reject first. These are the headers a real browser sends
    on the same navigation — not a disguise, just not gratuitously unlike one.
    """
    return httpx.Client(
        base_url=BASE,
        cookies=_cookies_from_session(),
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{BASE}/",
        },
        timeout=timeout or settings.request_timeout,
        follow_redirects=True,
    )


# Markers that only appear for a signed-in member. Checked against the raw HTML
# because there is no DOM to query without a browser.
_MEMBER_MARKERS = ("my-account", "/dashboard", "/profile", "logout", "sign-out")
_GUEST_MARKERS = ("sign in", "log in", "create account", "start free trial")


def probe() -> dict[str, Any]:
    """Can we reach DevelopmentAid over plain HTTP, and are we signed in?

    Returns a dict rather than a bool so a failure is diagnosable: "blocked at
    the network layer" and "reached the site but as a guest" need completely
    different fixes, and a bare False cannot tell them apart.
    """
    result: dict[str, Any] = {
        "session_file": str(SESSION_FILE),
        "session_present": Path(SESSION_FILE).exists(),
        "profile_present": Path(PROFILE_DIR).exists(),
        "cookies": len(_cookies_from_session()),
        "reachable": False,
        "status_code": None,
        "signed_in": None,
        "blocked_by": None,
        "verdict": "",
    }

    if not result["cookies"]:
        result["verdict"] = (
            "No usable session. Export devaid_session.json from a machine where "
            "you have logged in, then upload it here."
        )
        return result

    url = f"{BASE}/tenders/search?hiddenAdvancedFilters=0&sort=deadline.desc"
    try:
        with build_client() as client:
            r = client.get(url)
    except Exception as exc:                        # DNS, TLS, timeout, refused
        result["blocked_by"] = f"{type(exc).__name__}: {exc}"
        result["verdict"] = "Could not reach the site at all over plain HTTP."
        return result

    result["status_code"] = r.status_code
    body = r.text
    low = body.lower()

    if r.status_code in (403, 429) or "cf-mitigated" in {k.lower() for k in r.headers}:
        result["blocked_by"] = "cloudflare"
        result["verdict"] = (
            f"Blocked at the edge (HTTP {r.status_code}). A plain HTTP client is "
            "rejected from this IP, so the browser path has to stay."
        )
        return result

    if "just a moment" in low or "cf-challenge" in low or "turnstile" in low:
        result["blocked_by"] = "cloudflare-challenge"
        result["verdict"] = (
            "Served a Cloudflare interstitial instead of the page. Plain HTTP "
            "will not work from this machine."
        )
        return result

    result["reachable"] = True
    member_hits = [m for m in _MEMBER_MARKERS if m in low]
    guest_hits = [g for g in _GUEST_MARKERS if g in low]
    result["signed_in"] = bool(member_hits) and len(member_hits) >= len(guest_hits)
    result["markers"] = {"member": member_hits, "guest": guest_hits}

    if result["signed_in"]:
        result["verdict"] = (
            "Reached the site over plain HTTP and the session is recognised. "
            "Browser-free scraping is viable from here."
        )
    else:
        result["verdict"] = (
            "Reached the site, but the response looks like a logged-out guest. "
            "The session may have expired, or these cookies are not sufficient "
            "without the browser's localStorage."
        )
    return result


def load_api_spec() -> dict[str, Any] | None:
    """The captured search-API description, if scripts/devaid_capture_api.py ran."""
    try:
        return json.loads(API_SPEC_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
