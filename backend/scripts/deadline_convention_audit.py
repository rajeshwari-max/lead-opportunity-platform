"""Is a source's day/month convention wrong? Answer from its own data.

The suspicion
-------------
The logs showed deadline clusters at 2026-01-09, 2026-02-09 and 2026-03-09 —
the DAY pinned at 09 while the month walks. That is the fingerprint of day/month
inversion: if a source writes 09/01/2026 meaning 1 September and the parser is
told dayfirst, it reads 9 January. Real deadlines from a source publishing
across a season produce the opposite shape — the month clusters and the day
spreads.

Why a script rather than a fix
------------------------------
Changing a source's date convention rewrites the meaning of every stored
deadline for it. Doing that on a hunch is how a whole source's dates end up
wrong in the other direction. So this measures the distribution and states what
it implies; changing `deadline_format` in the manifest is a separate, deliberate
act.

    python scripts/deadline_convention_audit.py
    python scripts/deadline_convention_audit.py --source DevelopmentAid

Read-only. SELECTs only.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import sqlalchemy                                          # noqa: F401
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    _act = (".venv\\Scripts\\activate" if sys.platform == "win32"
            else "source .venv/bin/activate")
    print(f"Needs the project venv.\n\n    cd {_root}\n    {_act}\n"
          f"    python scripts/{Path(__file__).name}\n", file=sys.stderr)
    raise SystemExit(2)

from sqlalchemy import select                                  # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity                    # noqa: E402

# A date is only AMBIGUOUS when both parts could be a month. 31/07 cannot be
# month-first; 09/01 can be either. Only ambiguous dates carry the signal, and
# mixing unambiguous ones in dilutes it to nothing.
AMBIGUOUS_MAX = 12


def audit(source: str | None) -> int:
    with session_scope() as db:
        stmt = select(Opportunity.source_website, Opportunity.deadline,
                      Opportunity.title).where(Opportunity.deadline.is_not(None))
        if source:
            stmt = stmt.where(Opportunity.source_website == source)
        rows = db.execute(stmt).all()

    by_source: dict[str, list] = defaultdict(list)
    for r in rows:
        by_source[r.source_website].append(r.deadline)

    print("=" * 78)
    print("DEADLINE CONVENTION AUDIT — read-only")
    print("=" * 78)
    print("A source publishing across a season should show the DAY spread wide")
    print("and the MONTH clustered. The reverse — one day value dominating while")
    print("months walk — is the fingerprint of day/month inversion.")
    print()

    flagged = 0
    for src, dates in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        if len(dates) < 25:
            continue                       # too few to say anything
        ambiguous = [d for d in dates if d.day <= AMBIGUOUS_MAX]
        if len(ambiguous) < 15:
            continue

        days = Counter(d.day for d in ambiguous)
        months = Counter(d.month for d in ambiguous)
        top_day, top_day_n = days.most_common(1)[0]
        day_share = top_day_n / len(ambiguous)
        month_spread = len(months)
        day_spread = len(days)

        # Inversion looks like: one day value dominating, across many months.
        suspicious = day_share >= 0.35 and month_spread >= 4 and day_spread <= 6

        mark = "  ** SUSPICIOUS **" if suspicious else ""
        if suspicious:
            flagged += 1
        print(f"{src[:44]:<46} {len(dates):>6,} dated  "
              f"{len(ambiguous):>5,} ambiguous{mark}")
        print(f"    day  {top_day:>2} appears in {day_share:5.1%} of ambiguous dates "
              f"({day_spread} distinct days)")
        print(f"    months present: {month_spread}   "
              f"{sorted(months)}")
        if suspicious:
            print(f"    -> reads as day/month INVERSION. If this source writes "
                  f"MM/DD, every")
            print(f"       ambiguous date is wrong: what it calls "
                  f"{top_day:02d}/MM is being read")
            print(f"       as day {top_day}. Check a handful against the live site "
                  f"before changing")
            print(f"       deadline_format in services/source_manifest.py.")
        print()

    print("=" * 78)
    if flagged:
        print(f"{flagged} source(s) flagged. Nothing was changed — this only measures.")
    else:
        print("No source shows the inversion signature.")
    print("Raw deadline text is now stored in opportunities.deadline_raw, so a")
    print("confirmed inversion can be re-parsed rather than re-scraped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", help="limit to one source_website")
    return audit(ap.parse_args().source)


if __name__ == "__main__":
    raise SystemExit(main())
