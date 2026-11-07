"""Active leads that are not in English, and their English editions.

    python scripts/english_only_audit.py                       # report only
    python scripts/english_only_audit.py --apply --backup F.db # switch rows
    python scripts/english_only_audit.py --apply --backup F.db --archive-rest

--apply replaces a non-English Active row with the source's own English
edition of the same page (title, summary, link) when one exists. Nothing is
machine-translated, and the row keeps its id and unique_id.
--archive-rest also marks the remaining non-English Active rows Expired
(archived, not deleted). Without it they are left as they are.
The database is backed up first with SQLite's online backup.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                              # noqa: E402

from app.core.config import settings                       # noqa: E402
from app.database.db import session_scope                  # noqa: E402
from app.database.models import Opportunity, Status        # noqa: E402
from app.services import english_only as eo                # noqa: E402


def _backup(target: str) -> None:
    src = sqlite3.connect(settings.database_url.split("///")[-1])
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close(); src.close()
    print(f"backup: {target}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default="")
    ap.add_argument("--archive-rest", action="store_true")
    ap.add_argument("--limit", type=int, default=300, help="max English-edition lookups")
    args = ap.parse_args()
    if args.apply and not args.backup:
        ap.error("--apply needs --backup <file>")

    with session_scope() as db:
        rows = [(o.id, o.source_website, o.title or "", o.summary or "",
                 o.eligibility or "", o.opportunity_url or "")
                for o in db.execute(select(Opportunity)
                                    .where(Opportunity.status == Status.ACTIVE)).scalars()]
    foreign = [r for r in rows if not eo.detect(r[2], r[3], r[4])[0]]
    print(f"{len(foreign)} of {len(rows)} Active row(s) are not in English")
    for src, n in Counter(r[1] for r in foreign).most_common(15):
        print(f"  {n:5}  {src}")

    settings.english_lookup_max_per_run = args.limit
    eo.reset_run_budget()
    switched: list[tuple[int, tuple[str, str, str]]] = []
    for r in foreign:
        found = eo._fetch_english(r[5], settings.english_lookup_timeout_s) \
            if len(switched) < args.limit and r[5] else None
        if found:
            switched.append((r[0], found))
    print(f"\n{len(switched)} have an English edition on the source")
    for oid, (title, _, url) in switched[:8]:
        print(f"  #{oid}: {title[:60]}  <- {url}")
    rest = {r[0] for r in foreign} - {oid for oid, _ in switched}
    print(f"{len(rest)} are published only in another language")

    if not args.apply:
        print("\n(report only — re-run with --apply --backup <file>)")
        return 0
    _backup(args.backup)
    with session_scope() as db:
        for oid, (title, desc, url) in switched:
            o = db.get(Opportunity, oid)
            o.title = title[:500]
            o.opportunity_url = url
            if desc and not eo.detect(o.summary or "")[0]:
                o.summary = desc
        if args.archive_rest:
            for oid in rest:
                db.get(Opportunity, oid).status = Status.EXPIRED
    print(f"switched {len(switched)} to English"
          + (f"; archived {len(rest)}" if args.archive_rest else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
