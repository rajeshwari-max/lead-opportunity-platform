"""Are all open UNDP notices on the dashboard? Read-only.

    python scripts/undp_coverage.py               # RFPs only (the default)
    python scripts/undp_coverage.py --all         # every process type
    python scripts/undp_coverage.py --missing 30  # list up to 30 missing ones

Reads the live board (procurement-notices.undp.org) with the platform's own
UNDP reader, then looks each notice up in the database by its nego_id. Every
live notice ends in exactly one bucket:

  on the dashboard      stored, Active, closing date not passed
  stored, hidden        stored, but not shown (with the reason)
  not English           not stored on purpose: English-only intake, and UNDP
                        publishes no English edition of these notices
  missing               not stored and no known reason: a capture gap

Changes nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx                                                    # noqa: E402
from sqlalchemy import select                                   # noqa: E402

import app.scrapers  # noqa: E402,F401  registers every scraper
from app.core.config import settings                            # noqa: E402
from app.database.db import session_scope                       # noqa: E402
from app.database.models import Opportunity                     # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY              # noqa: E402
from app.services.actionable import application_today, is_actionable  # noqa: E402
from app.services.deadline_parser import DeadlineParser         # noqa: E402
from app.services.english_only import detect                    # noqa: E402

BOARD = "https://procurement-notices.undp.org/?lang=en"
_ID = re.compile(r"nego_id=(\d+)")


def _process(summary: str) -> str:
    m = re.search(r"Process\s+(\w+)", summary or "")
    return m.group(1).upper() if m else "?"


async def _fetch() -> str:
    async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent,
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=60, follow_redirects=True) as client:
        resp = await client.get(BOARD)
        resp.raise_for_status()
        return resp.text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="every process type, not only RFPs")
    ap.add_argument("--missing", type=int, default=15)
    args = ap.parse_args()

    html = asyncio.run(_fetch())
    rows = SCRAPER_REGISTRY["undp_procurement"]().parse_listing(html, BOARD)
    today = application_today()
    parser = DeadlineParser()
    live = []
    for r in rows:
        m = _ID.search(r.opportunity_url or "")
        if not m:
            continue
        kind = _process(r.summary)
        if not args.all and kind != "RFP":
            continue
        closes = parser.parse(r.deadline_raw)
        if closes is not None and closes < today:
            continue
        live.append((m.group(1), kind, r))
    print(f"Live on the UNDP board now: {len(live)} "
          f"{'notices' if args.all else 'RFPs'} still open")

    with session_scope() as db:
        stored = {}
        for o in db.execute(
                select(Opportunity).where(Opportunity.opportunity_url.like("%nego_id=%"))
        ).scalars():
            m = _ID.search(o.opportunity_url or "")
            if m:
                stored[m.group(1)] = (o.status, o.deadline, o.deadline_state, o.title)

    buckets: Counter = Counter()
    hidden_why: Counter = Counter()
    missing = []
    for nego, kind, r in live:
        if nego in stored:
            status, deadline, state, _title = stored[nego]
            if is_actionable(status, deadline, state, today) and deadline is not None:
                buckets["on the dashboard"] += 1
            else:
                buckets["stored, hidden"] += 1
                why = ("archived (Expired)" if str(getattr(status, "value", status)) != "Active"
                       else "no closing date stored" if deadline is None
                       else f"stored date {deadline} has passed")
                hidden_why[why] += 1
        elif not detect(r.title, r.summary)[0]:
            buckets["not English"] += 1
        else:
            buckets["missing"] += 1
            missing.append((nego, kind, r.deadline_raw, r.title[:90]))

    for name in ("on the dashboard", "stored, hidden", "not English", "missing"):
        print(f"  {name:18} {buckets[name]}")
    for why, n in hidden_why.most_common():
        print(f"      hidden because {why}: {n}")
    if missing:
        print(f"\nMissing (first {min(len(missing), args.missing)}):")
        for nego, kind, deadline, title in missing[:args.missing]:
            print(f"  nego_id={nego} | {kind} | closes {deadline} | {title}")
        print("\nA missing notice usually means the nightly run did not reach or "
              "finish UNDP. Run it now with:  .venv/bin/python scripts/rescrape_source.py undp_procurement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
