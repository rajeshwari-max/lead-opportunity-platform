"""How much noise did the word-boundary fix remove? Measure it on real rows.

Why this script exists
----------------------
The substring bug is demonstrable on twelve invented titles. That is enough to
prove it is a bug; it is not enough to say what it cost YOU. This runs both
matchers — the old substring test and the new whole-word one — over the real
database and reports, per team member, how many of the rows they would have
been emailed do not actually contain their keyword.

It changes nothing. Read-only, SELECTs only.

    python scripts/relevance_impact.py
    python scripts/relevance_impact.py --member someone@example.org
    python scripts/relevance_impact.py --examples 5

What to look at
---------------
`false positives` is the number of emails that were noise. If it is near zero
for everyone, the substring bug was not your relevance problem and the next
place to look is the vertical classifier — run `scripts/label_relevance.py`
to start building the labelled set that question needs.
"""
from __future__ import annotations

import argparse
import sys
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
from app.database.models import Opportunity, TeamMember        # noqa: E402
from app.services.actionable import actionable_clause          # noqa: E402
from app.services.relevance import (                           # noqa: E402
    MIN_SCORE,
    compile_keywords,
    score_opportunity,
)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _old_matcher(keywords: list[str], row) -> list[str]:
    """The rule as it was: case-insensitive substring, any field, any hit."""
    haystack = " ".join(filter(None, [
        row.title or "", row.summary or "", row.vertical or "",
        row.eligibility or "",
    ])).lower()
    return [kw for kw in keywords if kw.lower() in haystack]


def audit(member_email: str, examples: int) -> int:
    with session_scope() as db:
        members = db.execute(select(TeamMember).order_by(TeamMember.name)).scalars().all()
        if member_email:
            members = [m for m in members
                       if (m.email or "").lower() == member_email.lower()]
            if not members:
                print(f"No team member with email {member_email!r}.", file=sys.stderr)
                return 2
        rows = db.execute(select(Opportunity).where(actionable_clause())).scalars().all()

        print("=" * 78)
        print("EMAIL RELEVANCE — old substring filter vs whole-word matching")
        print("=" * 78)
        print(f"{len(rows):,} actionable opportunities in scope.")
        print()
        print("'false positives' are rows the old filter would have emailed where")
        print("the keyword only appears INSIDE another word — ict in District,")
        print("ai in Maintenance. Every one of those was a wrong email.")
        print()

        grand_old = grand_new = grand_bogus = 0

        for m in members:
            keywords = _csv(m.keywords)
            if not keywords:
                print(f"{m.name} <{m.email}>")
                print("    no keywords set — receives everything; nothing to measure")
                print()
                continue

            compiled = compile_keywords(keywords)
            old_hits, new_hits, bogus = [], [], []
            for row in rows:
                old_kw = _old_matcher(keywords, row)
                match = score_opportunity(
                    compiled, title=row.title or "", summary=row.summary or "",
                    vertical=row.vertical or "", eligibility=row.eligibility or "")
                if old_kw:
                    old_hits.append(row)
                if match.is_match:
                    new_hits.append(row)
                # A row the old filter took where no keyword is genuinely
                # present as a word. Not merely "scored below threshold" —
                # those are a judgement call about strength, and lumping them
                # in here would overstate the bug.
                if old_kw and not match.matched_keywords:
                    bogus.append((row, old_kw))

            grand_old += len(old_hits)
            grand_new += len(new_hits)
            grand_bogus += len(bogus)
            share = (len(bogus) / len(old_hits) * 100) if old_hits else 0.0

            print(f"{m.name} <{m.email}>")
            print(f"    keywords: {', '.join(keywords)}")
            print(f"    old filter matched {len(old_hits):,}   "
                  f"whole-word matches {len(new_hits):,}   "
                  f"false positives {len(bogus):,} ({share:.0f}% of the old list)")
            for row, kws in bogus[:examples]:
                print(f"      x  [{', '.join(kws)}] {(row.title or '')[:60]}")
            print()

        print("=" * 78)
        print(f"Across everyone: {grand_bogus:,} of {grand_old:,} matches the old "
              f"filter made were words-inside-words.")
        if grand_old:
            print(f"That is {grand_bogus / grand_old * 100:.0f}% of everything "
                  f"that was being emailed.")
        print()
        print(f"Rows now need a score of {MIN_SCORE} — one title hit, or two hits")
        print("elsewhere. A single mention in a long summary no longer qualifies,")
        print("which is why 'whole-word matches' can be below 'old filter matched'")
        print("by more than the false positives alone.")
        print()
        print("Nothing was changed by this script.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--member", default="", help="limit to one member's email")
    ap.add_argument("--examples", type=int, default=3,
                    help="false-positive examples to print per member")
    args = ap.parse_args()
    return audit(args.member, max(0, args.examples))


if __name__ == "__main__":
    raise SystemExit(main())
