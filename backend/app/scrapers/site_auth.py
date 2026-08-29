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
import sys
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


# ------------------------------------------------- your own Chrome, everywhere
# Scrape as yourself: whatever you are already signed into in your everyday
# Chrome, every source gets, with no per-site "Connect account" step. Configure
# once in backend/.env:
#
#   LOP_CHROME_USER_DATA_DIR=C:\Users\<you>\AppData\Local\Google\Chrome\User Data
#   LOP_CHROME_PROFILE_DIR=Profile 7
#
# Two constraints, both enforced below:
#   1. Chrome must be CLOSED while a scrape runs — it holds an exclusive lock on
#      the whole User Data folder.
#   2. Local only. A server has no Chrome profile, so EC2 still uses the
#      per-source session files.
MIRRORS_DIR = BASE_DIR / "data" / "chrome_mirrors"

# The only files that carry a logged-in session. Everything else in a Chrome
# profile is history, cache and extensions — hundreds of megabytes that would
# make this slow and would copy far more of your browsing than is needed.
_SESSION_FILES = ("Network/Cookies", "Cookies", "Preferences", "Secure Preferences")

# Cookies are not the whole story. A single-page app typically keeps its auth
# token in localStorage or IndexedDB, not in a cookie — UN Partner Portal is a
# React app, and copying only its cookies produced a browser that was still
# signed out and got redirected to the public landing page. These directories
# are where that state lives.
_SESSION_DIRS = ("Local Storage", "IndexedDB", "Session Storage")


# Sources with their own scraper module (not in sources.json) that sit behind a
# login. Everything else declares itself with "needs_login": true.
_LOGIN_SOURCES = {"developmentaid"}


def needs_login(source: str) -> bool:
    """True when this source is behind a login and should reuse your Chrome.

    Kept deliberately narrow. Every source that answers True here makes the
    scrape depend on Chrome being closed, so the default is False and a source
    has to say it needs an account.
    """
    if source in _LOGIN_SOURCES:
        return True
    try:
        import json
        cfg = json.loads((Path(__file__).with_name("sources.json"))
                         .read_text(encoding="utf-8"))
        entries = cfg if isinstance(cfg, list) else cfg.get("sources", cfg)
        entries = entries if isinstance(entries, list) else list(entries.values())
        for e in entries:
            if e.get("name") == source:
                return bool(e.get("needs_login"))
    except Exception:
        pass
    return False


def own_chrome_dir() -> Path | None:
    """Your everyday Chrome profile root, if configured and present."""
    raw = (getattr(settings, "chrome_user_data_dir", "")
           or getattr(settings, "devaid_chrome_user_data_dir", "")).strip().strip('"')
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_dir():
        log.warning("[site_auth] LOP_CHROME_USER_DATA_DIR points at %s, which does "
                    "not exist — falling back to per-source sessions", path)
        return None
    return path


def own_chrome_profile() -> str:
    """Which Chrome profile to take the session from.

    First non-blank of the new name, the older devaid name, then "Default".
    Each is stripped before being tested, so a setting of "   " counts as unset
    rather than as a profile named with spaces.
    """
    for value in (getattr(settings, "chrome_profile_dir", ""),
                  getattr(settings, "devaid_chrome_profile_dir", "")):
        if (value or "").strip():
            return value.strip()
    return "Default"


def chrome_is_running(user_data_dir: Path) -> str:
    """Non-empty reason when Chrome currently holds this profile.

    Chrome keeps an exclusive lock on its user-data directory, and this is the
    user's real browser with every other session in it — so this refuses on any
    positive sign rather than trying and risking the profile.
    """
    lock = user_data_dir / "lockfile"                    # Windows
    if lock.exists():
        try:
            with open(lock, "a"):
                pass
        except OSError:
            return "its lockfile is held"
    if (user_data_dir / "SingletonLock").exists():       # macOS / Linux
        return "SingletonLock is present"
    try:
        import subprocess
        if sys.platform.startswith("win"):
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                                 capture_output=True, text=True, timeout=10).stdout
            if "chrome.exe" in out.lower():
                return "chrome.exe is running"
        elif subprocess.run(["pgrep", "-x", "chrome"],
                            capture_output=True, timeout=10).returncode == 0:
            return "a chrome process is running"
    except Exception:
        pass
    return ""


