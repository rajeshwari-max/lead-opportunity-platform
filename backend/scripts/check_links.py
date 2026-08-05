"""Check that stored opportunity links actually open something.

Run from the backend directory, with the venv active:

    python scripts/check_links.py                # sample 200 links
    python scripts/check_links.py --all          # every link (slow)
    python scripts/check_links.py --source "Bond UK"
    python scripts/check_links.py --fix          # clear links that 404

Two kinds of problem are reported separately, because they need different
responses:

  BROKEN   the URL does not resolve — 404, DNS failure, connection refused.
           These are worth clearing; the link cannot ever work.

  LISTING  the URL resolves, but to an index or search page rather than the
           opportunity itself. Some sources never publish a per-call URL, so
           this is often the best link that exists and should be kept, just
           labelled honestly in the UI.

A redirect to the site root is reported as BROKEN even though it returns 200 —
that is exactly the "it opens the homepage" complaint this script exists to
catch, and a status code alone will not reveal it.
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.database.db import session_scope  # noqa: E402
from app.database.models import Opportunity, Status  # noqa: E402
from app.services.links import link_kind  # noqa: E402

TIMEOUT = 15.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


def is_root(url: str) -> bool:
    p = urlparse(url)
    return not [seg for seg in (p.path or "").split("/") if seg] and not p.query


def check(client: httpx.Client, url: str) -> tuple[str, str]:
    """Return (verdict, detail)."""
    try:
        # HEAD first — cheap. Many sites reject it, so fall back to a ranged GET
        # rather than downloading whole pages.
        r = client.head(url, follow_redirects=True)
        if r.status_code >= 400 or r.status_code == 405:
            r = client.get(url, follow_redirects=True, headers={**HEADERS, "Range": "bytes=0-2048"})
    except Exception as exc:
        return "BROKEN", type(exc).__name__

    if r.status_code >= 400:
        return "BROKEN", f"HTTP {r.status_code}"

    final = str(r.url)
    # Redirected to the site root: resolves, but not to the opportunity.
    if is_root(final) and not is_root(url):
        return "BROKEN", f"redirected to homepage ({final})"
    if link_kind(final) == "listing":
        return "LISTING", final
    return "OK", f"HTTP {r.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="check every link")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--source", default="", help="restrict to one source")
    ap.add_argument("--fix", action="store_true", help="clear links that come back BROKEN")
    args = ap.parse_args()

    with session_scope() as db:
        q = db.query(Opportunity).filter(
            Opportunity.opportunity_url != "", Opportunity.status == Status.ACTIVE
        )
        if args.source:
            q = q.filter(Opportunity.source_website == args.source)
        rows = [(o.id, o.source_website, o.opportunity_url) for o in q.all()]

    if not args.all and len(rows) > args.sample:
        random.seed(0)                     # same sample each run, so runs compare
        rows = random.sample(rows, args.sample)

    print(f"Checking {len(rows)} links…\n")
    verdicts: Counter[str] = Counter()
    by_source: dict[str, Counter] = {}
    broken_ids: list[int] = []

    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, verify=False) as client:
        for i, (oid, source, url) in enumerate(rows, 1):
            verdict, detail = check(client, url)
            verdicts[verdict] += 1
            by_source.setdefault(source, Counter())[verdict] += 1
            if verdict == "BROKEN":
                broken_ids.append(oid)
                print(f"  BROKEN  [{source}] {url[:90]}\n          {detail}")
            elif verdict == "LISTING":
                print(f"  LISTING [{source}] {url[:90]}")
            if i % 25 == 0:
                print(f"  … {i}/{len(rows)}", file=sys.stderr)

    total = sum(verdicts.values()) or 1
    print("\n" + "=" * 60)
    for v, n in verdicts.most_common():
        print(f"  {n:6}  {100 * n / total:5.1f}%  {v}")

    print("\nworst sources:")
    ranked = sorted(by_source.items(), key=lambda kv: -(kv[1]["BROKEN"] + kv[1]["LISTING"]))
    for source, c in ranked[:12]:
        if c["BROKEN"] or c["LISTING"]:
            print(f"  {source[:38]:40} broken={c['BROKEN']:4} listing={c['LISTING']:4} ok={c['OK']:4}")

    if args.fix and broken_ids:
        with session_scope() as db:
            for oid in broken_ids:
                opp = db.get(Opportunity, oid)
                if opp:
                    opp.opportunity_url = ""
        print(f"\nCleared {len(broken_ids)} broken links.")
    elif broken_ids:
        print(f"\n{len(broken_ids)} broken links found. Re-run with --fix to clear them.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
