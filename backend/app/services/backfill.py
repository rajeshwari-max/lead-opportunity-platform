"""Walk every opportunity without loading every opportunity.

The problem this exists to remove
---------------------------------
Four startup backfills were written as:

    for opp in db.execute(select(Opportunity)).scalars().all():

`.all()` materialises the entire table as fully-hydrated ORM objects in one
Python list, and the session's identity map then holds every one of them until
the pass finishes. That was acceptable at 106,854 rows. It is not at 279,129:
measured on 2026-09-01, a freshly restarted Gunicorn worker reached **1.53 GB
RSS and 84% CPU thirty seconds after boot**, before serving a single request,
because `main.py` runs eight of these passes on every start.

The symptom read like a leak — memory climbing while the process ran — but
nothing was leaking. The memory was one enormous list being built.

What this changes, and what it deliberately does not
----------------------------------------------------
Every row is still visited, in the same order, with the same computation and
the same update rule. The only difference is that at most `chunk` rows are
resident at a time. A backfill that updated N rows before updates the same N.

Keyset pagination, not OFFSET
-----------------------------
`LIMIT/OFFSET` gets slower with every page — the database still walks the rows
it is skipping — and worse, it is not stable: a row inserted or deleted mid-scan
shifts the window, so rows get visited twice or missed entirely. Scrapes run
against this database while maintenance is going, so that is a real risk here,
not a theoretical one. Ordering by primary key and asking for `id > last` is
stable under concurrent writes and uses the index every time.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("scraper")

# Big enough that the per-chunk query overhead is negligible, small enough that
# a chunk of hydrated rows is a few megabytes rather than a gigabyte.
DEFAULT_CHUNK = 1000


def iter_opportunities(db: Session, chunk: int = DEFAULT_CHUNK,
                       where=None) -> Iterator:
    """Yield every Opportunity in id order, `chunk` at a time.

    The caller may modify the yielded objects. Each chunk is flushed and
    expunged before the next is fetched, so changes reach the database and the
    identity map does not grow — which is the whole point.

    `where` narrows the scan. Use it: a backfill that only needs rows missing a
    field should not read the ones that have it.
    """
    from app.database.models import Opportunity

    last_id = 0
    while True:
        stmt = select(Opportunity).where(Opportunity.id > last_id)
        if where is not None:
            stmt = stmt.where(where)
        rows = db.execute(
            stmt.order_by(Opportunity.id).limit(chunk)).scalars().all()
        if not rows:
            return
        last_id = rows[-1].id
        for row in rows:
            yield row
        # Flush first: expunging a dirty object would discard the change the
        # caller just made to it, which would turn this from a memory fix into
        # a silent data-loss bug.
        db.flush()
        db.commit()
        for row in rows:
            db.expunge(row)
        rows.clear()


def run_backfill(name: str, apply_to: Callable[[object], bool],
                 chunk: int = DEFAULT_CHUNK, where=None) -> int:
    """Run one backfill over the whole table, bounded. Returns rows updated.

    `apply_to(row)` returns True when it changed the row. Exceptions on a single
    row are logged and skipped rather than abandoning the pass — one unparseable
    summary must not stop the other 279,128 rows being repaired.
    """
    from app.database.db import session_scope

    updated = 0
    seen = 0
    with session_scope() as db:
        for row in iter_opportunities(db, chunk=chunk, where=where):
            seen += 1
            try:
                if apply_to(row):
                    updated += 1
            except Exception:                                   # noqa: BLE001
                log.exception("[%s] row %s failed", name, getattr(row, "id", "?"))
    if updated:
        log.info("%s: updated %s of %s row(s)", name, updated, seen)
    return updated
