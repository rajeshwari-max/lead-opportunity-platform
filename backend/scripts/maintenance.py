"""Run the dashboard repair passes by hand.

The same three passes now run automatically at the end of every scrape (see
ScraperManager._maintenance). This script exists so the existing database can be
cleaned immediately, without waiting for the next full run:

    python scripts/maintenance.py            # show what would change
    python scripts/maintenance.py --apply    # actually change it

What it does, and why each pass exists:

  * audit_deadlines()  — recomputes Active/Expired from the deadline, clears
    sentinel dates (9999-12-31 and friends), and retires undated "Ongoing" rows
    that no working scrape has seen for LOP_ONGOING_MAX_AGE_DAYS. That last one
    is the fix for closed calls staying on the dashboard: a row with no deadline
    had nothing that could ever close it.

  * repair_links()     — clears links that cannot point at one opportunity and
    rewrites known machine endpoints to their human page.

  * purge_junk_rows()  — deletes rows that are page furniture, not calls
    ("Skip to main content", "Navigation breadcrumbs", bare email addresses).

--apply is required because two of these three delete or hide rows. A dry run
prints the same counts without writing, so the effect can be checked first.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.database.db import init_db, session_scope  # noqa: E402


def _preview() -> dict:
    """Counts only — no writes. Mirrors what --apply would do."""
    from datetime import date, datetime, timedelta, timezone

    from sqlalchemy import select

    from app.database.models import Opportunity, ScrapeRun, Status
    from app.services.deadline_audit import is_sentinel
    from app.services.links import is_furniture, is_usable_link

    today = date.today()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.ongoing_max_age_days)
    out = {"sentinels": 0, "status_changes": 0, "stale_ongoing": 0,
           "held_back": 0, "links_cleared": 0, "junk": 0}

    with session_scope() as db:
        healthy = {
            src for (src,) in db.execute(
                select(ScrapeRun.source_website)
                .where(ScrapeRun.started_at >= cutoff, ScrapeRun.found > 0)
                .group_by(ScrapeRun.source_website)
            )
        }
        for opp in db.execute(select(Opportunity)).scalars():
            if is_furniture(opp.title or "", opp.opportunity_url or ""):
                out["junk"] += 1
                continue
            if is_sentinel(opp.deadline):
                out["sentinels"] += 1
            elif opp.deadline is not None:
                should_be = Status.EXPIRED if opp.deadline < today else Status.ACTIVE
                if opp.status != should_be:
                    out["status_changes"] += 1
            elif opp.status is Status.ACTIVE:
                seen = getattr(opp, "last_seen", None) or opp.date_scraped
                if seen is not None:
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                    if seen < cutoff:
                        if opp.source_website in healthy:
                            out["stale_ongoing"] += 1
                        else:
                            out["held_back"] += 1
            url = (opp.opportunity_url or "").strip()
            if url and not is_usable_link(url, opp.website):
                out["links_cleared"] += 1
        db.rollback()          # preview must not write, even accidentally
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (without this, only report them)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()          # also runs the last_seen migration on an older database

    if not args.apply:
        p = _preview()
        print("\nDry run — nothing was changed.\n")
        print(f"  sentinel dates to clear          : {p['sentinels']:>7}")
        print(f"  Active/Expired to recompute      : {p['status_changes']:>7}")
        print(f"  undated 'Ongoing' rows to retire : {p['stale_ongoing']:>7}"
              f"   (unseen for {settings.ongoing_max_age_days}+ days)")
        print(f"  ...held back, source is broken   : {p['held_back']:>7}"
              "   (fix the source; these stay live)")
        print(f"  unusable links to clear          : {p['links_cleared']:>7}")
        print(f"  page-furniture rows to DELETE    : {p['junk']:>7}")
        print("\nRe-run with --apply to make these changes.\n")
        return 0

    from app.services.deadline_audit import audit_deadlines
    from app.services.links import purge_junk_rows, repair_links

    print("\ndeadlines :", audit_deadlines())
    print("links     :", repair_links())
    print("junk rows :", purge_junk_rows(), "deleted")
    print("\nDone. Hard-refresh the dashboard (Ctrl+Shift+R).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
