"""Re-scrape ONE source and WRITE the results, to repair rows already stored.

    python scripts/rescrape_source.py undp_procurement
    python scripts/rescrape_source.py devnet --dry-run

Why this exists
---------------
There was no way to do this. `check_scraper.py` prints every row a source
produces and writes nothing — its banner says so — and the only thing that
writes is the dashboard's scrape button, which runs all 83 sources.

That gap cost real time. UNDP's deadline extraction was fixed and verified
against the live site (0.4% of rows carrying a date, to 100%), the fix was
deployed, `check_scraper` was run, and the database still read

    (2231, 2)      2231 rows, 2 with a deadline
    dashboard-visible: 0

because nothing had written anything. The fix was correct and invisible.

Repairing one source is now the normal shape of this work: a parser is
corrected, and the rows already stored need the corrected value. Running all 83
sources to repair one is an hour of scraping and a lot of requests to sites that
did not need visiting.

What it does NOT do
-------------------
Nothing this script does is new behaviour. It calls the same
`ScraperManager.start()` the dashboard button calls, with one source named, so
every gate, contract, dedupe rule and deadline rule is the same. In particular
the repair itself is the ingest path's own: a row already stored WITHOUT a
deadline gains one, and a row that already has one is never overwritten.

The run lease is taken the same way too, so this cannot run alongside a
scheduled scrape and produce two writers.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Same as every other script here: run from anywhere, import the app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _counts(source_display: str) -> tuple[int, int, int]:
    """(rows, rows with a deadline, rows the dashboard would show)."""
    from sqlalchemy import func, select

    from app.database.db import session_scope
    from app.database.models import Opportunity
    from app.services.actionable import strict_actionable_clause

    with session_scope() as db:
        total = db.execute(
            select(func.count()).select_from(Opportunity)
            .where(Opportunity.source_website == source_display)
        ).scalar_one()
        dated = db.execute(
            select(func.count()).select_from(Opportunity)
            .where(Opportunity.source_website == source_display,
                   Opportunity.deadline.is_not(None))
        ).scalar_one()
        live = db.execute(
            select(func.count()).select_from(Opportunity)
            .where(Opportunity.source_website == source_display,
                   strict_actionable_clause())
        ).scalar_one()
    return total, dated, live


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="registry name, e.g. undp_procurement")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the before counts and exit without scraping")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")

    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                        # noqa: F401

    if args.source not in SCRAPER_REGISTRY:
        hits = [n for n in SCRAPER_REGISTRY if args.source.lower() in n.lower()]
        print(f"no source named {args.source!r}."
              + (f" Did you mean: {', '.join(hits[:5])}?" if hits else ""),
              file=sys.stderr)
        return 2

    display = SCRAPER_REGISTRY[args.source].display_name
    before = _counts(display)
    print(f"\n{display}  ({args.source})")
    print(f"  before : {before[0]} row(s), {before[1]} dated, "
          f"{before[2]} shown on the dashboard")
    if args.dry_run:
        print("  --dry-run: nothing scraped, nothing written.")
        return 0

    print("  scraping and WRITING — this uses the same path as the dashboard "
          "button, for this source only...\n")

    from app.services.scraper_manager import ScraperManager

    manager = ScraperManager()

    async def run() -> None:
        await manager.start([args.source])
        task = getattr(manager, "_task", None)
        if task is not None:
            await task

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        # The cross-process lease. Almost always a scheduled scrape already
        # running — worth saying plainly rather than as a traceback, because
        # the answer is to wait, not to debug.
        print(f"\ncould not start: {exc}", file=sys.stderr)
        return 1

    after = _counts(display)
    print(f"\n  after  : {after[0]} row(s), {after[1]} dated, "
          f"{after[2]} shown on the dashboard")
    gained_rows = after[0] - before[0]
    gained_dates = after[1] - before[1]
    gained_live = after[2] - before[2]
    print(f"  change : {gained_rows:+} row(s), {gained_dates:+} dated, "
          f"{gained_live:+} on the dashboard")
    if gained_dates == 0 and gained_rows == 0:
        # Said out loud. "Nothing changed" is a result, and reading it as
        # success is how a fix stays undeployed for a week.
        print("\n  NOTHING CHANGED. The scrape ran and wrote nothing new — "
              "either the source returned what was already stored and every "
              "row already had its deadline, or the fix you are testing is "
              "not in this deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
