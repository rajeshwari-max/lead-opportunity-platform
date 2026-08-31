"""Merge opportunities from another copy of the database into this one.

    python scripts/merge_db.py --source /home/ubuntu/from_pc.db
    python scripts/merge_db.py --source ... --dry-run      # report only
    python scripts/merge_db.py --source ... --members      # team members too

Used to bring a laptop's scrape history onto the server without losing what the
server has collected since deployment.

What it does and does not touch, and why:

  opportunities   Inserted when `unique_id` is not already present. Rows that
                  exist on both sides are LEFT ALONE — the target's copy may
                  carry an approval or a corrected link, and the incoming row
                  is not necessarily newer.

  team_members    Only with --members, matched on email, existing rows kept.

  sent_log        Never touched. It references opportunities by numeric id, and
  reminder_log    those ids are assigned per-database — row 42 on the laptop is
                  a different opportunity from row 42 on the server. Copying
                  them across would silently attach "already emailed" flags to
                  the wrong opportunities, which is worse than not merging: it
                  would suppress genuine emails with no visible symptom.

The target is backed up first. A merge that goes wrong should be recoverable.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


def target_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise SystemExit(f"Only SQLite is supported here (got {url})")
    return Path(url.split("///")[-1])


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def backup_database(source: Path, destination: Path) -> None:
    """Create one consistent SQLite backup, including committed WAL pages."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="the other .db file to merge FROM")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--members", action="store_true", help="merge team_members too")
    ap.add_argument(
        "--experts", action="store_true",
        help="merge Expert Pool counts. DevelopmentAid's bot protection blocks "
             "datacentre IPs, so a server cannot refresh these itself — refresh "
             "them on a machine that can reach the site, then carry them over",
    )
    ap.add_argument(
        "--active-only", action="store_true",
        help="skip expired rows (the dashboard shows live ones by default, and "
             "the Archive toggle was removed, so expired rows are invisible in "
             "the UI — they only add size)",
    )
    ap.add_argument(
        "--only-source", default="",
        help="merge only this exact source_website value, e.g. DevelopmentAid",
    )
    args = ap.parse_args()

    src_path, dst_path = Path(args.source), target_path()
    if not src_path.exists():
        raise SystemExit(f"Source not found: {src_path}")
    print(f"source : {src_path}\ntarget : {dst_path}\n")

    if not args.dry_run:
        backup = dst_path.with_name(
            f"{dst_path.stem}.before-merge-{datetime.now():%Y%m%d-%H%M%S}.db"
        )
        backup_database(dst_path, backup)
        print(f"backup : {backup}\n")

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    src.row_factory = sqlite3.Row

    # Only columns both schemas share — the two databases may be at different
    # migration levels, and an older export shouldn't abort the whole merge.
    shared = [c for c in columns(dst, "opportunities")
              if c in columns(src, "opportunities") and c != "id"]
    print(f"merging {len(shared)} shared columns")

    have = {r[0] for r in dst.execute("SELECT unique_id FROM opportunities")}
    print(f"target already holds : {len(have)}")

    filters: list[str] = []
    params: list[str] = []
    if args.active_only:
        filters.append("status = 'Active'")
    if args.only_source:
        filters.append("lower(source_website) = lower(?)")
        params.append(args.only_source)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    rows = src.execute(
        f"SELECT {','.join(shared)} FROM opportunities{where}", params
    ).fetchall()
    scope = []
    if args.active_only:
        scope.append("active")
    if args.only_source:
        scope.append(f"source={args.only_source!r}")
    print(f"source offers        : {len(rows)}"
          f" ({', '.join(scope) if scope else 'all rows'})")

    # The database constraint is the final guard, but dedupe the transfer batch
    # here as well so the report is exact even when an old/source database lacks
    # that constraint.  One unique_id can be inserted at most once.
    seen = set(have)
    new = []
    duplicates = 0
    for row in rows:
        uid = row["unique_id"]
        if uid in seen:
            duplicates += 1
            continue
        seen.add(uid)
        new.append(tuple(row[c] for c in shared))
    print(f"duplicates skipped   : {duplicates}")
    print(f"genuinely new        : {len(new)}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
    elif new:
        dst.executemany(
            f"INSERT OR IGNORE INTO opportunities ({','.join(shared)}) "
            f"VALUES ({','.join('?' * len(shared))})",
            new,
        )
        dst.commit()
        # len(new), not dst.total_changes: the FTS triggers write a row into the
        # search index for every insert, so total_changes counts those too and
        # reports a number several times larger than the opportunities added.
        print(f"inserted             : {len(new)}")

    if args.members:
        m_shared = [c for c in columns(dst, "team_members")
                    if c in columns(src, "team_members") and c != "id"]
        emails = {r[0] for r in dst.execute("SELECT email FROM team_members")}
        m_rows = src.execute(f"SELECT {','.join(m_shared)} FROM team_members").fetchall()
        m_new = [tuple(r[c] for c in m_shared) for r in m_rows if r["email"] not in emails]
        print(f"\nteam members new     : {len(m_new)}")
        if m_new and not args.dry_run:
            dst.executemany(
                f"INSERT OR IGNORE INTO team_members ({','.join(m_shared)}) "
                f"VALUES ({','.join('?' * len(m_shared))})",
                m_new,
            )
            dst.commit()

    if args.experts:
        # Upsert by vertical, and only when the incoming row is actually newer.
        # These are a snapshot of a live count, so "most recently refreshed
        # wins" is the right rule — unlike opportunities, where the target's
        # copy may carry an approval and must not be overwritten.
        e_shared = [c for c in columns(dst, "expert_counts")
                    if c in columns(src, "expert_counts") and c != "id"]
        existing = {
            r[0]: r[1] for r in dst.execute("SELECT vertical, updated_at FROM expert_counts")
        }
        rows_e = src.execute(f"SELECT {','.join(e_shared)} FROM expert_counts").fetchall()
        newer = [r for r in rows_e
                 if r["vertical"] not in existing
                 or str(r["updated_at"] or "") > str(existing[r["vertical"]] or "")]
        print(f"\nexpert counts newer  : {len(newer)} of {len(rows_e)}")
        for r in newer:
            print(f"    {r['vertical']:34} {r['count']}")
        if newer and not args.dry_run:
            for r in newer:
                dst.execute("DELETE FROM expert_counts WHERE vertical = ?", (r["vertical"],))
                dst.execute(
                    f"INSERT INTO expert_counts ({','.join(e_shared)}) "
                    f"VALUES ({','.join('?' * len(e_shared))})",
                    tuple(r[c] for c in e_shared),
                )
            dst.commit()

    total = dst.execute("SELECT count(*) FROM opportunities").fetchone()[0]
    print(f"\ntarget now holds     : {total}")
    print("\nRebuilding the search index…")
    if not args.dry_run:
        try:
            dst.execute("INSERT INTO opportunities_fts(opportunities_fts) VALUES('rebuild')")
            dst.commit()
            print("  done")
        except sqlite3.Error as exc:
            print(f"  skipped ({exc}) — restart the API and it rebuilds itself")

    src.close()
    dst.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
