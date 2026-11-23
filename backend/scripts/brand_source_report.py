"""Which source websites give leads for which brand. Read-only.

    python scripts/brand_source_report.py                  # live leads (dashboard)
    python scripts/brand_source_report.py --all            # every stored lead
    python scripts/brand_source_report.py --csv brands.csv # also save a CSV

One row per source website: how many leads it has, how many count for CMS
(any CMS vertical) and how many for each brand. Sources that gave leads but
none for any brand are listed at the end, so they can be checked or dropped.
Changes nothing.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402

from app.database.db import session_scope                        # noqa: E402
from app.database.models import Opportunity                      # noqa: E402
from app.services.actionable import strict_actionable_clause     # noqa: E402

BRANDS = ("Swasti", "Vrutti", "Green Foundation", "Upfront", "Setu",
          "Community Action Collab")
SHORT = {"Green Foundation": "GreenF", "Community Action Collab": "CAC"}


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def collect(db, live_only: bool = True) -> dict[str, Counter]:
    stmt = select(Opportunity.source_website, Opportunity.verticals,
                  Opportunity.archetypes, Opportunity.brands)
    if live_only:
        stmt = stmt.where(strict_actionable_clause())
    by_source: dict[str, Counter] = defaultdict(Counter)
    for source, verticals, archetypes, brands in db.execute(stmt).all():
        c = by_source[source or "(unknown)"]
        c["leads"] += 1
        if _split(verticals) or "Social Business" in (archetypes or ""):
            c["CMS"] += 1
        for brand in _split(brands):
            if brand in BRANDS:
                c[brand] += 1
    return by_source


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="every stored lead, not only live ones")
    ap.add_argument("--csv", default="", help="also write the table to this CSV file")
    args = ap.parse_args()
    with session_scope() as db:
        data = collect(db, live_only=not args.all)

    cols = ["leads", "CMS", *BRANDS]
    order = sorted(data, key=lambda s: (-sum(data[s][b] for b in BRANDS), -data[s]["leads"]))
    head = f"{'Source':34} " + " ".join(f"{SHORT.get(c, c):>7}" for c in cols)
    print(("Live leads" if not args.all else "All stored leads") + " per source and brand\n")
    print(head)
    print("-" * len(head))
    totals = Counter()
    for source in order:
        c = data[source]
        totals.update(c)
        print(f"{source[:34]:34} " + " ".join(f"{c[col]:>7}" for col in cols))
    print("-" * len(head))
    print(f"{'TOTAL':34} " + " ".join(f"{totals[col]:>7}" for col in cols))

    none = [s for s in order if data[s]["leads"] and not any(data[s][b] for b in BRANDS)]
    print(f"\n{len(none)} source(s) gave leads but none for any brand:")
    for s in none:
        print(f"  {s} ({data[s]['leads']} leads, {data[s]['CMS']} CMS)")
    for brand in BRANDS:
        givers = [s for s in order if data[s][brand]]
        print(f"\n{brand}: {totals[brand]} lead(s) from {len(givers)} source(s)"
              + (": " + ", ".join(f"{s} {data[s][brand]}" for s in givers[:8]) if givers else ""))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["source", *cols])
            for source in order:
                w.writerow([source, *(data[source][c] for c in cols)])
        print(f"\nCSV written: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