def mirror_own_chrome(source: str) -> Path | None:
    """Copy your Chrome session into a per-source working directory.

    Chrome 136+ refuses remote debugging against the real user-data directory
    ("DevTools remote debugging requires a non-default data directory"), so the
    live profile cannot be driven at all. A copy can: Chrome objects to
    debugging the live profile, not to the cookies, and the encryption key in
    "Local State" is protected by DPAPI bound to your Windows account, so a copy
    you make on your own machine decrypts and arrives signed in — to every site
    you are signed into, which is the point.

    Per source rather than one shared directory, because sources scrape
    concurrently and Chrome would lock a shared one. The files are a few hundred
    KB, so the copies are cheap.

    Returns the directory to launch against, or None if unavailable.
    """
    import shutil

    root = own_chrome_dir()
    if root is None:
        return None
    profile = own_chrome_profile()
    if not (root / profile).is_dir():
        log.warning("[site_auth] no Chrome profile %r under %s — falling back to "
                    "per-source sessions", profile, root)
        return None
    blocker = chrome_is_running(root)
    if blocker:
        log.error("[site_auth] Chrome is open (%s), so the session cannot be copied "
                  "out of it. Close Chrome completely and re-run.", blocker)
        return None

    dest = MIRRORS_DIR / source
    (dest / profile).mkdir(parents=True, exist_ok=True)
    copied = []
    if (root / "Local State").is_file():
        shutil.copy2(root / "Local State", dest / "Local State")
        copied.append("Local State")
    for rel in _SESSION_FILES:
        src = root / profile / rel
        if src.is_file():
            dst = dest / profile / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)

    # localStorage / IndexedDB, where SPAs keep their auth tokens. Capped so a
    # profile with a large IndexedDB (some sites cache megabytes there) cannot
    # turn every scrape into a long file copy.
    for rel in _SESSION_DIRS:
        src = root / profile / rel
        if not src.is_dir():
            continue
        try:
            size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        except OSError:
            continue
        if size > 80 * 1024 * 1024:
            log.info("[site_auth] skipping %s (%.0f MB) — too large to copy each run",
                     rel, size / 1024 / 1024)
            continue
        dst = dest / profile / rel
        shutil.rmtree(dst, ignore_errors=True)     # stale keys must not linger
        try:
            shutil.copytree(src, dst)
            copied.append(f"{rel}/")
        except OSError as exc:
            log.debug("[site_auth] could not copy %s (%s)", rel, exc)

    if not any(c.endswith("Cookies") for c in copied):
        log.warning("[site_auth] no cookie database in %s — the mirror will not be "
                    "signed in anywhere", root / profile)
        return None
    log.info("[site_auth] %s: using your Chrome session from %s (%s)",
             source, profile, ", ".join(copied))
    return dest


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


