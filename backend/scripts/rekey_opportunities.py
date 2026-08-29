"""Re-key opportunities onto the deadline-free unique_id. DRY RUN BY DEFAULT.

Why this script has to exist
----------------------------
`make_unique_id` used to include the deadline. When a source corrected or
extended a closing date, every identifying field stayed the same but the hash
moved, so the same call was stored again — two rows, two deadlines, no way to
tell which was current.

The fix removes the deadline from the key. That changes the key for all 106,854
existing rows, which means the two changes are one change: deploy the new
function without this backfill and the next scrape sees an empty database and
re-inserts everything.

Why it merges rather than deletes
---------------------------------
Rows that differed ONLY by deadline collapse onto one key. That is the point —
they were always the same opportunity. But collapsing them is a destructive
operation on real data, so:

  * nothing is deleted; superseded rows are marked Expired and keep a note
    saying which row they merged into
  * the survivor of each group is the most recently seen row, and it inherits
    approval from any group member that had it — losing a human's approval
    because of a key change would be indefensible
  * it does nothing at all unless you pass --apply

Usage
-----
    python scripts/rekey_opportunities.py              # report only
    python scripts/rekey_opportunities.py --apply      # after reading the report

Run with the backend STOPPED. It takes an exclusive write lock at the end.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# This script imports the application, so it needs the project's virtual
# environment. Run on the system interpreter it dies with a bare
# "ModuleNotFoundError: No module named 'sqlalchemy'" six frames deep, which
# says nothing about what to do. db_baseline.py works either way because it uses
# only the standard library — so "the last script ran fine" is not evidence the
# environment is right.
try:
    import sqlalchemy                                          # noqa: F401
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    # Built outside the f-string: a backslash inside an f-string expression is a
    # syntax error before Python 3.12, and this file must at least PARSE on an
    # older interpreter to be able to print this message at all.
    _activate = (".venv\\Scripts\\activate" if sys.platform == "win32"
                 else "source .venv/bin/activate")
    _name = Path(__file__).name
    print(
        "This needs the project's virtual environment, and it is not active.\n"
        f"\n  Running under : {sys.executable}\n"
        f"  Expected      : the interpreter inside {_root / '.venv'}\n"
        "\nActivate it first, then re-run:\n"
        f"\n    cd {_root}\n"
        f"    {_activate}\n"
        f"    python scripts/{_name}\n"
        "\nYour prompt should show (.venv) before you run it.",
        file=sys.stderr,
    )
    raise SystemExit(2)

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity, Status            # noqa: E402
from app.services.deduplication import make_unique_id          # noqa: E402


def analyse() -> tuple[dict[str, list], dict]:
    """Group every row by its NEW key. No writes."""
    groups: dict[str, list] = defaultdict(list)
    stats = {"rows": 0, "no_link": 0}
    with session_scope() as db:
        rows = db.execute(
            # Only the columns the decision needs — the full ORM objects for
            # 106k rows would be gratuitous memory on a small box.
            __import__("sqlalchemy").select(
                Opportunity.id, Opportunity.unique_id, Opportunity.title,
                Opportunity.organization, Opportunity.deadline,
                Opportunity.opportunity_url, Opportunity.last_seen,
                Opportunity.approved, Opportunity.status,
                Opportunity.source_website,
            )
        ).all()
    for r in rows:
        stats["rows"] += 1
        if not (r.opportunity_url or "").strip():
            stats["no_link"] += 1
        key = make_unique_id(r.title, r.organization, r.deadline, r.opportunity_url)
        groups[key].append(r)
    return groups, stats


def report(groups: dict[str, list], stats: dict) -> dict:
    collisions = {k: v for k, v in groups.items() if len(v) > 1}
    merged_away = sum(len(v) - 1 for v in collisions.values())
    approved_at_risk = sum(
        1 for v in collisions.values()
        if any(r.approved for r in v) and not max(v, key=lambda r: r.last_seen).approved
    )

    print("=" * 74)
    print("RE-KEY ANALYSIS — no changes made")
    print("=" * 74)
    print(f"  rows examined                    {stats['rows']:>10,}")
    print(f"  distinct keys after re-keying    {len(groups):>10,}")
    print(f"  groups that collapse             {len(collisions):>10,}")
    print(f"  rows superseded (archived)       {merged_away:>10,}")
    print(f"  rows with no link (weaker key)   {stats['no_link']:>10,}")
    print(f"  groups where approval must be")
    print(f"    carried to the survivor        {approved_at_risk:>10,}")
    print()

    if collisions:
        print("Largest groups (these are the same call stored repeatedly):")
        for key, rows in sorted(collisions.items(), key=lambda kv: -len(kv[1]))[:10]:
            rows_sorted = sorted(rows, key=lambda r: r.last_seen, reverse=True)
            keep = rows_sorted[0]
            print(f"  {len(rows):>3} rows  keep id={keep.id}  {keep.title[:58]!r}")
            for r in rows_sorted[1:4]:
                print(f"           merge id={r.id}  deadline={r.deadline}  "
                      f"approved={r.approved}")
            if len(rows_sorted) > 4:
                print(f"           …and {len(rows_sorted) - 4} more")
    else:
        print("No collisions — the new key changes ids but merges nothing.")
    print()
    return {"collisions": len(collisions), "merged_away": merged_away}


def apply(groups: dict[str, list]) -> dict:
    """Write the new keys and archive superseded rows."""
    now = datetime.now(timezone.utc)
    rekeyed = archived = approval_carried = 0

    with session_scope() as db:
        for key, rows in groups.items():
            survivor = max(rows, key=lambda r: r.last_seen)
            group_approved = any(r.approved for r in rows)

            keep = db.get(Opportunity, survivor.id)
            if keep is None:
                continue
            keep.unique_id = key
            rekeyed += 1
            # Approval is a human act. A key change must never discard one.
            if group_approved and not keep.approved:
                keep.approved = True
                approval_carried += 1

            for other in rows:
                if other.id == survivor.id:
                    continue
                row = db.get(Opportunity, other.id)
                if row is None:
                    continue
                # Not deleted — archived, with a pointer to what replaced it, so
                # the merge is reversible and auditable.
                row.status = Status.EXPIRED
                row.unique_id = f"merged:{other.id}:{key[:16]}"
                note = f"[merged into #{survivor.id} on {now:%Y-%m-%d}]"
                row.summary = f"{note} {row.summary or ''}"[:8000]
                archived += 1

    return {"rekeyed": rekeyed, "archived": archived,
            "approval_carried": approval_carried}


def inspect(groups: dict[str, list], row_id: int) -> int:
    """Show every row that would merge with this one, and why.

    A merge is only correct if the rows really are one opportunity. The key
    falls back to title+organization when there is no link, and it trusts
    `opportunity_url` to be a DETAIL url — if a source stores its listing or
    search url on every row instead, that whole source collapses to one record.
    That is the failure this exists to catch, and it is not visible from counts
    alone: 86 rows merging looks identical whether it is one notice scraped 86
    times or 86 notices sharing a bad url.
    """
    target = None
    for key, rows in groups.items():
        if any(r.id == row_id for r in rows):
            target = (key, rows)
            break
    if target is None:
        print(f"No row with id={row_id}.", file=sys.stderr)
        return 1

    key, rows = target
    rows = sorted(rows, key=lambda r: r.last_seen, reverse=True)
    urls = {(r.opportunity_url or "").strip() for r in rows}
    deadlines = {r.deadline for r in rows}

    print("=" * 74)
    print(f"GROUP for id={row_id}  ({len(rows)} rows would become 1)")
    print("=" * 74)
    print(f"  new key            {key[:32]}…")
    print(f"  distinct urls      {len(urls)}")
    print(f"  distinct deadlines {len(deadlines)}")
    print(f"  keyed on           {'URL' if any(urls - {''}) else 'title+organization'}")
    print()

    if len(urls) == 1 and urls != {""}:
        url = next(iter(urls))
        print(f"  All {len(rows)} rows share one url:")
        print(f"    {url}")
        print()
        print("  Read that url. If it opens ONE specific opportunity, every row")
        print("  here is the same notice re-scraped and the merge is correct.")
        print("  If it opens a LISTING or SEARCH page, this source is storing a")
        print("  listing url on every row — do NOT apply; that source needs its")
        print("  detail-link extraction fixed first.")
    elif urls == {""}:
        print("  No urls at all — merged on title+organization, which is the")
        print("  weaker key. Two genuinely different calls a funder gave the same")
        print("  name would merge here. Check the rows below are one call.")
    print()

    print(f"  {'id':>8}  {'deadline':<12} {'last seen':<20} {'source':<24} title")
    print(f"  {'-'*8}  {'-'*12} {'-'*20} {'-'*24} {'-'*30}")
    for i, r in enumerate(rows[:30]):
        mark = "KEEP" if i == 0 else "  ->"
        print(f"  {mark:>4}{r.id:>4}  {str(r.deadline):<12} "
              f"{str(r.last_seen)[:19]:<20} "
              f"{(r.source_website or '')[:24]:<24} {r.title[:44]}")
    if len(rows) > 30:
        print(f"  …and {len(rows) - 30} more")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, this reports and exits.")
    ap.add_argument("--inspect", type=int, metavar="ID",
                    help="show every row that would merge with this row id, "
                         "and whether the merge looks right")
    args = ap.parse_args()

    groups, stats = analyse()

    if args.inspect is not None:
        return inspect(groups, args.inspect)

    summary = report(groups, stats)

    if not args.apply:
        print("DRY RUN — nothing was written.")
        print("Re-run with --apply once the numbers above look right.")
        return 0

    if summary["merged_away"]:
        print(f"Applying: {summary['merged_away']:,} row(s) will be archived "
              f"(not deleted) and their survivors re-keyed.")
    result = apply(groups)
    print()
    print("=" * 74)
    print(f"  re-keyed                {result['rekeyed']:>10,}")
    print(f"  archived as superseded  {result['archived']:>10,}")
    print(f"  approval carried over   {result['approval_carried']:>10,}")
    print("=" * 74)
    print("Nothing was deleted. Superseded rows are Expired and note their survivor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
