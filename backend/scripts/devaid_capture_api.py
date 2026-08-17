"""Record the JSON search endpoint DevelopmentAid's own front-end calls.

    python scripts/devaid_capture_api.py            # headless, uses saved session
    python scripts/devaid_capture_api.py --show     # visible window, to watch it

Run this on a machine that already has a working session (your PC — the one
where "Connect account" was used).

Why
---
The scraper currently reads rendered HTML, which means running Chromium and
paying a full render per page. The site's own front-end fetches results as JSON
from an internal endpoint. If we know that endpoint, its request body and which
field carries the page number, the scraper can request the same JSON directly —
far fewer requests, no rendering, and no dependence on the DOM staying the same.

This script only observes. It opens a normal search page, records the network
calls the page makes, and writes what it saw to data/devaid_api.json. It does
not log in, submit anything, or alter your account.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings                      # noqa: E402
from app.scrapers.devaid_auth import open_persistent      # noqa: E402
from app.scrapers.devaid_http import API_SPEC_FILE        # noqa: E402

SEARCH_URL = (
    "https://www.developmentaid.org/tenders/search"
    "?hiddenAdvancedFilters=0&sort=deadline.desc"
)

# An endpoint is interesting when it returns JSON containing what look like
# result records. Everything else a page fetches — fonts, analytics, consent —
# is noise.
_MIN_ROWS = 3


def _looks_like_results(payload) -> tuple[bool, int, list[str]]:
    """(is_results, row_count, field_names) for a decoded JSON body."""
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("items", "data", "results", "records", "hits", "rows", "content"):
            v = payload.get(key)
            if isinstance(v, list):
                rows = v
                break
            if isinstance(v, dict):                 # {"data": {"items": [...]}}
                for k2 in ("items", "results", "records", "rows"):
                    if isinstance(v.get(k2), list):
                        rows = v[k2]
                        break
            if rows:
                break
    if not isinstance(rows, list) or len(rows) < _MIN_ROWS:
        return False, 0, []
    fields = sorted({k for r in rows[:5] if isinstance(r, dict) for k in r})
    # Result records carry an id and something title-like; taxonomy lists
    # (sectors, countries) have an id but no title, which is how they differ.
    titleish = any(f.lower() in {"title", "name", "subject", "heading"} for f in fields)
    return (bool(fields) and titleish), len(rows), fields


def main() -> int:
    from playwright.sync_api import sync_playwright

    headless = "--show" not in sys.argv
    seen: list[dict] = []

    with sync_playwright() as pw:
        browser = open_persistent(pw, headless=headless)
        page = browser.pages[0] if browser.pages else browser.new_page()

        def on_response(resp):
            ctype = (resp.headers or {}).get("content-type", "").lower()
            if "json" not in ctype:
                return
            try:
                payload = resp.json()
            except Exception:
                return
            ok, n, fields = _looks_like_results(payload)
            req = resp.request
            entry = {
                "url": resp.url,
                "method": req.method,
                "status": resp.status,
                "rows": n,
                "looks_like_results": ok,
                "fields": fields[:40],
            }
            try:
                if req.method == "POST":
                    entry["request_body"] = req.post_data
            except Exception:
                pass
            seen.append(entry)

        page.on("response", on_response)

        print(f"Opening {SEARCH_URL}")
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(3000)

        # Move to page 2 as well: the paging parameter is only visible in a
        # request that actually asks for a different page.
        try:
            page.goto(SEARCH_URL + "&pageNr=2", wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(3000)
        except Exception:
            print("  (could not load page 2 — paging parameter may be missing)")

        browser.close()

    results = [e for e in seen if e["looks_like_results"]]
    print(f"\nJSON responses seen : {len(seen)}")
    print(f"Look like results   : {len(results)}")

    for e in results:
        print("\n" + "-" * 60)
        print(f"  {e['method']} {e['url'][:130]}")
        print(f"  status={e['status']}  rows={e['rows']}")
        print(f"  fields: {', '.join(e['fields'][:18])}")
        if e.get("request_body"):
            print(f"  body  : {str(e['request_body'])[:400]}")

    API_SPEC_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_SPEC_FILE.write_text(
        json.dumps({"candidates": results, "all_json": seen[:80]}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWritten to {API_SPEC_FILE}")

    if not results:
        print(
            "\nNo result-shaped JSON was seen. That most likely means the page is\n"
            "server-rendered rather than fetching results over XHR, in which case\n"
            "there is no API to page and the current HTML approach is already the\n"
            "right one. Re-run with --show to watch what actually loads."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
