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
import shutil
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="the other .db file to merge FROM")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--members", action="store_true", help="merge team_members too")
    ap.add_argument(
        "--active-only", action="store_true",
        help="skip expired rows (the dashboard shows live ones by default, and "
             "the Archive toggle was removed, so expired rows are invisible in "
             "the UI — they only add size)",
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
        shutil.copy2(dst_path, backup)
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

    where = " WHERE status = 'Active'" if args.active_only else ""
    rows = src.execute(f"SELECT {','.join(shared)} FROM opportunities{where}").fetchall()
    if args.active_only:
        total_src = src.execute("SELECT count(*) FROM opportunities").fetchone()[0]
        print(f"source offers        : {len(rows)} active "
              f"(skipping {total_src - len(rows)} expired)")
    else:
        print(f"source offers        : {len(rows)} (including expired)")

    new = [tuple(r[c] for c in shared) for r in rows if r["unique_id"] not in have]
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
        print(f"inserted             : {dst.total_changes}")

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
