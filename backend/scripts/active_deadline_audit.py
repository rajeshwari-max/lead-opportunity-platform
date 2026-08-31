"""Audit and optionally archive passed Active opportunities in Asia/Kolkata.

Dry-run is the default. ``--apply`` changes only rows that are Active and have
a real deadline before today; it never deletes rows and never archives undated
or rolling opportunities. A backup path is mandatory for every apply.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from sqlalchemy import and_, func, or_, select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.db import SessionLocal, engine  # noqa: E402
from app.database.models import Opportunity, Status  # noqa: E402
from app.services.actionable import DeadlineState, application_today  # noqa: E402


def bucket_clauses(today: date) -> list[tuple[str, object]]:
    active = Opportunity.status == Status.ACTIVE
    return [
        ("active_deadline_null", and_(active, Opportunity.deadline.is_(None))),
        ("active_empty_or_invalid_deadline_text", and_(
            active,
            Opportunity.deadline.is_(None),
            Opportunity.deadline_raw.is_not(None),
            func.trim(Opportunity.deadline_raw) != "",
        )),
        ("active_rolling_open_ended", and_(
            active,
            Opportunity.deadline_state == DeadlineState.ROLLING.value,
        )),
        ("active_unknown_unassessed", and_(
            active,
            Opportunity.deadline.is_(None),
            or_(
                Opportunity.deadline_state.is_(None),
                Opportunity.deadline_state == DeadlineState.UNKNOWN.value,
            ),
        )),
        ("active_deadline_before_today", and_(
            active,
            Opportunity.deadline.is_not(None),
            Opportunity.deadline < today,
        )),
        ("active_deadline_today", and_(
            active,
            Opportunity.deadline == today,
        )),
    ]


def _row(row: Opportunity) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "source": row.source_website,
        "status": getattr(row.status, "value", str(row.status)),
        "deadline_state": row.deadline_state,
        "deadline": row.deadline.isoformat() if row.deadline else None,
        "deadline_raw": row.deadline_raw,
    }


def collect(session, today: date, sample_size: int = 5) -> dict:
    report = {"today_ist": today.isoformat(), "buckets_overlap": True, "buckets": {}}
    for name, clause in bucket_clauses(today):
        rows = list(session.execute(
            select(Opportunity).where(clause).order_by(Opportunity.id)
        ).scalars())
        by_source = Counter((row.source_website or "(unknown)") for row in rows)
        report["buckets"][name] = {
            "count": len(rows),
            "by_source": dict(sorted(by_source.items(), key=lambda item: (-item[1], item[0]))),
            "samples": [_row(row) for row in rows[:sample_size]],
        }
    return report


def sqlite_database_path() -> Path:
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        raise RuntimeError("automatic backup is implemented only for the configured SQLite database")
    return Path(engine.url.database).resolve()


def backup_database(destination: Path) -> list[str]:
    source = sqlite_database_path()
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(
            f"refusing to overwrite an existing backup: {destination}")
    # SQLite's online-backup API creates one transactionally consistent file
    # and includes committed pages still resident in WAL. A filesystem copy of
    # .db followed by -wal can capture them at different instants and look
    # healthy until restore, which is precisely when a backup must be boring.
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    return [str(destination)]


def archive_passed(session, today: date) -> int:
    result = session.execute(
        update(Opportunity)
        .where(
            Opportunity.status == Status.ACTIVE,
            Opportunity.deadline.is_not(None),
            Opportunity.deadline < today,
        )
        .values(status=Status.EXPIRED)
    )
    return int(result.rowcount or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="mark passed Active rows Expired; never deletes")
    parser.add_argument("--backup", default="",
                        help="required backup .db path when --apply is used")
    parser.add_argument("--json", default="", help="write the report to this path")
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires --backup PATH; no production write without recovery")

    today = application_today()
    backup_files: list[str] = []
    with SessionLocal() as session:
        before = collect(session, today, max(0, args.samples))
        changed = 0
        if args.apply:
            backup_files = backup_database(Path(args.backup))
            changed = archive_passed(session, today)
            session.commit()
        after = collect(session, today, max(0, args.samples))

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "backup_files": backup_files,
        "rows_marked_expired": changed,
        "before": before,
        "after": after,
        "rows_deleted": 0,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=True)
    print(rendered)
    if args.json:
        output = Path(args.json).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
