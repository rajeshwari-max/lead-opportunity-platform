"""Read-only check of what users can see: wrong dates, duplicates, closed calls.

    python scripts/lead_accuracy_audit.py            # counts + 3 examples each
    python scripts/lead_accuracy_audit.py --samples 10

Looks only at leads the ordinary dashboard shows (strict_actionable_clause:
Active, a real closing date, not passed). Changes nothing.

Checks, each one a defect that has actually reached the dashboard:

  shared_source_date   one source, one closing date, on most of its live
                       leads. Until 2026-11-17 every UNDP notice carried the
                       closing date of the notice at the top of the page.
  date_disagrees_raw   the stored date is not what the source's own date text
                       says when read again (day/month swap, old parser).
  far_future           closes more than three years from today.
  run_together_title   a title that is several table cells glued together
                       ("... Ref No ... Deadline ...").
  duplicate_title      the same title from the same funder shown more than once.
  passed_but_active    status Active with a date that has passed. Hidden by
                       the dashboard query, archived by the nightly audit;
                       listed so a stalled audit is visible.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                  # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity, Status            # noqa: E402
from app.services.actionable import (application_today,        # noqa: E402
                                     strict_actionable_clause)
from app.services.deadline_parser import DeadlineParser        # noqa: E402

_GLUED = re.compile(r"\b(Ref\.?\s*No|Deadline|Posted|Closing date)\b.*\d", re.I)
_NORM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORM.sub(" ", (text or "").casefold()).strip()


def audit(db, today=None) -> dict[str, list[tuple]]:
    today = today or application_today()
    rows = db.execute(
        select(Opportunity.id, Opportunity.source_website, Opportunity.title,
               Opportunity.organization, Opportunity.deadline,
               Opportunity.deadline_raw, Opportunity.deadline_convention,
               Opportunity.deadline_confidence)
        .where(strict_actionable_clause(today))
    ).all()
    found: dict[str, list[tuple]] = defaultdict(list)
    parser = DeadlineParser()

    by_source: dict[str, list] = defaultdict(list)
    for r in rows:
        by_source[r.source_website].append(r)
    for source, items in by_source.items():
        dates = defaultdict(list)
        for r in items:
            dates[r.deadline].append(r)
        day, same = max(dates.items(), key=lambda kv: len(kv[1]))
        if len(same) >= 25 and len(same) >= 0.5 * len(items):
            found["shared_source_date"].append(
                (source, f"{len(same)} of {len(items)} live leads close on {day}"))

    for r in rows:
        raw = (r.deadline_raw or "").strip()
        if raw and (r.deadline_confidence or "") != "human":
            again = parser.parse(raw, dayfirst=(r.deadline_convention != "monthfirst"))
            if again is not None and again != r.deadline:
                found["date_disagrees_raw"].append(
                    (r.id, r.source_website, f"stored {r.deadline}, text {raw!r} reads {again}",
                     (r.title or "")[:70]))
        if r.deadline and r.deadline > today + timedelta(days=3 * 365):
            found["far_future"].append((r.id, r.source_website, str(r.deadline),
                                        (r.title or "")[:70]))
        if _GLUED.search(r.title or ""):
            found["run_together_title"].append((r.id, r.source_website, (r.title or "")[:110]))

    groups = defaultdict(list)
    for r in rows:
        key = (_norm(r.title), _norm(r.organization))
        if key[0]:
            groups[key].append(r)
    for (title, _), items in groups.items():
        if len(items) > 1:
            found["duplicate_title"].append(
                (len(items), ", ".join(sorted({i.source_website for i in items}))[:80],
                 ", ".join(str(i.deadline) for i in items)[:60], title[:70]))

    passed = db.execute(
        select(Opportunity.id, Opportunity.source_website, Opportunity.deadline)
        .where(Opportunity.status == Status.ACTIVE, Opportunity.deadline < today)
    ).all()
    for r in passed:
        found["passed_but_active"].append((r.id, r.source_website, str(r.deadline)))
    found["_visible"] = [(len(rows),)]
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()
    with session_scope() as db:
        found = audit(db)
    print(f"Leads users can see now: {found.pop('_visible')[0][0]:,}")
    names = ("shared_source_date", "date_disagrees_raw", "far_future",
             "run_together_title", "duplicate_title", "passed_but_active")
    problems = 0
    for name in names:
        items = found.get(name, [])
        problems += len(items)
        print(f"\n{name}: {len(items)}")
        for item in items[:max(0, args.samples)]:
            print("   ", " | ".join(str(x) for x in item))
    print("\nNo problems found." if not problems else
          "\nRead-only: nothing was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
