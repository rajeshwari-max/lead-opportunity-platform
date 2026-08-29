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
import sys
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


def _own_chrome_dir() -> Path | None:
    """The user's everyday Chrome profile directory, if they configured one.

    Returns None when unset or missing. A configured-but-missing path is a
    warning rather than an error: falling back to the dedicated profile still
    scrapes, and a typo in a path should not take the whole source down.
    """
    raw = (settings.devaid_chrome_user_data_dir or "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        log.warning("[devaid] LOP_DEVAID_CHROME_USER_DATA_DIR points at %s, which does "
                    "not exist — falling back to the dedicated profile", path)
        return None
    return path


MIRROR_DIR = PROFILE_DIR.parent / "devaid_chrome_mirror"

# The few files that carry a logged-in session. Everything else in a Chrome
# profile is history, cache, extensions and site data — hundreds of megabytes
# that would make this slow and would copy far more of your browsing than this
# needs. Paths are relative to the profile directory, except "Local State",
# which sits at the user-data root.
_SESSION_FILES = (
    "Network/Cookies",       # modern Chrome
    "Cookies",               # older layout, harmless if absent
    "Preferences",
    "Secure Preferences",
)


def _mirror_own_chrome(root: Path, profile: str) -> Path:
    """Copy just the session out of your real Chrome profile into a working dir.

    Chrome 136 and later REFUSE to enable remote debugging against the default
    user-data directory:

        DevTools remote debugging requires a non-default data directory.
        Specify this using --user-data-dir.

    Playwright drives Chrome over CDP, so pointing it straight at your live
    profile cannot work on any current Chrome — that is a deliberate security
    change (it stopped malware attaching a debugger to a live profile and
    reading its cookies), and no combination of flags gets around it.

    What still works is a copy. Chrome objects to debugging the live profile,
    not to the cookies themselves, and the cookie encryption key in "Local
    State" is protected by Windows DPAPI bound to YOUR user account — so a copy
    made by you, on your machine, decrypts normally and arrives signed in.

    Refreshed on every launch so an expiring session is picked up rather than
    going stale in the copy. Only the handful of files above are taken.
    """
    import shutil

    MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    (MIRROR_DIR / profile).mkdir(parents=True, exist_ok=True)

    copied, missing = [], []
    # Local State holds the DPAPI-wrapped key the Cookies file is encrypted
    # with. Without it the cookies copy over but cannot be decrypted, and the
    # browser opens signed out — which looks exactly like an expired session.
    src_state = root / "Local State"
    if src_state.is_file():
        shutil.copy2(src_state, MIRROR_DIR / "Local State")
        copied.append("Local State")
    else:
        missing.append("Local State")

    for rel in _SESSION_FILES:
        src = root / profile / rel
        if not src.is_file():
            continue
        dst = MIRROR_DIR / profile / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    if not any(c.endswith("Cookies") for c in copied):
        log.warning(
            "[devaid] no cookie database found under %s — the mirrored profile "
            "will not be signed in. Check LOP_DEVAID_CHROME_PROFILE_DIR names the "
            "profile you actually use (chrome://version -> Profile Path).",
            root / profile,
        )
    if missing:
        log.warning("[devaid] could not copy %s from your Chrome profile; cookies "
                    "may not decrypt", ", ".join(missing))
    log.info("[devaid] mirrored %s from your Chrome profile", ", ".join(copied))
    return MIRROR_DIR


