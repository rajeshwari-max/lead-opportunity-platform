"""Two questions about the live database, answered from the real rules.

    python scripts/db_health.py                  # both sections, read-only
    python scripts/db_health.py --duplicates     # just the duplicate report
    python scripts/db_health.py --deadlines      # just the deadline report
    python scripts/db_health.py --json health.json

Nothing is written. Nothing is deleted. This only reports.

Exit code
---------
0 when no expired row can reach a user, non-zero when one can. That makes it
usable as a check after a deploy or on a schedule, rather than something
somebody has to read and interpret every time.

Why it imports the application's own clause
-------------------------------------------
The deadline half asks "can an expired row reach the dashboard?", and the only
honest way to answer that is with the SAME SQL the dashboard runs. A script
that re-implements the rule tests its own copy of it, agrees with itself
forever, and goes on agreeing on the day the two drift apart — which is
precisely when you needed it to disagree.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402

from app.database.db import session_scope  # noqa: E402
from app.database.models import Opportunity, Status  # noqa: E402

# The grouping that defines "the same opportunity".
#
# opportunity_url is deliberately NOT in it. 86 DevNetJobsIndia rows in this
# database share the single URL rfp_assignments.aspx — the postback-link defect
# — and they are 86 genuinely different RFPs, not 86 copies of one. Keying on
# the URL would delete 85 real opportunities and leave one.
#
# Two rows are the same call only if the title, the funder, the source and the
# closing date all agree.
DUP_KEY = """
    lower(trim(title)),
    lower(trim(coalesce(organization,''))),
    lower(trim(coalesce(source_website,''))),
    coalesce(deadline,'')
