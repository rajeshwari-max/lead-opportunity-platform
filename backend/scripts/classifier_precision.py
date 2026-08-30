"""Why is E4C on 30% of the database, and where does digest noise come from?

What the routing audit showed
-----------------------------
Members DO have routing set, so "no filter" was not the problem either. What
they receive is filtered by category and vertical and is still noisy, and the
example titles say why:

    Banyule Environment Grants Round - Individuals (Australia)
    Call for Binn Wind Turbine Community Fund (United Kingdom)
    Applications open for Festive Fund Grants (Australia)

Local council micro-grants, in other countries, for individuals rather than
organisations — reaching an India-based consultancy because nothing filters on
geography, on who may apply, or on whether a vertical tag was well-founded.

This script measures the three:

  1. GEOGRAPHY   where the actionable rows actually are
  2. EVIDENCE    which keyword pattern caused each vertical tag, and how often
                 it was the ONLY reason
  3. OVERLAP     how much of the database each vertical claims

A pattern that is the sole reason for thousands of tags is the one to look at
first. "Evaluation" and "Research" appear in every consultancy RFP ever
written, so a rule that tags on them tags everything — and a vertical that
covers a third of the database cannot route anything.

Read-only. SELECTs only. Nothing is changed.

    python scripts/classifier_precision.py
    python scripts/classifier_precision.py --vertical "E4C(Evidence for Change)"
    python scripts/classifier_precision.py --sample 4000
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

from sqlalchemy import func, select                            # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity                    # noqa: E402
from app.services.actionable import actionable_clause          # noqa: E402
from app.services.verticals import VERTICALS, explain_verticals  # noqa: E402


def report(only_vertical: str, sample: int, examples: int) -> int:
    with session_scope() as db:
        stmt = select(Opportunity).where(actionable_clause())
        if sample:
            # Newest first: a sample of the oldest rows measures a classifier
            # and a source mix that may no longer be what runs today.
            stmt = stmt.order_by(Opportunity.date_scraped.desc()).limit(sample)
        rows = db.execute(stmt).scalars().all()

        print("=" * 78)
        print("CLASSIFIER PRECISION — what is driving the vertical tags")
        print("=" * 78)
        print(f"{len(rows):,} actionable rows examined"
              f"{' (sampled, newest first)' if sample else ''}.")
        print()

        # ------------------------------------------------------- 1. geography
        print("-" * 78)
        print("1. GEOGRAPHY — where these opportunities are")
        print("-" * 78)
        countries = Counter((r.country or "").strip() or "(blank)" for r in rows)
        for name, n in countries.most_common(15):
            print(f"    {name[:40]:<42} {n:>7,}  ({n / len(rows) * 100:>4.1f}%)")
        blank = countries.get("(blank)", 0)
        print()
        print(f"    {blank:,} rows ({blank / len(rows) * 100:.0f}%) have no country at all,")
        print("    so a geographic filter cannot see them either way. That is a")
        print("    separate problem from the filter not existing.")
        print()
        print("    TeamMember has no country or region field — geography is a")
        print("    dashboard filter only, and the digest ignores it entirely.")
        print()

        # -------------------------------------------------------- 2. evidence
        print("-" * 78)
        print("2. EVIDENCE — which keyword caused each vertical tag")
        print("-" * 78)
        print("'sole reason' counts rows where that pattern was the ONLY one")
        print("matching for that vertical. Those tags stand or fall on it alone.")
        print()

        targets = [only_vertical] if only_vertical else VERTICALS
        sole_examples: dict[tuple[str, str], list[str]] = {}

        for vertical in targets:
            if vertical not in VERTICALS:
                print(f"Unknown vertical {vertical!r}. Known: {VERTICALS}",
                      file=sys.stderr)
                return 2
            fired: Counter = Counter()
            sole: Counter = Counter()
            tagged = 0
            for r in rows:
                body = " ".join(filter(None, [r.summary, r.vertical, r.eligibility]))
                why = explain_verticals(r.title or "", body)
                pats = why.get(vertical)
                if not pats:
                    continue
                tagged += 1
                for p in pats:
                    fired[p] += 1
                if len(pats) == 1:
                    sole[pats[0]] += 1
                    key = (vertical, pats[0])
                    if len(sole_examples.setdefault(key, [])) < examples:
                        sole_examples[key].append(r.title or "")

            share = tagged / len(rows) * 100 if rows else 0
            print(f"  {vertical}  —  {tagged:,} rows ({share:.0f}% of everything)")
            if not tagged:
                print("      never assigned in this sample")
                print()
                continue
            for pat, n in fired.most_common(8):
                s = sole[pat]
                flag = ""
                if s and s / tagged >= 0.15:
                    flag = "   <-- carries this tag on its own"
                print(f"      {pat[:44]:<46} fired {n:>6,}   sole reason {s:>6,}{flag}")
            for pat, n in sole.most_common(2):
                for title in sole_examples.get((vertical, pat), [])[:examples]:
                    print(f"          [{pat}] {title[:58]}")
            print()

        # --------------------------------------------------------- 3. overlap
        print("-" * 78)
        print("3. OVERLAP — can these verticals route anything?")
        print("-" * 78)
        counts: Counter = Counter()
        multi = 0
        untagged = 0
        for r in rows:
            tags = [t.strip() for t in (r.verticals or "").split(",") if t.strip()]
            if not tags:
                untagged += 1
            if len(tags) > 1:
                multi += 1
            for t in tags:
                counts[t] += 1
        for name, n in counts.most_common():
            print(f"    {name:<36} {n:>7,}  ({n / len(rows) * 100:>4.0f}%)")
        print()
        print(f"    no vertical            {untagged:>7,}  ({untagged / len(rows) * 100:>4.0f}%)")
        print(f"    more than one          {multi:>7,}  ({multi / len(rows) * 100:>4.0f}%)")
        print()
        print("    A vertical on a third of the database narrows a digest by a")
        print("    third. That is a labelling problem, not a ranking one — and")
        print("    it is fixed by removing the patterns above that fire on")
        print("    everything, not by adding a model on top of them.")
        print()
        print("Nothing was changed by this script.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vertical", default="", help="examine one vertical only")
    ap.add_argument("--sample", type=int, default=4000,
                    help="rows to examine, newest first (0 = all)")
    ap.add_argument("--examples", type=int, default=2,
                    help="example titles per sole-reason pattern")
    a = ap.parse_args()
    return report(a.vertical, max(0, a.sample), max(0, a.examples))


if __name__ == "__main__":
    raise SystemExit(main())
