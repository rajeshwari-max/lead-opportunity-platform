"""Give stored leads the brand their CMS vertical implies.

    python scripts/apply_brand_mapping.py            # counts only
    python scripts/apply_brand_mapping.py --apply    # write

New leads get it at classification time (services/brands.BRAND_FROM_VERTICAL:
Health -> Swasti, Livelihood -> Vrutti, Climate/Sustainability(ESG) -> Green
Foundation, Worker Wellbeing -> Upfront). This brings the leads already stored
into line. It only ADDS a brand; it never removes one, and it leaves alone any
lead whose labels a person set by hand.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                    # noqa: E402

from app.database.db import session_scope                        # noqa: E402
from app.database.models import Opportunity                      # noqa: E402
from app.services.actionable import strict_actionable_clause     # noqa: E402
from app.services.brands import (BRAND_FROM_VERTICAL,            # noqa: E402
                                 brands_from_verticals, brands_to_str)


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def apply_mapping(db, write: bool) -> Counter:
    added: Counter = Counter()
    wanted = list(BRAND_FROM_VERTICAL)
    rows = db.execute(select(Opportunity).where(
        Opportunity.verticals.is_not(None), Opportunity.verticals != "")).scalars()
    for o in rows:
        if "human" in ((o.classification_source or ""), (o.verticals_source or "")):
            continue
        verticals = [v for v in _split(o.verticals) if v in wanted]
        if not verticals:
            continue
        have = _split(o.brands)
        new = [b for b in brands_from_verticals(verticals) if b not in have]
        if not new:
            continue
        for b in new:
            added[b] += 1
        if write:
            o.brands = brands_to_str(have + new)
    return added


def _live_counts(db) -> Counter:
    counts: Counter = Counter()
    for brands in db.execute(select(Opportunity.brands)
                             .where(strict_actionable_clause())).scalars():
        counts.update(_split(brands))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with session_scope() as db:
        before = _live_counts(db)
        added = apply_mapping(db, args.apply)
    print("Brand added from the lead's CMS vertical (all stored leads):")
    for brand in ("Swasti", "Vrutti", "Green Foundation", "Upfront"):
        print(f"  {brand:18} +{added.get(brand, 0)}")
    if args.apply:
        with session_scope() as db:
            after = _live_counts(db)
        print("\nLeads users can see, per brand (before -> after):")
        for brand in ("Swasti", "Vrutti", "Green Foundation", "Upfront", "Setu",
                      "Community Action Collab"):
            print(f"  {brand:24} {before.get(brand, 0):5} -> {after.get(brand, 0)}")
    else:
        print("\n(preview — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