def mask_headless(context):
    """Present the same browser identity headless as headed.

    Headless Chrome writes "HeadlessChrome/151.0.0.0" into its own user agent.
    That single word is the difference between a session that works and a 403,
    because Cloudflare issues cf_clearance against the identity it saw and
    re-challenges anything that presents a different one.

    The fix is deliberately minimal: take the browser's REAL user agent and
    replace "HeadlessChrome" with "Chrome". Everything else — version, platform,
    and the Sec-CH-UA client hints — is read back out of the browser itself and
    passed through unchanged, so no two headers can contradict each other. That
    is the failure mode this replaces; inventing a version string is what caused
    it in the first place.

    Applied through CDP rather than Playwright's user_agent option because only
    CDP can set the client hints (userAgentMetadata) alongside the UA string.
    Best-effort: any failure leaves the browser exactly as it was.
    """
    script = """async () => {
        const ua = navigator.userAgent;
        const d = navigator.userAgentData;
        let meta = null;
        if (d) {
            const hi = await d.getHighEntropyValues([
                'architecture', 'bitness', 'model', 'platformVersion',
                'uaFullVersion', 'fullVersionList',
            ]).catch(() => ({}));
            meta = {
                brands: d.brands.map(b => ({brand: b.brand, version: b.version})),
                fullVersionList: (hi.fullVersionList || d.brands).map(
                    b => ({brand: b.brand, version: b.version})),
                platform: d.platform,
                platformVersion: hi.platformVersion || '',
                architecture: hi.architecture || 'x86',
                bitness: hi.bitness || '64',
                model: hi.model || '',
                mobile: d.mobile,
                fullVersion: hi.uaFullVersion || '',
            };
        }
        return {ua, meta};
    }"""

    def _read_identity():
        """The browser's own identity, read once from a throwaway page."""
        borrowed = not context.pages
        page = context.new_page() if borrowed else context.pages[0]
        try:
            return page.evaluate(script)
        except Exception:
            return None
        finally:
            if borrowed:
                try:
                    page.close()
                except Exception:
                    pass

    try:
        info = _read_identity()
    except Exception:
        info = None
    ua = (info or {}).get("ua") or ""
    if "Headless" not in ua:
        return context                  # headed run — leave the identity alone

    # userAgent ONLY. Setting acceptLanguage here too produced the malformed
    # "en-US,en;q=0.9;q=0.9" — CDP appends its own q-value on top of the one the
    # context's locale already generated, and a malformed header is a worse
    # fingerprint than no override at all. Accept-Language comes from
    # locale="en-US" on the context, which formats it correctly.
    override = {"userAgent": ua.replace("HeadlessChrome", "Chrome")}
    meta = (info or {}).get("meta")
    if meta:
        override["platform"] = meta.get("platform") or ""
        override["userAgentMetadata"] = meta

    # The override is computed ONCE, above, and each page handler only sends it.
    # Reading navigator.userAgent inside the "page" event handler instead raced
    # with page creation and silently left every page after the first
    # unmasked — the sync API cannot re-enter the driver from an event callback.
    def _apply(page) -> None:
        try:
            context.new_cdp_session(page).send(
                "Emulation.setUserAgentOverride", override)
        except Exception:
            log.debug("[site_auth] could not apply the user-agent override", exc_info=True)

    try:
        # navigator.webdriver is true whenever Playwright drives the browser and
        # is checked by every commercial bot filter. Removing the flag is not
        # evasion of the login (that stays human, see the module docstring) —
        # it stops an ordinary signed-in session being refused at the door.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        for existing in context.pages:
            _apply(existing)
        context.on("page", _apply)
        log.info("[site_auth] headless browser presenting as %s", override["userAgent"])
    except Exception:
        log.debug("[site_auth] headless masking unavailable", exc_info=True)
    return context


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
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        # Playwright adds --enable-automation by default, which sets
        # navigator.webdriver and marks the session as automated.
        "ignore_default_args": ["--enable-automation"],
    }
    # NO user_agent here. It used to be settings.user_agent, which hard-codes
    # "Chrome/126.0.0.0" — but a browser also announces its version in the
    # Sec-CH-UA client hints, which come from the real build and cannot be
    # overridden by Playwright's user_agent option. So every request said
    # "I am Chrome 126" in one header and something else in the next.
    #
    # That contradiction is a stock bot signature, and this function is used by
    # EVERY JS-rendered source (base_scraper and adb both call it), so it went
    # to all of them. ADB's Cloudflare rule fires on exactly this sort of
    # signal: its block page is "Sorry, you have been blocked" — the
    # firewall-rule response, not the passable "Just a moment..." challenge.
    #
    # mask_headless() instead keeps the browser's own identity and removes only
    # the word "Headless" from it, leaving the client hints untouched so no two
    # headers can disagree.
    context_args = {
        "viewport": {"width": 1400, "height": 900},
        "locale": "en-US",
    }

    def _launch(**extra):
        """Real Chrome when installed, bundled Chromium otherwise.

        A stock Chromium build is a weaker reputation signal than an installed
        Chrome, and preferring it costs nothing.
        """
        try:
            return pw.chromium.launch(channel="chrome", **launch_args, **extra)
        except Exception:
            return pw.chromium.launch(**launch_args, **extra)

    # Your own Chrome, but ONLY for sources that actually need a login.
    #
    # Applying it to every source was wrong: copying the session requires Chrome
    # to be closed, and imposing that on the ~80 public sources that never needed
    # a login makes every scrape depend on the browser being shut. A source opts
    # in with "needs_login": true in sources.json, or by being named below.
    mirror = mirror_own_chrome(source) if needs_login(source) else None
    if mirror is not None:
        profile = own_chrome_profile()
        try:
            return mask_headless(pw.chromium.launch_persistent_context(
                str(mirror), channel="chrome",
                args=launch_args["args"] + [f"--profile-directory={profile}"],
                ignore_default_args=launch_args["ignore_default_args"],
                headless=launch_args["headless"], **context_args))
        except Exception as exc:                                # noqa: BLE001
            log.warning("[site_auth] %s: could not open the Chrome mirror (%s) — "
                        "falling back to the per-source session", source, exc)

    sfile = session_file(source)
    if sfile.exists():
        browser = _launch()
        return mask_headless(
            browser.new_context(storage_state=str(sfile), **context_args))

    pdir = profile_dir(source)
    if pdir.exists() and any(pdir.iterdir()):
        # A persistent context is launched, not created, so it takes both sets.
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(pdir), channel="chrome", **launch_args, **context_args)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(
                str(pdir), **launch_args, **context_args)
        return mask_headless(ctx)

    browser = _launch()
    return mask_headless(browser.new_context(**context_args))


