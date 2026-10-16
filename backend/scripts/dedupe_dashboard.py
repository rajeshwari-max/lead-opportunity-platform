"""Safely collapse dashboard duplicates; preview by default, backup required.

Duplicate identity intentionally matches scripts/db_health.py: same normalized
title, funder, source and deadline. Different deadlines are different calls.
Rows referenced by user/workspace data, approved by a person, or carrying a
human classification are never deleted. If two protected rows collide, the
whole cluster is left alone for manual review.
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


def _protected_ids(db: sqlite3.Connection) -> set[int]:
    protected: set[int] = set()
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )]
    for table in tables:
        columns = {row[1] for row in db.execute(
            f"PRAGMA table_info({_quote(table)})"
        )}
        refs = {"opportunity_id"} & columns
        for fk in db.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            if fk[2] == "opportunities" and fk[4] in ("id", None, ""):
                refs.add(fk[3])
        for column in refs:
            protected.update(row[0] for row in db.execute(
                f"SELECT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IS NOT NULL"
            ))
    return protected


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
    for row in db.execute("SELECT * FROM opportunities ORDER BY id"):
        key = (
            (row["title"] or "").strip().casefold(),
            (row["organization"] or "").strip().casefold(),
            (row["source_website"] or "").strip().casefold(),
            row["deadline"] or "",
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
            db.execute("DELETE FROM opportunities WHERE id=?", (item["drop"],))
        db.commit()
        print(f"Deleted {len(actions):,} duplicate row(s). Backup: {backup}")
        print(f"Deletion audit: {audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
