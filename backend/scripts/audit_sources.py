"""Health check across every source, from what is actually in the database.

    python scripts/audit_sources.py                 # report
    python scripts/audit_sources.py --source "Clean Air Fund"
    python scripts/audit_sources.py --purge-junk    # delete flagged junk rows

Checking 85 websites by hand is not practical, and eyeballing the dashboard only
finds what happens to be on the first page. This works from the stored rows and
flags the four failure modes that have actually occurred:

  1. JUNK      — the title is site navigation, not a funding call
                 ("Our work", "Insights", "Procurement Policy")
  2. EXPIRED   — status says Active but the deadline has passed
  3. NO LINK   — nothing clickable
  4. NO SIGNAL — no deadline, no amount, no vertical: probably not a real call

A source where most rows are flagged is a source whose parser has drifted, which
is what you want to know *before* the team reports it.

Deletes nothing unless --purge-junk is given, and prints what it would delete
first either way.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                       # noqa: E402

from app.database.db import session_scope           # noqa: E402
from app.database.models import Opportunity, Status  # noqa: E402
from app.scrapers.generic_listing import looks_like_funding  # noqa: E402

# Titles that are navigation on almost any site. Deliberately conservative:
# a false positive here deletes a real lead.
_JUNK_TITLE = re.compile(
    r"^(our\s+work|our\s+funding|what\s+we\s+(do|fund)|who\s+we\s+are|"
    r"insights?|resources?|publications?|reports?|news|blog|events?|"
    r"about(\s+us)?|contact(\s+us)?|home|search|menu|"
    r"funded\s+projects?|case\s+studies|our\s+(team|people|partners|approach)|"
    r"privacy|terms|cookies?|sitemap|accessibility|newsletter|subscribe|"
    r"skip\s+to|read\s+more|learn\s+more|find\s+out\s+more|view\s+all|see\s+all|"
    r"procurement(\s+policy|\s+notices?)?|tenders?|opportunit(y|ies)|grants?|"
    r"donate|careers?|jobs?|press|media|login|sign\s+in)\s*$",
    re.IGNORECASE,
)


def classify_row(o: Opportunity, today: date) -> list[str]:
    flags = []
    title = (o.title or "").strip()

    if _JUNK_TITLE.match(title) or len(title) < 12:
        flags.append("JUNK")
    elif not looks_like_funding(title, o.opportunity_url or "",
                                " ".join([o.summary or "", o.funding_amount or ""])):
        flags.append("NO-SIGNAL")

    if o.status == Status.ACTIVE and o.deadline and o.deadline < today:
        flags.append("EXPIRED")

    if not (o.opportunity_url or "").strip():
        flags.append("NO-LINK")

    return flags


def main() -> int:
    args = sys.argv[1:]
    purge = "--purge-junk" in args
    only = None
    if "--source" in args:
        only = args[args.index("--source") + 1]

    today = date.today()
    per_source: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "JUNK": 0, "EXPIRED": 0, "NO-LINK": 0,
                 "NO-SIGNAL": 0, "samples": []}
    )
    junk_ids: list[int] = []

    with session_scope() as db:
        stmt = select(Opportunity).where(Opportunity.status == Status.ACTIVE)
        if only:
            stmt = stmt.where(Opportunity.source_website == only)

        for o in db.execute(stmt).scalars():
            s = per_source[o.source_website]
            s["total"] += 1
            for f in classify_row(o, today):
                s[f] += 1
                if f == "JUNK":
                    junk_ids.append(o.id)
                if len(s["samples"]) < 3 and f in ("JUNK", "EXPIRED"):
                    s["samples"].append(f"[{f}] {(o.title or '')[:56]!r}"
                                        + (f" (deadline {o.deadline})" if o.deadline else ""))

        rows = sorted(per_source.items(),
                      key=lambda kv: -(kv[1]["JUNK"] + kv[1]["EXPIRED"]))

        print(f"\n{'SOURCE':34} {'ACTIVE':>7} {'JUNK':>6} {'EXPIRED':>8} "
              f"{'NO-LINK':>8} {'NO-SIG':>7}  HEALTH")
        print("-" * 92)
        broken = []
        for name, s in rows:
            bad = s["JUNK"] + s["NO-SIGNAL"]
            ratio = bad / s["total"] if s["total"] else 0
            if s["total"] and ratio > 0.6:
                health, mark = "PARSER DRIFTED", "!!"
                broken.append(name)
            elif s["JUNK"]:
                health, mark = "junk present", " ·"
            elif s["EXPIRED"]:
                health, mark = "stale statuses", " ·"
            else:
                health, mark = "ok", "  "
            print(f"{mark}{str(name)[:32]:32} {s['total']:>7} {s['JUNK']:>6} "
                  f"{s['EXPIRED']:>8} {s['NO-LINK']:>8} {s['NO-SIGNAL']:>7}  {health}")

        print("\n" + "=" * 92)
        tot = sum(s["total"] for _, s in rows)
        print(f"active rows      : {tot}")
        print(f"junk titles      : {sum(s['JUNK'] for _, s in rows)}")
        print(f"expired-but-active: {sum(s['EXPIRED'] for _, s in rows)}")
        print(f"no link          : {sum(s['NO-LINK'] for _, s in rows)}")
        print(f"no funding signal: {sum(s['NO-SIGNAL'] for _, s in rows)}")

        if broken:
            print(f"\nSources whose parser looks broken (>60% unusable): {len(broken)}")
            for b in broken:
                print(f"   {b}")
                for smp in per_source[b]["samples"]:
                    print(f"      {smp}")

        if junk_ids and not purge:
            print(f"\n{len(junk_ids)} row(s) flagged JUNK. Re-run with --purge-junk "
                  f"to delete them.")
        elif junk_ids and purge:
            for oid in junk_ids:
                obj = db.get(Opportunity, oid)
                if obj is not None:
                    db.delete(obj)
            print(f"\nDeleted {len(junk_ids)} junk row(s).")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
