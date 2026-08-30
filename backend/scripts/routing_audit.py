"""What does each team member actually receive, and why?

Why this replaces my first guess
--------------------------------
`relevance_impact.py` measured the substring bug and found it cost nothing —
because **no member has any keywords set**, so the keyword filter never ran at
all. The word-boundary fix was still a real bug fix; it was not the cause of
your relevance problem.

With every routing field empty, `matches_for()` applies no filter beyond "still
open" and returns every actionable row. A digest built from ~14,800 rows is
100% noise by construction, and no scoring change alters that: there is nothing
to score against.

So this script reports the thing that actually decides relevance — each
member's routing configuration and the size of the list it produces.

    python scripts/routing_audit.py
    python scripts/routing_audit.py --show 10

Read-only. SELECTs only. Nothing is sent.
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

from sqlalchemy import func, select                            # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity, SentLog, TeamMember  # noqa: E402
from app.services.actionable import actionable_clause          # noqa: E402
from app.services.geo_routing import describe                  # noqa: E402
from app.services.matching_service import MatchingService      # noqa: E402


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def audit(show: int) -> int:
    with session_scope() as db:
        members = db.execute(
            select(TeamMember).order_by(TeamMember.name)).scalars().all()
        total_actionable = int(db.execute(
            select(func.count()).select_from(Opportunity)
            .where(actionable_clause())).scalar_one() or 0)

        print("=" * 78)
        print("ROUTING AUDIT — what each member is set up to receive")
        print("=" * 78)
        print(f"{total_actionable:,} actionable opportunities in the database.")
        print()
        print("Every routing field is 'empty = all', so an unconfigured member")
        print("matches EVERYTHING. That is the default, and for a digest it is")
        print("the same as having no filter at all.")
        print()

        svc = MatchingService(db)
        unconfigured = []

        for m in members:
            kws, cats, verts = (_csv(m.keywords), _csv(m.categories),
                                _csv(getattr(m, "verticals", "") or ""))
            sent = int(db.execute(
                select(func.count()).select_from(SentLog)
                .where(SentLog.member_id == m.id)).scalar_one() or 0)
            unsent = svc.matches_for(m)
            geo = _csv(getattr(m, "countries", "") or "") + _csv(
                getattr(m, "regions", "") or "")
            everything = not (kws or cats or verts or geo)
            if everything:
                unconfigured.append(m)

            flags = []
            if not m.active:
                flags.append("INACTIVE")
            if m.auto_send:
                flags.append("auto-send ON")
            else:
                flags.append("auto-send off")

            print(f"{m.name} <{m.email}>   [{', '.join(flags)}]")
            print(f"    keywords   : {', '.join(kws) if kws else '(none — matches all)'}")
            print(f"    categories : {', '.join(cats) if cats else '(none — matches all)'}")
            print(f"    verticals  : {', '.join(verts) if verts else '(none — matches all)'}")
            # Built outside the f-string: a multi-line expression inside one
            # is a syntax error before Python 3.12, and this venv is 3.11.
            geo_line = describe(
                _csv(getattr(m, "countries", "") or ""),
                _csv(getattr(m, "regions", "") or ""),
                bool(getattr(m, "geo_include_unknown", True)),
            )
            print(f"    geography  : {geo_line}")
            print(f"    already sent: {sent:,}     would send now: {len(unsent):,}")
            if everything:
                print("    -> NO FILTER. This member's digest is the whole database.")
            for row in unsent[:show]:
                print(f"       . {(row.title or '')[:64]}")
            print()

        # ----------------------------------------------------------- verticals
        print("-" * 78)
        print("Vertical coverage — the routing axis that IS populated")
        print("-" * 78)
        rows = db.execute(
            select(Opportunity.verticals, func.count(Opportunity.id))
            .where(actionable_clause())
            .group_by(Opportunity.verticals)
        ).all()
        untagged = sum(n for v, n in rows if not (v or "").strip())
        tagged = total_actionable - untagged
        print(f"  tagged with at least one vertical : {tagged:,} "
              f"({tagged / total_actionable * 100:.0f}%)" if total_actionable
              else "  no rows")
        print(f"  no vertical at all                : {untagged:,} "
              f"({untagged / total_actionable * 100:.0f}%)" if total_actionable
              else "")

        per_vertical: dict[str, int] = {}
        multi = 0
        for value, n in rows:
            tags = [t.strip() for t in (value or "").split(",") if t.strip()]
            if len(tags) > 1:
                multi += n
            for t in tags:
                per_vertical[t] = per_vertical.get(t, 0) + n
        print()
        for name, n in sorted(per_vertical.items(), key=lambda kv: -kv[1]):
            share = n / total_actionable * 100 if total_actionable else 0
            print(f"    {name:<34} {n:>7,}  ({share:>4.0f}%)")
        if total_actionable:
            print()
            print(f"  rows carrying MORE THAN ONE vertical: {multi:,} "
                  f"({multi / total_actionable * 100:.0f}%)")
            print("  A vertical that covers most of the database cannot route")
            print("  anything — if every row is tagged Livelihood, filtering by")
            print("  Livelihood is the same as not filtering.")

        # -------------------------------------------------------------- verdict
        print()
        print("=" * 78)
        if unconfigured:
            names = ", ".join(m.name for m in unconfigured)
            print(f"{len(unconfigured)} of {len(members)} members have NO routing set: {names}")
            print()
            print("This is the relevance problem. Nothing is being filtered for")
            print("them, so every digest is the entire actionable database ordered")
            print("by deadline. Scoring cannot help a list that was never narrowed.")
        else:
            print("Every member has at least one routing field set.")
        print()
        print("Nothing was changed by this script.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show", type=int, default=5,
                    help="example titles to print per member")
    return audit(max(0, ap.parse_args().show))


if __name__ == "__main__":
    raise SystemExit(main())
