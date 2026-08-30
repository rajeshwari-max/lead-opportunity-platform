"""How many stored deadlines were inverted by the ISO parsing bug?

The bug
-------
`dateutil` applies `dayfirst` to the last two components whatever the shape,
including ISO:

    du_parser.parse("2026-01-09", dayfirst=True).date()   ->  2026-09-01

The pipeline default is `dayfirst=True`. DevelopmentAid returns ISO dates from
its API and never sets the flag, so every one of its deadlines where the month
and day are both 12 or under has been stored eight months out.

This is the exact signature the brief flagged. The clusters at 2026-01-09,
2026-02-09 and 2026-03-09 are ISO dates 2026-09-01, 2026-09-02 and 2026-09-03
read backwards — the day looked pinned at 09 because 09 was really the month.

What this reports
-----------------
Rows whose `deadline_raw` is ISO and whose stored `deadline` does not match it.
Nothing is guessed: the raw text is the source's own words, kept precisely so a
parsing correction can be applied without re-scraping.

    python scripts/iso_inversion_audit.py
    python scripts/iso_inversion_audit.py --apply      # rewrite the deadlines

Read-only unless --apply is given.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
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

from sqlalchemy import select, update                          # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity, Status            # noqa: E402
from app.services.actionable import application_today          # noqa: E402
from app.services.deadline_parser import DeadlineParser        # noqa: E402

parser = DeadlineParser()


def audit(apply: bool, examples: int) -> int:
    today = application_today()
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity).where(
                Opportunity.deadline_raw.is_not(None),
                Opportunity.deadline_raw != "",
                Opportunity.deadline.is_not(None),
            )
        ).scalars().all()

        wrong = []
        by_source: Counter = Counter()
        for r in rows:
            correct = parser.parse(r.deadline_raw or "")
            if correct is not None and correct != r.deadline:
                wrong.append((r, correct))
                by_source[r.source_website or "(unknown)"] += 1

        print("=" * 78)
        print("ISO INVERSION AUDIT — deadlines that disagree with the source's own text")
        print("=" * 78)
        print(f"{len(rows):,} rows have raw deadline text stored.")
        print(f"{len(wrong):,} of them parse to a DIFFERENT date than what is stored.")
        print()

        if not wrong:
            print("Nothing to correct. Note that rows scraped BEFORE deadline_raw")
            print("existed have no raw text to check against — for those the only")
            print("fix is a re-scrape, and this script cannot see them.")
            print()
            print("Nothing was changed.")
            return 0

        for source, n in by_source.most_common(15):
            print(f"    {source[:44]:<46} {n:>7,}")
        print()

        print("Examples:")
        for r, correct in wrong[:examples]:
            print(f"    {(r.title or '')[:52]:<54}")
            print(f"        source says {r.deadline_raw!r:<24} "
                  f"stored {r.deadline}  ->  {correct}")

        # Counted once, over the whole set. Tallying inside the examples loop
        # as well double-counted the first N rows and reported 6 where there
        # were 3.
        reopened = closed = 0
        for r, correct in wrong:
            was_open = r.status == Status.ACTIVE
            now_open = correct >= today
            if not was_open and now_open:
                reopened += 1
            elif was_open and not now_open:
                closed += 1
        print()
        print(f"    {reopened:,} rows are currently EXPIRED but their real deadline")
        print(f"      has not passed — those are live opportunities nobody can see.")
        print(f"    {closed:,} rows are currently ACTIVE but have really closed.")
        print()

        if not apply:
            print("DRY RUN — nothing was written.")
            print("Re-run with --apply once the examples above look right.")
            return 0

        for r, correct in wrong:
            db.execute(
                update(Opportunity)
                .where(Opportunity.id == r.id)
                .values(
                    deadline=correct,
                    # The status has to move with the date, or a corrected row
                    # keeps the visibility its wrong date gave it — which is
                    # the whole harm, just with a right-looking date beside it.
                    status=Status.ACTIVE if correct >= today else Status.EXPIRED,
                    deadline_confidence="reparsed",
                )
            )
        print(f"APPLIED — {len(wrong):,} deadlines rewritten from the source's own text.")
        print("Statuses were moved with them; deadline_confidence is now 'reparsed'.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the corrected deadlines (default: dry run)")
    ap.add_argument("--examples", type=int, default=10)
    a = ap.parse_args()
    return audit(a.apply, max(0, a.examples))


if __name__ == "__main__":
    raise SystemExit(main())
