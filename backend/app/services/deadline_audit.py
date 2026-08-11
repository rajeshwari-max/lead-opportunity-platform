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
    """Fix sentinels and refresh stale Active/Expired status. Returns counts."""
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity, Status

    stats = {"sentinels_cleared": 0, "expired": 0, "reactivated": 0}
    today = date.today()

    with session_scope() as db:
        for opp in db.execute(select(Opportunity)).scalars():
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

    if any(stats.values()):
        log.info(
            "Deadline audit: %s sentinel(s) cleared, %s expired, %s reactivated",
            stats["sentinels_cleared"], stats["expired"], stats["reactivated"],
        )
    return stats