def close_owned(context) -> None:
    """Close a context returned by open_context AND the browser that owns it.

    Why this exists
    ---------------
    open_context has four return paths. Two of them launch a Browser and return
    only one of its contexts:

        browser = _launch()
        return mask_headless(browser.new_context(...))       # browser is a local

    `browser` goes out of scope the moment this function returns, so the caller
    receives a BrowserContext with no reference to the process behind it.
    Callers then do `context.close()`, which closes the context and leaves the
    Chromium process running. That is the orphan-Chrome leak: it is structural,
    not intermittent, and it affects the storage-state and anonymous paths —
    which between them cover most sources.

    It stayed invisible for two reasons. First, `base_scraper` named the
    variable `browser` while holding a BrowserContext, so `browser.close()`
    read as correct in review. Second, `with sync_playwright()` kills the driver
    (and its browsers) on exit, so a run that completes normally cleans up
    anyway — the leak only shows when the thread never reaches that exit, which
    is exactly what happens on stop, timeout or an abandoned worker.

    The other two paths use launch_persistent_context, which has no separate
    Browser: closing the context closes its process. `BrowserContext.browser`
    returns None for those, so one branch covers both ownership models without
    the caller needing to know which it got.

    Every step is guarded independently: a half-dead browser must not stop the
    rest of the teardown, and teardown runs in `finally` blocks where an
    exception would mask the real error.
    """
    if context is None:
        return

    # Pages first. Closing a context closes its pages, but an unresponsive page
    # can make context.close() hang, and closing pages individually gives that
    # a chance to resolve before the bigger hammer.
    try:
        for page in list(getattr(context, "pages", []) or []):
            try:
                page.close()
            except Exception:                                   # noqa: BLE001
                pass
    except Exception:                                           # noqa: BLE001
        pass

    owner = None
    try:
        owner = context.browser          # None for a persistent context
    except Exception:                                           # noqa: BLE001
        owner = None

    try:
        context.close()
    except Exception:                                           # noqa: BLE001
        pass

    if owner is not None:
        try:
            owner.close()
        except Exception:                                       # noqa: BLE001
            pass


def chrome_process_count() -> int:
    """How many Chrome/Chromium processes exist right now, or -1 if unknowable.

    The acceptance criterion for the lifecycle work is "the count returns to its
    pre-run baseline", and a number nobody can read is not a criterion. Used by
    the tests and by the runbook's before/after check; deliberately dependency
    free (no psutil) so it works on the EC2 box as shipped.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            return sum(1 for line in out.splitlines() if "chrome.exe" in line.lower())
        out = subprocess.run(
            ["ps", "-e", "-o", "comm="], capture_output=True, text=True, timeout=15,
        ).stdout
        return sum(
            1 for line in out.splitlines()
            if line.strip().lower() in ("chrome", "chromium", "chromium-browser",
                                        "headless_shell")
        )
    except Exception:                                           # noqa: BLE001
        return -1


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
