"""A scrape lease that survives process boundaries.

Why APScheduler's max_instances is not enough
---------------------------------------------
`max_instances=1` is enforced inside one scheduler object, in one process.
Everything that can produce a second scraper lives outside that boundary:

  * a Gunicorn reload that overlaps old and new workers
  * a deploy where supervisor starts the new process before the old one exits
  * `workers = 1` being raised by someone who does not know the scheduler is
    in-process (the config says so, but configs get edited)
  * a manual scrape started from the dashboard on one process while a
    scheduled one runs on another

Two scrapers on one SQLite file is not a theoretical problem. They share a
177 MB database, a browser budget sized for a small EC2 box, and the same
source list — so the second run mostly re-fetches what the first is fetching,
doubles memory, and produces duplicate rows that the dedup key then has to
absorb.

The lease
---------
One row, held by `worker_id`, refreshed by a heartbeat. Acquisition is a single
conditional UPDATE, which SQLite executes atomically, so two processes racing
cannot both win: exactly one sees `rowcount == 1`.

A holder that dies stops heartbeating, and after `lease_ttl_s` the lease is
takeable again. That TTL is the only tuning decision here, and it trades two
failures against each other: too short and a slow-but-alive run gets its lease
stolen mid-scrape; too long and a crashed process blocks scraping until it
expires. Ten minutes with a 30-second heartbeat means 20 consecutive missed
beats before anyone concludes a run is dead.

This module deliberately does not know what a scrape is. It hands out and takes
back a token, and reports honestly who holds it — nothing else.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database.db import session_scope

log = logging.getLogger("scraper")

LEASE_ID = 1                    # single-row table; there is only ever one scrape
DEFAULT_TTL_S = 600             # 10 minutes without a heartbeat = abandoned
HEARTBEAT_S = 30


def worker_id() -> str:
    """host:pid — enough to find the process that holds a stuck lease."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LeaseNotAcquired(RuntimeError):
    """Raised with the current holder named, so the caller can say who."""

    def __init__(self, holder: str, since: datetime | None, age_s: float) -> None:
        self.holder, self.since, self.age_s = holder, since, age_s
        super().__init__(
            f"a scrape is already running on {holder}"
            + (f" (started {since:%Y-%m-%d %H:%M} UTC, last heartbeat "
               f"{age_s:.0f}s ago)" if since else "")
        )


def _ensure_row(db) -> None:
    db.execute(text(
        "INSERT OR IGNORE INTO scrape_lease (id, worker_id, acquired_at, "
        "heartbeat_at, label) VALUES (:id, NULL, NULL, NULL, '')"
    ), {"id": LEASE_ID})


def acquire(label: str = "", ttl_s: int = DEFAULT_TTL_S) -> str:
    """Take the lease, or raise LeaseNotAcquired naming who holds it.

    The UPDATE's WHERE clause is the entire concurrency argument: it matches
    only when the lease is free, already ours, or stale. Two processes issuing
    it at the same instant are serialised by SQLite's write lock, so the loser
    updates zero rows and finds out it lost.
    """
    me, now = worker_id(), _now()
    cutoff = now - timedelta(seconds=ttl_s)
    with session_scope() as db:
        _ensure_row(db)
        result = db.execute(text(
            "UPDATE scrape_lease "
            "   SET worker_id = :me, acquired_at = :now, heartbeat_at = :now, "
            "       label = :label "
            " WHERE id = :id "
            "   AND (worker_id IS NULL "
            "        OR worker_id = :me "
            "        OR heartbeat_at IS NULL "
            "        OR heartbeat_at < :cutoff)"
        ), {"me": me, "now": now, "label": label[:200], "id": LEASE_ID,
            "cutoff": cutoff})
        if result.rowcount == 1:
            log.info("[run-lock] lease acquired by %s (%s)", me, label or "scrape")
            return me

        row = db.execute(text(
            "SELECT worker_id, acquired_at, heartbeat_at FROM scrape_lease "
            "WHERE id = :id"), {"id": LEASE_ID}).first()

    holder = (row[0] if row else None) or "another process"
    since = row[1] if row else None
    beat = row[2] if row else None
    age = (now - beat).total_seconds() if beat else float("inf")
    raise LeaseNotAcquired(holder, since, age)


def heartbeat(me: str) -> bool:
    """Refresh the lease. False means we no longer hold it.

    A caller that gets False should stop: something concluded this run was dead
    and gave the lease away, so continuing would produce the concurrent scrape
    the lease exists to prevent. Losing a lease is not an error to swallow.
    """
    with session_scope() as db:
        result = db.execute(text(
            "UPDATE scrape_lease SET heartbeat_at = :now "
            " WHERE id = :id AND worker_id = :me"
        ), {"now": _now(), "id": LEASE_ID, "me": me})
        return result.rowcount == 1


def release(me: str) -> None:
    """Give the lease back. Scoped to us, so a late release cannot clear a
    lease that has since been legitimately taken by someone else."""
    with session_scope() as db:
        result = db.execute(text(
            "UPDATE scrape_lease "
            "   SET worker_id = NULL, heartbeat_at = NULL, label = '' "
            " WHERE id = :id AND worker_id = :me"
        ), {"id": LEASE_ID, "me": me})
    if result.rowcount == 1:
        log.info("[run-lock] lease released by %s", me)
    else:
        log.warning("[run-lock] %s tried to release a lease it no longer held — "
                    "it was taken over while this run was in progress", me)


def status(ttl_s: int = DEFAULT_TTL_S) -> dict:
    """Who holds it, and is that holder still alive? For the dashboard."""
    with session_scope() as db:
        _ensure_row(db)
        row = db.execute(text(
            "SELECT worker_id, acquired_at, heartbeat_at, label FROM scrape_lease "
            "WHERE id = :id"), {"id": LEASE_ID}).first()
    if not row or not row[0]:
        return {"held": False, "holder": None, "stale": False,
                "acquired_at": None, "heartbeat_at": None, "label": ""}
    beat = row[2]
    age = (_now() - beat).total_seconds() if beat else float("inf")
    return {
        "held": True, "holder": row[0], "stale": age > ttl_s,
        "acquired_at": row[1], "heartbeat_at": beat,
        "heartbeat_age_s": None if age == float("inf") else round(age),
        "label": row[3] or "",
    }


def force_release(reason: str) -> bool:
    """Clear the lease regardless of holder. Administrative escape hatch.

    Exists because "how do I stop a stuck run" needs an answer in the runbook
    that is not "restart the server". It logs loudly and names the reason,
    because taking a lease from a process that might still be alive is exactly
    how two scrapers end up sharing one database.
    """
    with session_scope() as db:
        row = db.execute(text(
            "SELECT worker_id FROM scrape_lease WHERE id = :id"),
            {"id": LEASE_ID}).first()
        held_by = row[0] if row else None
        db.execute(text(
            "UPDATE scrape_lease SET worker_id = NULL, heartbeat_at = NULL, "
            "label = '' WHERE id = :id"), {"id": LEASE_ID})
    if held_by:
        log.warning("[run-lock] lease FORCE-RELEASED from %s — %s. If that "
                    "process is still alive it may still be scraping.",
                    held_by, reason)
        return True
    return False