def _chrome_is_running(user_data_dir: Path) -> str:
    """Non-empty reason string when Chrome currently holds this profile.

    Chrome keeps an exclusive lock on its user-data directory. Launching a
    second browser against it does not merely fail — it can leave the profile
    inconsistent, and this is the user's real browser with all their other
    sessions in it. So this refuses on any positive sign rather than trying and
    hoping.

    Windows: the lock file cannot be opened for writing while Chrome has it,
    which is a more reliable signal than process names. POSIX: pgrep.
    """
    lock = user_data_dir / "lockfile"                 # Windows
    if lock.exists():
        try:
            with open(lock, "a"):
                pass
        except OSError:
            return "its lockfile is held"
    singleton = user_data_dir / "SingletonLock"       # macOS / Linux
    if singleton.exists() or singleton.is_symlink():
        return "SingletonLock is present"
    try:
        import subprocess
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                                 capture_output=True, text=True, timeout=10).stdout
            if "chrome.exe" in out.lower():
                return "chrome.exe is running"
        else:
            if subprocess.run(["pgrep", "-x", "chrome"],
                              capture_output=True, timeout=10).returncode == 0:
                return "a chrome process is running"
    except Exception:
        pass                       # can't tell — the lock checks above stand
    return ""


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
        locale="en-US",
        args=[
            "--disable-blink-features=AutomationControlled",
            # Chrome's own "this is a test browser" infobar/flag. Left on, it is
            # one more thing that differs from the browser the human logged in
            # with, and Cloudflare's cf_clearance cookie is issued against that
            # browser's fingerprint.
            "--no-first-run",
            "--no-default-browser-check",
        ],
        # Playwright adds --enable-automation by default, which sets
        # navigator.webdriver and marks the session as automated.
        ignore_default_args=["--enable-automation"],
    )

    # Your everyday Chrome, when configured. Highest priority, because if you
    # pointed us at it you are already signed in there and no other route is
    # needed on this machine.
    own = _own_chrome_dir()
    if own:
        blocker = _chrome_is_running(own)
        if blocker:
            raise RuntimeError(
                f"Chrome is using your profile at {own}, so the session cannot be "
                f"copied out of it ({blocker}). Close Chrome completely — including "
                "any background instance in the system tray — and run again. Or "
                "unset LOP_DEVAID_CHROME_USER_DATA_DIR to go back to the dedicated "
                "profile, which needs no Chrome to be closed."
            )
        profile = settings.devaid_chrome_profile_dir.strip() or "Default"
        mirror = _mirror_own_chrome(own, profile)
        log.info("[devaid] using the session from your own Chrome profile "
                 "%s (%s), mirrored to %s", own, profile, mirror)
        ctx = pw.chromium.launch_persistent_context(
            str(mirror), channel="chrome",
            args=common["args"] + [f"--profile-directory={profile}"],
            **{k: v for k, v in common.items() if k != "args"},
        )
        return _mask_headless(ctx)

    # An uploaded session takes priority over the local profile, but only when
    # this machine has no interactive login of its own. That ordering matters:
    # on your PC the profile is the live thing and stays authoritative, while
    # on a server the uploaded session is the only thing there is.
    if has_session_file() and not _CONNECTED_MARKER.exists():
        return _context_from_session(pw, common)

    # The user agent is NOT set from settings here, in either mode.
    #
    # It used to be, for headless only: settings.user_agent hard-codes
    # "Chrome/126.0.0.0". The Chrome actually installed is 151, and a browser
    # announces its version twice — in the User-Agent header AND in the
    # Sec-CH-UA / Sec-CH-UA-Full-Version-List client hints, which come from the
    # real build and cannot be faked by Playwright's user_agent option. So every
    # headless request said "I am Chrome 126" in one header and "I am Chrome 151"
    # in the next. That contradiction is a textbook bot signal, and Cloudflare's
    # cf_clearance cookie is bound to the exact user agent that earned it — so
    # the clearance the human's headed login obtained (real UA) was void the
    # moment a headless run presented the fake one. Result: HTTP 403 and a
    # "Just a moment..." challenge on every single run, both sections, 0 rows.
    #
    # What replaces it: keep the browser's own identity, and only remove the
    # word "Headless" from it (see _mask_headless), with the matching client
    # hints supplied from the browser's own values. Headed and headless then
    # present the same identity, which is what keeps a session usable.
    def _launch():
        try:
            return pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", **common
            )
        except Exception as exc:
            if "already in use" in str(exc).lower():
                raise                    # handled below, not a missing-Chrome case
            log.warning("[devaid] real Chrome not available — using bundled Chromium")
            return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **common)

    try:
        return _mask_headless(_launch())
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
        return _mask_headless(_launch())


