"""Safely collapse dashboard duplicates; preview by default, backup required.

Duplicate identity intentionally matches scripts/db_health.py: same normalized
title, funder, source and deadline. Different deadlines are different calls.
Rows referenced by user/workspace data, approved by a person, or carrying a
human classification are never deleted. If two protected rows collide, the
whole cluster is left alone for manual review.

What does NOT protect a row (2026-10-21). Every table that pointed at an
opportunity used to count as "user data", including two the system fills in
by itself: opportunity_intelligence (a score for every lead) and sent_log
(which leads were emailed). Nearly every row was therefore "protected" and a
deploy reported 2,479 protected collisions and removed nothing. Now:
  - opportunity_intelligence rows of a removed copy are deleted (the score is
    recalculated for the copy that is kept);
  - sent_log rows move to the kept copy, so nobody is emailed it again;
  - experience_events "prediction" rows (machine audit) move to the kept copy;
    any other experience event (a person's review or application step)
    still protects the row, as do saved/viewed leads, application journeys,
    human feedback, Wrike tasks and any table not listed here.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# Tables the system writes by itself; handled when a copy is removed instead
# of protecting it. See the module docstring.
_REGENERATED = {"opportunity_intelligence", "sent_log"}
# Rows of this table protect an opportunity unless they are machine predictions.
_MACHINE_EVENTS_TABLE = "experience_events"


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _protected_ids(db: sqlite3.Connection) -> set[int]:
    protected: set[int] = set()
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    for table in tables:
        if table in _REGENERATED:
            continue
        columns = {row[1] for row in db.execute(
            f"PRAGMA table_info({_quote(table)})"
        )}
        refs = {"opportunity_id"} & columns
        for fk in db.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            if fk[2] == "opportunities" and fk[4] in ("id", None, ""):
                refs.add(fk[3])
        machine_only = (table == _MACHINE_EVENTS_TABLE and "event_type" in columns)
        for column in refs:
            protected.update(row[0] for row in db.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IS NOT NULL"
                + (" AND event_type != 'prediction'" if machine_only else "")
            ))
    return protected


def _release_links(db: sqlite3.Connection, drop: int, keep: int) -> None:
    """Move or remove system-written rows that point at a copy being removed."""
    tables = _tables(db)
    if "opportunity_intelligence" in tables:
        db.execute("DELETE FROM opportunity_intelligence WHERE opportunity_id=?", (drop,))
    if "sent_log" in tables:
        # A member already logged for the kept copy keeps that row; the
        # duplicate's row is then redundant.
        db.execute("UPDATE OR IGNORE sent_log SET opportunity_id=? "
                   "WHERE opportunity_id=?", (keep, drop))
        db.execute("DELETE FROM sent_log WHERE opportunity_id=?", (drop,))
    if _MACHINE_EVENTS_TABLE in tables:
        db.execute(f"UPDATE {_MACHINE_EVENTS_TABLE} SET opportunity_id=? "
                   "WHERE opportunity_id=? AND event_type='prediction'", (keep, drop))


def _score(row: sqlite3.Row) -> tuple:
    url = (row["opportunity_url"] or "").strip()
    return (
        bool(row["approved"]),
        row["verticals_source"] == "human" or row["classification_source"] == "human",
        row["status"] == "Active",
        bool(url and "/search" not in url),
        bool(row["documents_json"] or row["pdf_url"]),
        len(row["summary"] or ""),
        row["last_seen"] or row["date_scraped"] or "",
        row["id"],
    )


def _norm_url(url: str) -> str:
    u = (url or "").strip().casefold().split("#")[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u[4:] if u.startswith("www.") else u


def _undated_evidence(row) -> str:
    summary = " ".join((row["summary"] or "").split()).casefold()[:300]
    return "s:" + summary if summary else "u:" + _norm_url(row["opportunity_url"])


def plan(db: sqlite3.Connection) -> tuple[list[dict], int]:
    columns = {row[1] for row in db.execute("PRAGMA table_info(opportunities)")}
    required = {
        "id", "title", "organization", "source_website", "deadline", "approved",
        "verticals_source", "classification_source", "status", "opportunity_url",
        "summary", "last_seen", "date_scraped", "pdf_url", "documents_json",
    }
    if not required <= columns:
        raise RuntimeError(f"Unexpected database schema; missing {sorted(required - columns)}")
    protected = _protected_ids(db)
    groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    has_country = "country" in columns
    for row in db.execute("SELECT * FROM opportunities ORDER BY id"):
        key = (
            (row["title"] or "").strip().casefold(),
            (row["organization"] or "").strip().casefold(),
            (row["source_website"] or "").strip().casefold(),
            row["deadline"] or "",
            # Same generic title for a different country is a different call
            # (e.g. one programme announced separately per country).
            (row["country"] or "").strip().casefold() if has_country else "",
            # Without a deadline, title + funder is too weak on its own:
            # generic titles ("Call for proposals for action grants…") are
            # reused for different calls. Undated rows must also agree on the
            # call's own text, or on the link when there is no text.
            "" if row["deadline"] else _undated_evidence(row),
        )
        if key[0]:
            groups[key].append(row)

    actions: list[dict] = []
    blocked = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        guarded = [row for row in rows if row["id"] in protected or row["approved"]
                   or row["verticals_source"] == "human"
                   or row["classification_source"] == "human"]
        if len(guarded) > 1:
            blocked += len(rows) - 1
            continue
        keeper = guarded[0] if guarded else max(rows, key=_score)
        for row in rows:
            if row["id"] == keeper["id"]:
                continue
            if row["id"] in protected or row["approved"]:
                blocked += 1
                continue
            actions.append({
                "drop": row["id"], "keep": keeper["id"],
                "title": row["title"], "source": row["source_website"],
                "deadline": row["deadline"],
            })
    return actions, blocked


def main() -> int:
    from app.core.config import settings
    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        parser.error("Only file-backed SQLite is supported")
    path = Path(url.database).resolve(strict=True)
    mode = "rw" if args.apply else "ro"
    with sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=60) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        actions, blocked = plan(db)
        print(f"Safe duplicate rows to remove: {len(actions):,}")
        print(f"Protected collisions left for review: {blocked:,}")
        for item in actions[:max(0, args.samples)]:
            print(f"Remove {item['drop']} / keep {item['keep']}: "
                  f"{item['source']} | {item['deadline'] or '(no date)'} | {item['title'][:90]}")
        if not args.apply or not actions:
            print("No changes made.")
            return 0

        backup = args.backup.expanduser().resolve()
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise FileExistsError(f"Refusing to overwrite backup: {backup}")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed; nothing deleted")
        audit = backup.with_suffix(backup.suffix + ".removed.json")
        audit.write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in actions:
            _release_links(db, item["drop"], item["keep"])
            db.execute("DELETE FROM opportunities WHERE id=?", (item["drop"],))
        db.commit()
        print(f"Deleted {len(actions):,} duplicate row(s). Backup: {backup}")
        print(f"Deletion audit: {audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
