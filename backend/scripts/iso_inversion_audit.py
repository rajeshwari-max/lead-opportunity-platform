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
    python scripts/iso_inversion_audit.py --apply --backup data/backups/x.db

Read-only unless --apply is given; --apply requires a backup path and never
deletes rows. Status moves with the corrected date (passed -> Expired, still
open -> Active), so a row with a wrong future date stops showing as Active.

Each row is re-read with the date convention it was stored with
(`deadline_convention`: dayfirst / monthfirst). Rows with no recorded
convention are corrected only when the text is unambiguous — both readings
agree, e.g. ISO dates or a day above 12. Re-reading a US source's "09/01/2026"
day-first would "correct" a right date into a wrong one. Sentinel and
implausibly distant results (see deadline_audit.is_sentinel) are never written.
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
from app.services.deadline_audit import is_sentinel            # noqa: E402
from app.services.deadline_parser import DeadlineParser        # noqa: E402

parser = DeadlineParser()


def corrected_deadline(raw: str, convention: str | None):
    """The date the source's own text means, or None when it cannot be told.

    Uses the row's recorded convention; with none recorded, only a reading
    that is the same day-first and month-first is trusted.
    """
    if not raw or not raw.strip():
        return None
    if convention == "monthfirst":
        value = parser.parse(raw, dayfirst=False)
    elif convention == "dayfirst":
        value = parser.parse(raw, dayfirst=True)
    else:
        a, b = parser.parse(raw, dayfirst=True), parser.parse(raw, dayfirst=False)
        value = a if a == b else None
    if value is None or is_sentinel(value):
        return None
    return value


def audit(apply: bool, examples: int, backup: str = "") -> int:
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
            if (r.unique_id or "").startswith("merged:"):
                continue
            correct = corrected_deadline(r.deadline_raw or "",
                                         getattr(r, "deadline_convention", None))
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

        from scripts.active_deadline_audit import backup_database
        print(f"Backup: {backup_database(Path(backup))[0]}")
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
    ap.add_argument("--backup", default="",
                    help="required with --apply: path for a database backup")
    ap.add_argument("--examples", type=int, default=10)
    a = ap.parse_args()
    if a.apply and not a.backup:
        ap.error("--apply requires --backup PATH; no production write without recovery")
    return audit(a.apply, max(0, a.examples), a.backup)


if __name__ == "__main__":
    raise SystemExit(main())
