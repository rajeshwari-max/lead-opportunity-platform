"""The second class of duplicate: the same call, stored once per deadline.

    python scripts/dedupe_titles.py                      # preview, writes nothing
    python scripts/dedupe_titles.py --samples 40         # look harder before deciding
    python scripts/dedupe_titles.py --apply --backup ~/pre-titles-$(date +%F).db

What this catches that the first pass did not
---------------------------------------------
The earlier dedupe grouped on title + funder + source + DEADLINE, and after it
ran the database reported 0 duplicates. Then db_health.py surfaced this:

    rows  distinct titles  url
      20                1  https://www2.fundsforngos.org/fellowships/apply-for-fulbrigh

Twenty rows, ONE title, ONE url. They survived because their deadlines differ —
the same post scraped again and again as its date text changed, or as the
parser read it differently. Including the deadline in the key made every copy
look unique, which is exactly backwards: the deadline is the field that varies
BECAUSE it is the same record seen at different times.

So this groups on title + organization + source and ignores the deadline.

Which row survives, and why it is not the oldest
------------------------------------------------
The first pass kept MIN(id) — the earliest row. That is right when the copies
are identical and wrong here, because the earliest row carries the STALEST
deadline. Keeping it would leave the dashboard showing a closing date that has
already passed while the live one is deleted.

The keeper is chosen: latest deadline first, then most recently scraped, then
highest id. If a call genuinely re-opened with a new closing date, that is also
the row you want — the earlier instance is closed, and the current one is the
opportunity.

The guard, which is the important part
--------------------------------------
The argument above breaks on generic titles. "Request for Proposals" or
"Call for Applications" from the same funder can be two entirely different
opportunities that happen to share a name, and merging those destroys a real
lead rather than a copy.

So clusters whose title is shorter than --min-title-chars are NEVER touched.
They are counted and shown separately, because "we left these alone" is a
result, not an omission. Raise the threshold if the samples look risky; there
is no number that is safe for every source, which is why it is a flag and not
a constant.

Nothing is deleted without --apply, and --apply refuses to run without
--backup.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.database.db import session_scope  # noqa: E402

# Deadline deliberately absent — see the module docstring.
KEY = """
    lower(trim(title)),
    lower(trim(coalesce(organization,''))),
    lower(trim(coalesce(source_website,'')))
"""

# Latest deadline wins. NULLs sort last so a dated row always beats an undated
# one; then the most recent scrape; then the highest id as a stable tiebreak.
KEEPER = """
    ORDER BY (deadline IS NULL), deadline DESC, date_scraped DESC, id DESC