# The canonical implementation lives in site_auth, because every
# JS-rendered source needs the same browser identity — not just this one.
from app.scrapers import site_auth  # noqa: E402
from app.scrapers.site_auth import mask_headless as _mask_headless  # noqa: E402


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
    injects the cookies and localStorage instead.

    That difference DOES leak out, and this docstring used to claim otherwise.
    A persistent context owns its browser process, so closing it is enough. The
    object returned here does not: `browser` below is a local that goes out of
    scope, so a caller doing `context.close()` closes the context and leaves
    Chromium running. On DevelopmentAid — a run that opens the browser for tens
    of minutes across hundreds of page loads — that is the worst instance of
    the leak in the codebase.

    Callers must tear this down with site_auth.close_owned(), which follows
    `context.browser` and closes the owner when there is one. Never
    `context.close()` alone.
    """
    launch_args = {k: v for k, v in common.items()
                   if k in ("headless", "args", "ignore_default_args")}
    # No user_agent here either — same reason as open_persistent: a hard-coded
    # version contradicts the Sec-CH-UA client hints the real build sends, and
    # the session's cf_clearance cookie is bound to the identity that earned it.
    context_args = {
        "viewport": common.get("viewport"),
        "storage_state": str(SESSION_FILE),
        "locale": common.get("locale", "en-US"),
    }
    try:
        browser = pw.chromium.launch(channel="chrome", **launch_args)
    except Exception:
        log.warning("[devaid] real Chrome not available — using bundled Chromium")
        browser = pw.chromium.launch(**launch_args)
    log.info("[devaid] using uploaded session (%s)", SESSION_FILE.name)
    return _mask_headless(browser.new_context(**context_args))


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
                site_auth.close_owned(context)
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


# --------------------------------------------------------------- membership
# One detector, used by verify_session() here and by the scraper, because two
# detectors is how this project ended up with `status` reporting "signed in:
# True" and `export` refusing in the same minute, on the same machine, against
# the same profile:
#
#     status : signed in  : True
#     push   : The saved session is no longer signed in
#
# They disagreed because they asked different questions. `is_signed_in()` asks
# "is there a visible Sign in control?" and said yes — the page was showing one.
# `verify_session()` asked for POSITIVE member evidence and accepted
# `a[href*="/membership"]`, which matches DevelopmentAid's Expert-plan UPSELL
# TILE — the advert shown to people who are not members. So an advert aimed at
# logged-out visitors outvoted a Sign in button, and the machine reported a
# working session it did not have.
#
# The rules below are what survives that:
#   * proof of membership must be a control that only exists once authenticated
#     — a log-out link, or a link into the signed-in account area;
#   * promo / card / banner / cta / pricing classes are advertising and prove
#     nothing either way;
#   * pagination controls prove nothing: a guest is shown them too, they just
#     re-serve page 1;
#   * a visible Sign in control, or the site's own members-only notice, is
#     evidence AGAINST — the site stating its answer beats us inferring one.
_MEMBER_STATE_JS = """() => {
    const vis = e => {
        if (!e) return false;
        const s = window.getComputedStyle(e);
        return s.display !== 'none' && s.visibility !== 'hidden'
               && e.offsetParent !== null;
    };
    const norm = s => (s || '').toString().replace(/\\s+/g, ' ').trim().toLowerCase();
    const path = a => {
        try { return new URL(a.getAttribute('href') || '', location.origin)
                     .pathname.toLowerCase(); }
        catch (e) { return ''; }
    };
    const isPromo = e => /card|banner|promo|upsell|cta|pricing|plan/i
        .test((e.className || '').toString());

    const anchors = Array.from(document.querySelectorAll('a,button'));
    const logout = anchors.find(e => {
        const p = e.tagName === 'A' ? path(e) : '';
        const t = norm(e.textContent).slice(0, 30);
        return /(log-?out|sign-?out)/.test(p)
            || ['log out','logout','sign out','signout'].includes(t);
    });
    const account = anchors.find(e => e.tagName === 'A' && !isPromo(e)
        && /^\\/(my-account|my-profile|my-dashboard|my-|account|profile|dashboard)(\\/|$)/
            .test(path(e)));
    const signin = anchors.find(
        e => ['sign in','log in','login'].includes(norm(e.textContent)));

    const body = document.body ? document.body.innerText : '';
    const describe = e => e
        ? (e.tagName.toLowerCase() + '.' + (e.className || '').toString().slice(0, 40))
        : '';
    return {
        logout: !!logout, logoutSel: describe(logout),
        account: !!account, accountSel: describe(account),
        signin: vis(signin), signinSel: describe(signin),
        paywall: /available only for members|only for members/i.test(body),
        cards: document.querySelectorAll('da-search-card').length,
        title: (document.title || '').slice(0, 80),
        bodyLen: body.length,
    };
}"""


def membership_state(page) -> tuple[str, str]:
    """('in' | 'out' | 'unknown', evidence) — stated, not guessed.

    "unknown" is a real answer, not a failure: it means the page could not
    settle the question, which is worth saying rather than rounding to
    whichever verdict is convenient.
    """
    try:
        info = page.evaluate(_MEMBER_STATE_JS) or {}
    except Exception as exc:                                    # noqa: BLE001
        return "unknown", f"the page could not be inspected ({type(exc).__name__})"

    if info.get("logout"):
        return "in", f"a log-out control ({info.get('logoutSel') or '?'})"
    if info.get("account"):
        return "in", f"a signed-in account link ({info.get('accountSel') or '?'})"
    if info.get("signin"):
        return "out", (f"a visible Sign in control ({info.get('signinSel') or '?'}) "
                       f"and no signed-in controls")
    if info.get("paywall"):
        return "out", ("the page is showing its members-only notice, which is the "
                       "site's own answer about this session")
    if (info.get("bodyLen") or 0) < 400:
        return "unknown", (f"the page is nearly empty (title={info.get('title')!r}) "
                           f"— a challenge or a failed load, not an answer")
    return "unknown", (f"no log-out or account control, and no Sign in control "
                       f"(title={info.get('title')!r}, "
                       f"cards={info.get('cards')})")


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
        # Same headless setting as the scrape itself. Verifying in headless
        # while scraping headed (or the reverse) would test a different client
        # than the one that does the work — the check could pass and every
        # scrape still be challenged, or vice versa.
        context = open_persistent(pw, headless=settings.devaid_headless)
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
            state, evidence = membership_state(page)
            LAST_VERIFY.clear()
            LAST_VERIFY.update({"reason": {"in": "ok", "out": "guest",
                                           "unknown": "blank"}[state],
                                "state": state, "evidence": evidence})
            log.info("[devaid] verify: %s — %s",
                     {"in": "SIGNED IN", "out": "SIGNED OUT",
                      "unknown": "UNCLEAR"}[state], evidence)

            if state == "out":
                log.error(
                    "[devaid] verify: this profile is NOT signed in — %s. Exporting "
                    "or pushing it would just move an expired session. Click "
                    "'Connect account' on the dashboard and finish signing in.",
                    evidence,
                )
                return False
            if state == "unknown":
                # Not treated as success. Reporting a working account on no
                # evidence is what produced a dashboard that claimed a live
                # session while every scrape came back with one page.
                log.error(
                    "[devaid] verify: the session could not be confirmed — %s. "
                    "Treating it as NOT connected rather than reporting an "
                    "account that returns no results.", evidence,
                )
                return False
            return True
        finally:
            try:
                site_auth.close_owned(context)
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
            site_auth.close_owned(context)
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
