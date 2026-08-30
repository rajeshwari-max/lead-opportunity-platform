"""What would the pruned classifier change? Show it before it happens.

The next backend restart runs `backfill_verticals()`, which re-tags every
machine-classified row against the current rules. Two rules changed:

  1. Service-line terms (Research, Evaluation, Training & Capacity Building,
     Consultancy) no longer tag a SECTOR — except E4C, whose identity is that
     kind of work.
  2. A spreadsheet term already owned by another vertical no longer also tags
     this one. Twelve of the eighteen terms in the sheet's Livelihood row were
     already matched by E4C, Climate, Health, Worker Wellbeing or Innovative
     Finance.

That is a large change to make sight-unseen, so this shows it first: how many
rows gain or lose each vertical, and real examples of both.

Read-only. Nothing is written; the change lands at the next restart.

    python scripts/reclassify_preview.py
    python scripts/reclassify_preview.py --sample 0        # whole database
    python scripts/reclassify_preview.py --vertical Health
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
from app.services.actionable import actionable_clause          # noqa: E402
from app.services.verticals import (                           # noqa: E402
    VERTICALS,
    classify_verticals,
    is_human_labeled,
)


def preview(sample: int, only: str, examples: int,
            compare_scoring: bool = False) -> int:
    with session_scope() as db:
        stmt = select(Opportunity).where(actionable_clause())
        if sample:
            stmt = stmt.order_by(Opportunity.date_scraped.desc()).limit(sample)
        rows = db.execute(stmt).scalars().all()

        gained: Counter = Counter()
        lost: Counter = Counter()
        examples_lost: dict[str, list[str]] = defaultdict(list)
        examples_gained: dict[str, list[str]] = defaultdict(list)
        now_untagged = became_untagged = protected = 0
        before_multi = after_multi = 0

        for r in rows:
            if is_human_labeled(r):
                protected += 1
                continue
            old = {t.strip() for t in (r.verticals or "").split(",") if t.strip()}
            body = " ".join(filter(None, [r.summary, r.vertical, r.eligibility]))
            new = set(classify_verticals(r.title or "", body))

            if len(old) > 1:
                before_multi += 1
            if len(new) > 1:
                after_multi += 1
            if not new:
                now_untagged += 1
                if old:
                    became_untagged += 1

            for v in old - new:
                lost[v] += 1
                if len(examples_lost[v]) < examples:
                    examples_lost[v].append(r.title or "")
            for v in new - old:
                gained[v] += 1
                if len(examples_gained[v]) < examples:
                    examples_gained[v].append(r.title or "")

        total = len(rows) or 1
        print("=" * 78)
        print("RECLASSIFY PREVIEW — what the next restart would change")
        print("=" * 78)
        print(f"{len(rows):,} actionable rows examined"
              f"{' (sampled, newest first)' if sample else ' (all)'}.")
        if protected:
            print(f"{protected:,} human-labelled rows skipped — the backfill "
                  f"leaves those alone.")
        print()

        for v in VERTICALS:
            if only and v != only:
                continue
            g, l = gained[v], lost[v]
            if not (g or l):
                print(f"  {v:<32} unchanged")
                continue
            print(f"  {v}")
            print(f"      loses {l:>6,}   gains {g:>6,}   net "
                  f"{g - l:>+7,}")
            for t in examples_lost[v][:examples]:
                print(f"        - {t[:64]}")
            for t in examples_gained[v][:examples]:
                print(f"        + {t[:64]}")
            print()

        print("-" * 78)
        print(f"rows with MORE THAN ONE vertical: {before_multi:,} "
              f"({before_multi / total * 100:.0f}%) -> {after_multi:,} "
              f"({after_multi / total * 100:.0f}%)")
        print(f"rows with NO vertical after the change: {now_untagged:,} "
              f"({now_untagged / total * 100:.0f}%)")
        print(f"   of which {became_untagged:,} have a vertical today and would lose it")
        print()
        if compare_scoring:
            print("-" * 78)
            print("SPAN SCORING — a second, separate change, currently OFF")
            print("-" * 78)
            print("The threshold is documented as 'a title hit, or 2+ body hits'.")
            print("It has not meant that for a long time: general and specific")
            print("patterns overlap, so 'health system strengthening' matches both")
            print(r"\bhealth and health\s+system and scores 2 from one phrase.")
            print("Counting distinct matched TEXT instead makes two hits mean two")
            print("pieces of text. Enable with LOP_VERTICAL_SPAN_SCORING=true.")
            print()
            span_counts: Counter = Counter()
            span_untagged = 0
            differs = 0
            span_examples: list[str] = []
            for r in rows:
                if is_human_labeled(r):
                    continue
                body = " ".join(filter(None, [r.summary, r.vertical, r.eligibility]))
                a = set(classify_verticals(r.title or "", body))
                b = set(classify_verticals(r.title or "", body, span_scoring=True))
                if a != b:
                    differs += 1
                    if len(span_examples) < examples:
                        span_examples.append(
                            f"{(r.title or '')[:52]}  {sorted(a)} -> {sorted(b)}")
                for v in b:
                    span_counts[v] += 1
                if not b:
                    span_untagged += 1
            for v in VERTICALS:
                print(f"    {v:<32} {span_counts[v]:>6,}")
            print(f"    {'no vertical':<32} {span_untagged:>6,} "
                  f"({span_untagged / total * 100:.0f}%)")
            print(f"\n    {differs:,} rows ({differs / total * 100:.0f}%) would be "
                  f"tagged differently again under span scoring.")
            for e in span_examples:
                print(f"      {e}")
            print()

        print("A row that loses every vertical is not deleted or hidden — the")
        print("dashboard's 'has_vertical' filter defaults to ON, so it stops")
        print("appearing in the working view until something tags it. Check the")
        print("'-' examples above: if they are rows you would want to see, the")
        print("pruning went too far and the terms should go back.")
        print()
        print("Nothing was changed by this script.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", type=int, default=4000,
                    help="rows to examine, newest first (0 = all)")
    ap.add_argument("--vertical", default="", help="show one vertical only")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--compare-scoring", action="store_true",
                    help="also show what span scoring would do")
    a = ap.parse_args()
    return preview(max(0, a.sample), a.vertical, max(0, a.examples),
                   a.compare_scoring)


if __name__ == "__main__":
    raise SystemExit(main())
