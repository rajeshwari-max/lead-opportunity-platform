"""Check the 'use my own Chrome profile' setup before running a scrape.

    python scripts/check_chrome_profile.py

Reports, in order: whether the two .env lines are set, whether the path exists,
whether the named profile is really in there, and whether Chrome currently holds
the lock. Every failure says what to change.

This is a script rather than a python -c one-liner because the one-liner is long
enough that PowerShell breaks it across lines mid-string and hangs waiting for
the closing quote.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.scrapers.devaid_auth import (  # noqa: E402
    MIRROR_DIR,
    PROFILE_DIR,
    _chrome_is_running,
    _own_chrome_dir,
    has_profile,
)


def main() -> int:
    raw = (settings.devaid_chrome_user_data_dir or "").strip()
    want = (settings.devaid_chrome_profile_dir or "Default").strip()

    print("\nDevelopmentAid browser profile check")
    print("-" * 52)
    print(f"LOP_DEVAID_CHROME_USER_DATA_DIR : {raw or '(not set)'}")
    print(f"LOP_DEVAID_CHROME_PROFILE_DIR   : {want}")

    if not raw:
        print("\nUsing the DEDICATED profile, not your own Chrome.")
        print(f"  profile dir : {PROFILE_DIR}")
        print(f"  connected   : {has_profile()}")
        if not has_profile():
            print("\n  -> Not connected. Either click 'Connect account' on the")
            print("     dashboard, or set the two .env lines to use your own Chrome.")
            return 1
        print("\n  -> Ready. Chrome does NOT need to be closed for this mode.")
        return 0

    root = _own_chrome_dir()
    if root is None:
        print("\nFAIL: that path does not exist.")
        print("  Open chrome://version and copy the 'Profile Path' line, minus")
        print("  its last folder. Paste it UNQUOTED into backend/.env — inside")
        print("  double quotes, dotenv treats the backslashes as escapes.")
        return 1
    print(f"\nprofile root exists             : {root}")

    target = root / want
    if not target.is_dir():
        available = sorted(p.name for p in root.iterdir()
                           if p.is_dir() and (p.name == "Default"
                                              or p.name.startswith("Profile ")))
        print(f"FAIL: no profile named {want!r} in there.")
        print(f"  available: {', '.join(available) or '(none found)'}")
        print("  Set LOP_DEVAID_CHROME_PROFILE_DIR to whichever one you are")
        print("  signed into DevelopmentAid on (chrome://version tells you).")
        return 1
    print(f"profile {want!r} found{' ' * max(0, 10 - len(want))}: {target}")

    blocker = _chrome_is_running(root)
    if blocker:
        print(f"\nBLOCKED: Chrome is holding the profile ({blocker}).")
        print("  Close Chrome completely — every window, plus anything left in")
        print("  the system tray. If it persists, end chrome.exe in Task Manager.")
        print("  Chrome locks the whole 'User Data' folder, not just one profile,")
        print("  so any open Chrome window blocks this regardless of which")
        print("  profile it is using.")
        return 2

    cookies = [p for p in (target / "Network" / "Cookies", target / "Cookies")
               if p.is_file()]
    if not cookies:
        print(f"FAIL: no cookie database inside {want!r}.")
        print("  That profile has never stored a session, so it cannot be the one")
        print("  you are signed into DevelopmentAid on. Check chrome://version in")
        print("  the window where you ARE logged in and use that Profile Path.")
        return 1
    print(f"cookie database                 : {cookies[0].name} "
          f"({cookies[0].stat().st_size // 1024} KB)")

    if not (root / "Local State").is_file():
        print("FAIL: no 'Local State' at the profile root — the cookie encryption")
        print("  key lives there, so the copied session could not be decrypted.")
        return 1

    print("\nREADY — nothing is holding the profile. A scrape can run now.")
    print("  Keep Chrome closed while it runs.")
    print("\n  Note: Chrome 136+ refuses remote debugging on a live profile, so the")
    print("  scraper does NOT open yours directly. It copies just the session")
    print("  files (Local State + Cookies + Preferences) into")
    print(f"  {MIRROR_DIR}")
    print("  and drives that. Your real profile is only ever read, never opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
