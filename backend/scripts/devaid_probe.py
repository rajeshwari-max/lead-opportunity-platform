"""Can this machine reach DevelopmentAid without a browser?

    python scripts/devaid_probe.py

Run it on EC2. It answers the only question that decides whether the
browser-free path is worth building out:

  * blocked at the edge      -> keep the headless browser, nothing else helps
  * reached but logged out   -> the session needs refreshing or is incomplete
  * reached and signed in    -> plain HTTP works; the fast path is viable

Read-only. Fetches exactly one page and changes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scrapers import devaid_http          # noqa: E402


def main() -> int:
    r = devaid_http.probe()

    print("\nDevelopmentAid — browser-free access probe")
    print("=" * 58)
    print(f"  session file present : {r['session_present']}")
    print(f"  profile dir present  : {r['profile_present']}")
    print(f"  usable cookies       : {r['cookies']}")
    print(f"  reachable over HTTP  : {r['reachable']}")
    print(f"  HTTP status          : {r['status_code']}")
    print(f"  signed in            : {r['signed_in']}")
    if r.get("blocked_by"):
        print(f"  blocked by           : {r['blocked_by']}")
    if r.get("markers"):
        print(f"  markers seen         : {json.dumps(r['markers'])}")
    print("-" * 58)
    print(f"  {r['verdict']}")
    print()

    if r["reachable"] and r["signed_in"]:
        print("  Next: capture the search API on a machine with a screen —")
        print("      python scripts/devaid_capture_api.py")
        print("  then this scraper can page JSON directly instead of rendering.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
