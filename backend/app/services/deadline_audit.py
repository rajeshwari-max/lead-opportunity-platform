"""Keep the deadline column honest.

Three faults were found in live data and are corrected here:

  1. Sentinel dates stored as real ones. DevelopmentAid publishes 9999-12-31
     to mean "no closing date"; the parser accepted it literally, so 148 rows
     claimed a deadline in the year 9999 and the dashboard offered "2,911,000
     days left". These become NULL, which the UI already renders as "Ongoing".

  2. Rows still marked Active with a deadline in the past — 1,674 of them. The
     status is set once at scrape time and never revisited, so it drifts as
     dates pass. The live view filters on the date as well, so nothing wrong is
     displayed, but every count derived from `status` alone is inflated.

  3. Deadlines implausibly far out. Anything beyond three years is far more
     likely to be a misparse (a reference number read as a year, a dd/mm/yy
     misread) than a real call, so it is treated as unknown rather than trusted.

All three are idempotent and safe to run repeatedly.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("scraper")

# Values that mean "no deadline" rather than a date. 9999-12-31 is the common
# one; 0001-01-01 and 1970-01-01 turn up as null-date artefacts.
SENTINELS = {date(9999, 12, 31), date(1, 1, 1), date(1970, 1, 1), date(1900, 1, 1)}

# Beyond this, a "deadline" is almost certainly a parsing accident.
MAX_YEARS_AHEAD = 3


def is_sentinel(value: date | None) -> bool:
    if value is None:
        return False
    if value in SENTINELS:
        return True
    return value > date.today() + timedelta(days=365 * MAX_YEARS_AHEAD)


def audit_deadlines() -> dict:
    """Fix sentinels, refresh stale Active/Expired status, retire stale Ongoing."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.core.config import settings
    from app.database.db import session_scope
    from app.database.models import Opportunity, ScrapeRun, Status

    stats = {"sentinels_cleared": 0, "expired": 0, "reactivated": 0, "stale_ongoing": 0}
    today = date.today()
    # An undated row has no deadline to expire by, so the only evidence that it
    # is over is that the source stopped listing it. That is what `last_seen`
    # records and what this retires on.
    #
    # This is the fix for closed calls staying on the dashboard: every "Ongoing"
    # row was immortal. A funder page that states no closing date produces
    # assume_active=True in the generic scraper, and nothing downstream ever
    # revisited it — so a call taken down in March was still shown in August.
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.ongoing_max_age_days)

    with session_scope() as db:
        # A source only gets to retire its own undated rows if it has actually
        # been working since the cutoff. Without this, a source that is DOWN
        # deletes its whole catalogue from the live view: DevelopmentAid was
        # blocked by Cloudflare from 18 Aug and returned 0 rows on every run, so
        # an unguarded rule would have quietly retired all 3,125 of its ongoing
        # listings and called it housekeeping. "We stopped seeing it" only means
        # "it is gone" when we were genuinely looking.
        healthy = {
            src for (src,) in db.execute(
                select(ScrapeRun.source_website)
                .where(ScrapeRun.started_at >= cutoff, ScrapeRun.found > 0)
                .group_by(ScrapeRun.source_website)
            )
        }
        skipped_unhealthy = 0

        # `.scalars()` streams rather than building a list, which looks safe —
        # but the session's identity map still holds every row it has yielded,
        # so peak memory is the same. iter_opportunities expunges each chunk.
        from app.services.backfill import iter_opportunities

        for opp in iter_opportunities(db):
            if is_sentinel(opp.deadline):
                # NULL means "ongoing", which is what the source meant.
                opp.deadline = None
                stats["sentinels_cleared"] += 1

            if opp.deadline is not None:
                should_be = Status.EXPIRED if opp.deadline < today else Status.ACTIVE
                if opp.status != should_be:
                    opp.status = should_be
                    key = "expired" if should_be is Status.EXPIRED else "reactivated"
                    stats[key] += 1
                continue

            # Undated. last_seen can be NULL on rows written before the column
            # existed; the migration backfills it from date_scraped, but guard
            # anyway rather than retiring a row on a missing value.
            if opp.status is not Status.ACTIVE:
                continue
            seen = getattr(opp, "last_seen", None) or opp.date_scraped
            if seen is None:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen >= cutoff:
                continue
            if opp.source_website not in healthy:
                skipped_unhealthy += 1
                continue
            opp.status = Status.EXPIRED
            stats["stale_ongoing"] += 1

        if skipped_unhealthy:
            log.warning(
                "Deadline audit: left %s undated listing(s) live because their source "
                "has not returned anything in %s days — fix the source rather than "
                "letting its catalogue age out silently",
                skipped_unhealthy, settings.ongoing_max_age_days,
            )

    if any(stats.values()):
        log.info(
            "Deadline audit: %s sentinel(s) cleared, %s expired, %s reactivated, "
            "%s undated listing(s) retired after %s days unseen",
            stats["sentinels_cleared"], stats["expired"], stats["reactivated"],
            stats["stale_ongoing"], settings.ongoing_max_age_days,
        )
    return stats