"""


def db_path() -> Path:
    from app.core.config import settings
    url = settings.database_url
    return Path(url.split("///", 1)[1]) if "///" in url else Path("")


def preview(db, min_title: int, samples: int) -> dict:
    total = db.execute(text("SELECT count(*) FROM opportunities")).scalar_one()

    # Eligible = long enough titles to judge. Everything else is left alone.
    stats = db.execute(text(f"""
        SELECT
          sum(CASE WHEN long_enough THEN n - 1 ELSE 0 END) AS removable,
          sum(CASE WHEN NOT long_enough THEN n - 1 ELSE 0 END) AS protected_rows,
          sum(CASE WHEN long_enough THEN 1 ELSE 0 END)      AS clusters,
          sum(CASE WHEN NOT long_enough THEN 1 ELSE 0 END)  AS protected_clusters
        FROM (
          SELECT count(*) AS n, length(trim(title)) >= :m AS long_enough
          FROM opportunities GROUP BY {KEY} HAVING n > 1)
    """), {"m": min_title}).one()

    removable = stats.removable or 0
    protected_rows = stats.protected_rows or 0

    print("=" * 78)
    print("DUPLICATES BY TITLE  (the deadline is ignored — that is the point)")
    print("=" * 78)
    print(f"  rows in the table                     : {total:,}")
    print(f"  clusters eligible to collapse         : {stats.clusters or 0:,}")
    print(f"  rows that would be REMOVED            : {removable:,}")
    print(f"  clusters left alone (title too short) : "
          f"{stats.protected_clusters or 0:,}")
    print(f"  rows inside those, untouched          : {protected_rows:,}")
    print(f"\n  Titles shorter than {min_title} characters are never collapsed:")
    print("  \"Request for Proposals\" from one funder can be two different")
    print("  calls, and merging those destroys a lead rather than a copy.")

    rows = db.execute(text(f"""
        SELECT count(*) AS n, source_website,
               substr(title,1,58) AS title,
               min(coalesce(deadline,'-')) AS earliest,
               max(coalesce(deadline,'-')) AS latest,
               count(DISTINCT coalesce(deadline,'')) AS distinct_deadlines,
               count(DISTINCT coalesce(opportunity_url,'')) AS distinct_urls
        FROM opportunities
        GROUP BY {KEY}
        HAVING n > 1 AND length(trim(title)) >= :m
        ORDER BY n DESC LIMIT :s
    """), {"m": min_title, "s": samples}).all()

    if rows:
        print(f"\n  The {len(rows)} largest eligible clusters:")
        print(f"    {'rows':>5} {'dls':>4} {'urls':>5}  {'earliest':<11}"
              f"{'latest':<11} {'source':<20} title")
        for r in rows:
            print(f"    {r.n:>5} {r.distinct_deadlines:>4} {r.distinct_urls:>5}  "
                  f"{str(r.earliest):<11}{str(r.latest):<11} "
                  f"{(r.source_website or '')[:20]:<20} {r.title}")
        print("\n  Read the `urls` column before applying. 1 means every copy")
        print("  points at the same page, which is a re-scrape. A high number")
        print("  with a wide earliest..latest spread may be genuinely different")
        print("  calls sharing a name — check those by hand.")

    short = db.execute(text(f"""
        SELECT count(*) AS n, source_website, title
        FROM opportunities
        GROUP BY {KEY}
        HAVING n > 1 AND length(trim(title)) < :m
        ORDER BY n DESC LIMIT 10
    """), {"m": min_title}).all()
    if short:
        print(f"\n  Largest clusters being LEFT ALONE (title < {min_title} chars):")
        for r in short:
            print(f"    {r.n:>5}  {(r.source_website or '')[:20]:<20} {r.title}")

    return {"total": total, "removable": removable,
            "protected_rows": protected_rows,
            "clusters": stats.clusters or 0}


def apply(db, min_title: int) -> int:
    """Delete every row in an eligible cluster except the best one."""
    result = db.execute(text(f"""
        DELETE FROM opportunities
        WHERE id IN (
          SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY {KEY} {KEEPER}) AS rn,
                   COUNT(*) OVER (PARTITION BY {KEY}) AS n,
                   length(trim(title)) AS tlen
            FROM opportunities)
          WHERE rn > 1 AND n > 1 AND tlen >= :m)
    """), {"m": min_title})
    return result.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-title-chars", type=int, default=25,
                    help="never collapse titles shorter than this (default 25)")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument("--backup", default="",
                    help="required with --apply: where to copy the database first")
    args = ap.parse_args()

    if args.apply and not args.backup:
        print("--apply needs --backup. 'Recoverable' has to mean a file exists,\n"
              "not that somebody intended one.", file=sys.stderr)
        return 2

    with session_scope() as db:
        stats = preview(db, args.min_title_chars, args.samples)

        if not args.apply:
            print("\n" + "-" * 78)
            print(f"  Preview only. Nothing was written. {stats['removable']:,} "
                  f"row(s) would be removed.")
            print("  Re-run with --apply --backup <path> when the samples above")
            print("  look like copies rather than distinct calls.")
            return 0

        if stats["removable"] == 0:
            print("\n  Nothing to do.")
            return 0

        src = db_path()
        dest = Path(args.backup).expanduser()
        if not src.exists():
            print(f"\nCannot find the database at {src} to back it up.",
                  file=sys.stderr)
            return 2
        # Fold the write-ahead log into the main file FIRST. A WAL-mode
        # database copied without checkpointing is missing every transaction
        # still sitting in the -wal, and restores to a state that looks fine
        # until you read it.
        db.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        db.commit()
        shutil.copy2(src, dest)
        print(f"\n  Backed up to {dest} ({dest.stat().st_size/1e6:.0f} MB)")

        removed = apply(db, args.min_title_chars)
        db.commit()
        print(f"  Deleted {removed:,} row(s). "
              f"{stats['protected_rows']:,} left alone by the title guard.")
        print("\n  Then, to reclaim the space and restart:")
        print("    sqlite3 data/opportunities.db \"VACUUM;\"")
        print("    sudo supervisorctl restart lead-scanning-api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