"""


def ist_today():
    from app.services.actionable import application_today
    return application_today()


def visible_clause():
    """The clause the dashboard actually uses, whichever version is deployed.

    Returns (clause, name). `strict_actionable_clause` only exists after this
    round's changes land, so the script reports which rule it measured rather
    than assuming — a number is not interpretable without knowing which
    question it answers.
    """
    from app.services import actionable
    if hasattr(actionable, "strict_actionable_clause"):
        return actionable.strict_actionable_clause(), "strict_actionable_clause"
    return actionable.actionable_clause(), "actionable_clause (pre-update)"


# ----------------------------------------------------------------- duplicates

def duplicates(db) -> dict:
    total = db.execute(select(func.count(Opportunity.id))).scalar_one()
    groups = db.execute(text(
        f"SELECT count(*) FROM (SELECT 1 FROM opportunities GROUP BY {DUP_KEY})"
    )).scalar_one()
    dup_rows = total - groups

    clusters = db.execute(text(f"""
        SELECT count(*) AS n, source_website, substr(title,1,70) AS title,
               coalesce(deadline,'(none)') AS deadline, min(id) AS keep_id
        FROM opportunities
        GROUP BY {DUP_KEY}
        HAVING n > 1
        ORDER BY n DESC
        LIMIT 15
    """)).all()

    by_source = db.execute(text(f"""
        SELECT source_website, sum(n - 1) AS extra_rows
        FROM (SELECT count(*) AS n, source_website FROM opportunities
              GROUP BY {DUP_KEY})
        WHERE n > 1
        GROUP BY source_website
        ORDER BY extra_rows DESC
        LIMIT 15
    """)).all()

    # Reported SEPARATELY and never folded into the number above, because these
    # are usually not duplicates at all — see the note on DUP_KEY.
    shared_urls = db.execute(text("""
        SELECT count(*) FROM (
            SELECT opportunity_url FROM opportunities
            WHERE opportunity_url IS NOT NULL AND trim(opportunity_url) <> ''
            GROUP BY opportunity_url HAVING count(*) > 1)
    """)).scalar_one()
    worst_url = db.execute(text("""
        SELECT count(*) AS n, count(DISTINCT lower(trim(title))) AS distinct_titles,
               opportunity_url
        FROM opportunities
        WHERE opportunity_url IS NOT NULL AND trim(opportunity_url) <> ''
        GROUP BY opportunity_url HAVING n > 1
        ORDER BY n DESC LIMIT 5
    """)).all()

    print("=" * 78)
    print("DUPLICATES")
    print("=" * 78)
    print(f"  rows in the table          : {total:,}")
    print(f"  distinct opportunities     : {groups:,}")
    print(f"  duplicate rows             : {dup_rows:,}"
          + (f"   ({100.0*dup_rows/total:.1f}% of the table)" if total else ""))
    print("\n  Definition: same title + funder + source + closing date.")
    print("  The URL is deliberately excluded — see below.")

    if clusters:
        print(f"\n  The {len(clusters)} largest clusters:")
        print(f"    {'copies':>6}  {'keep id':>8}  {'deadline':<12} "
              f"{'source':<22} title")
        for c in clusters:
            print(f"    {c.n:>6}  {c.keep_id:>8}  {str(c.deadline):<12} "
                  f"{(c.source_website or '')[:22]:<22} {c.title}")
    else:
        print("\n  No duplicates under this definition.")

    if by_source:
        print("\n  Extra rows by source:")
        for s in by_source:
            print(f"    {s.extra_rows:>7,}  {s.source_website}")

    print(f"\n  SEPARATELY — URLs shared by more than one row : {shared_urls:,}")
    print("  These are NOT counted as duplicates above, and mostly are not")
    print("  duplicates. `distinct_titles` is the tell: a high number means one")
    print("  broken link shared by many different calls, which is a link defect")
    print("  to repair, not rows to delete.")
    if worst_url:
        print(f"\n    {'rows':>5}  {'distinct titles':>15}  url")
        for u in worst_url:
            flag = "  <-- different calls, one link" if u.distinct_titles > 1 else ""
            print(f"    {u.n:>5}  {u.distinct_titles:>15}  "
                  f"{(u.opportunity_url or '')[:60]}{flag}")

    return {
        "rows": total, "distinct": groups, "duplicate_rows": dup_rows,
        "duplicate_pct": round(100.0 * dup_rows / total, 2) if total else 0.0,
        "largest_clusters": [
            {"copies": c.n, "keep_id": c.keep_id, "source": c.source_website,
             "title": c.title, "deadline": str(c.deadline)} for c in clusters],
        "extra_rows_by_source": {s.source_website: s.extra_rows for s in by_source},
        "urls_shared_by_more_than_one_row": shared_urls,
    }


# ------------------------------------------------------------------ deadlines

def deadlines(db) -> tuple[dict, bool]:
    today = ist_today()
    clause, clause_name = visible_clause()

    def count(*where):
        stmt = select(func.count(Opportunity.id))
        for w in where:
            stmt = stmt.where(w)
        return db.execute(stmt).scalar_one()

    active = Opportunity.status == Status.ACTIVE
    buckets = {
        "1. Active, deadline IS NULL":
            count(active, Opportunity.deadline.is_(None)),
        "2. Active, deadline text present but unparsed":
            count(active, Opportunity.deadline.is_(None),
                  Opportunity.deadline_raw.is_not(None),
                  Opportunity.deadline_raw != ""),
        "3. Active, rolling / open-ended":
            count(active, Opportunity.deadline_state == "rolling"),
        "4. Active, unknown / unassessed":
            count(active, Opportunity.deadline_state == "unknown"),
        "5. Active, deadline BEFORE today (IST)":
            count(active, Opportunity.deadline.is_not(None),
                  Opportunity.deadline < today),
        "6. Active, deadline IS today (IST)":
            count(active, Opportunity.deadline == today),
    }

    # THE question. Not "how many Active rows are past their date" — that is
    # bucket 5 and it is survivable, because the list query filters them out.
    # This asks whether one can still REACH a user through the rule the
    # dashboard actually applies.
    leaking = db.execute(
        select(func.count(Opportunity.id))
        .where(clause)
        .where(Opportunity.deadline.is_not(None))
        .where(Opportunity.deadline < today)
    ).scalar_one()
    undated_visible = db.execute(
        select(func.count(Opportunity.id))
        .where(clause).where(Opportunity.deadline.is_(None))
    ).scalar_one()
    visible = db.execute(select(func.count(Opportunity.id)).where(clause)).scalar_one()

    print("\n" + "=" * 78)
    print(f"DEADLINES     today = {today} (Asia/Kolkata)")
    print("=" * 78)
    for label, n in buckets.items():
        print(f"  {label:<48} {n:>9,}")
    print("\n  The buckets overlap on purpose — a row can be both undated and")
    print("  rolling — so they do not sum to a total. Each answers a different")
    print("  question, and forcing a partition would mean picking which one")
    print("  matters.")

    by_source = db.execute(
        select(Opportunity.source_website, func.count(Opportunity.id))
        .where(active, Opportunity.deadline.is_not(None),
               Opportunity.deadline < today)
        .group_by(Opportunity.source_website)
        .order_by(func.count(Opportunity.id).desc()).limit(15)
    ).all()
    if by_source:
        print("\n  Bucket 5 (past deadline, still marked Active) by source:")
        for name, n in by_source:
            print(f"    {n:>7,}  {name}")

    samples = db.execute(
        select(Opportunity.id, Opportunity.title, Opportunity.source_website,
               Opportunity.status, Opportunity.deadline_state, Opportunity.deadline)
        .where(active, Opportunity.deadline.is_not(None),
               Opportunity.deadline < today)
        .order_by(Opportunity.deadline.desc()).limit(8)
    ).all()
    if samples:
        print("\n  Samples:")
        for r in samples:
            print(f"    #{r.id:<8} {str(r.deadline):<12} "
                  f"{(r.deadline_state or '-'):<9} "
                  f"{(r.source_website or '')[:20]:<20} {(r.title or '')[:44]}")

    print("\n" + "-" * 78)
    print(f"  Rule the dashboard applies : {clause_name}")
    print(f"  Rows a user can see        : {visible:,}")
    print(f"  ...of those, PAST DEADLINE : {leaking:,}")
    print(f"  ...of those, no deadline   : {undated_visible:,}")

    ok = leaking == 0
    if ok:
        print("\n  PASS — no expired opportunity can reach the dashboard.")
    else:
        print(f"\n  FAIL — {leaking:,} expired row(s) are reachable by a user.")
        print("  Fix: python scripts/active_deadline_audit.py --apply --backup <path>")
    if undated_visible:
        print(f"\n  NOTE — {undated_visible:,} visible row(s) carry no deadline at")
        print("  all. Under the strict rule those should be 0; if this is not 0")
        print("  and the rule above says 'pre-update', this round's change has")
        print("  not been deployed yet.")

    return ({"today_ist": str(today), "rule": clause_name, "buckets": buckets,
             "visible": visible, "visible_past_deadline": leaking,
             "visible_without_deadline": undated_visible,
             "past_deadline_by_source": {n: c for n, c in by_source}}, ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--duplicates", action="store_true", help="only this section")
    ap.add_argument("--deadlines", action="store_true", help="only this section")
    ap.add_argument("--json", default="", help="also write the numbers here")
    args = ap.parse_args()

    both = not (args.duplicates or args.deadlines)
    out: dict = {}
    ok = True
    with session_scope() as db:
        if both or args.duplicates:
            out["duplicates"] = duplicates(db)
        if both or args.deadlines:
            out["deadlines"], ok = deadlines(db)

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
