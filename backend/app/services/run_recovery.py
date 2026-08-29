"""Tidy up runs whose process is gone, before anything reads them.

The 2026-08-29 baseline found 106 rows sitting in `status='running'`. They are
not one population, and the difference is diagnostic rather than cosmetic:

  * 30 have a `finished_at`.
    `ScraperManager._close_run` stamps `finished_at` and then copies
    `prog["status"]`, which is only set to "completed" AFTER the crawl loop.
    So a `running` row WITH a finish time means `_close_run` ran but the status
    was never advanced — the source raised inside the loop. The record is
    otherwise complete. These were crashes.

  * 76 have none.
    `_close_run` never ran at all. The process was killed, the container went
    away, or the event loop died mid-source.

Marking all 106 the same way would be exactly the guess the outcome taxonomy
exists to prevent, applied 106 times in one statement. They get different
terminal states, and both say in `error_message` how the conclusion was reached
so nobody has to re-derive it later.

Runs started by THIS process are never touched: a fresh worker_id plus a live
heartbeat is positive evidence a run is alive, and reconciliation that cannot
tell "abandoned" from "in progress" is worse than none.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.database.db import session_scope
from app.database.models import ScrapeRun
from app.services import run_lock
from app.services.scrape_outcome import Outcome, reconcile_stale

log = logging.getLogger("scraper")

# A run must be silent for this long before it is presumed dead. Deliberately
# generous: DevelopmentAid legitimately runs for tens of minutes on one source,
# and declaring a working run abandoned would close its record underneath it.
STALE_AFTER_S = 900


def reconcile_stale_runs(stale_after_s: int = STALE_AFTER_S) -> dict:
    """Close out abandoned runs. Returns counts for the startup log.

    Idempotent — a second call finds nothing, because everything it touches
    stops being `running`.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(seconds=stale_after_s)
    me = run_lock.worker_id()

    crashed = abandoned = 0
    with session_scope() as db:
        rows = db.execute(
            select(ScrapeRun).where(
                ScrapeRun.status == "running",
                # Never touch a run that is demonstrably alive: ours, or one
                # still heartbeating. Absence of a heartbeat is not proof of
                # death for the 106 historical rows (the column did not exist
                # when they were written), which is why the age of the run
                # itself is the fallback test.
                or_(ScrapeRun.worker_id.is_(None), ScrapeRun.worker_id != me),
                or_(ScrapeRun.heartbeat_at.is_(None),
                    ScrapeRun.heartbeat_at < cutoff),
                ScrapeRun.started_at < cutoff,
            )
        ).scalars().all()

        for run in rows:
            outcome, why = reconcile_stale("running", run.finished_at is not None)
            run.status = outcome.value
            run.outcome = outcome.value
            run.error_message = why
            if run.finished_at is None:
                # No finish time was ever written, so the only honest stamp is
                # the last moment we know the run existed. Using `now` would
                # claim a duration that includes however long the server was
                # down — on this database, potentially days.
                run.finished_at = run.heartbeat_at or run.started_at
            if outcome is Outcome.CRASHED:
                crashed += 1
            else:
                abandoned += 1

    result = {"crashed": crashed, "abandoned": abandoned,
              "total": crashed + abandoned}
    if result["total"]:
        log.warning(
            "[recovery] closed %s abandoned run(s): %s crashed inside the crawl "
            "loop (finish time present, terminal status never written), %s lost "
            "their process (no finish time at all)",
            result["total"], crashed, abandoned,
        )
    else:
        log.info("[recovery] no abandoned runs to close")
    return result


def reconcile_lease(ttl_s: int = run_lock.DEFAULT_TTL_S) -> dict:
    """Release a lease whose holder is gone.

    A crash leaves the lease held by a dead process. `acquire()` would take it
    over on its own once the TTL passed, but doing it explicitly at startup
    means the dashboard shows an accurate lock state immediately instead of
    reporting a scrape in progress for the next ten minutes.
    """
    st = run_lock.status(ttl_s=ttl_s)
    if st["held"] and st["stale"]:
        run_lock.force_release(
            f"holder {st['holder']} has not heartbeat for "
            f"{st.get('heartbeat_age_s', '?')}s — presumed gone at startup"
        )
        return {"released": True, "was_held_by": st["holder"]}
    if st["held"]:
        log.info("[recovery] lease is held by %s and still heartbeating — leaving it",
                 st["holder"])
    return {"released": False, "was_held_by": st["holder"]}


def run_startup_recovery() -> dict:
    """Everything that must happen before the app serves data or schedules a run.

    Called from the lifespan handler ahead of scheduler.start(), so a scheduled
    or manual scrape can never begin while records are still inconsistent.
    """
    return {
        "runs": reconcile_stale_runs(),
        "lease": reconcile_lease(),
    }
