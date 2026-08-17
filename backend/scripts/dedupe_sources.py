"""Find and repair duplicate sources in the database.

    python scripts/dedupe_sources.py            # report only
    python scripts/dedupe_sources.py --apply

Two distinct problems, both of which make the same site appear twice:

  1. **Orphaned source names.** A source that was renamed or removed leaves its
     old rows behind under the old label, so the dashboard's Source filter
     offers a name no scraper produces any more.

  2. **Duplicate rows.** The unique_id constraint prevents exact duplicates,
     but the same opportunity found under two different source names hashes
     differently (the URL differs) and both survive.

Reports before it changes anything. Deleting rows is the user's call.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select                 # noqa: E402

import app.scrapers                                 # noqa: E402,F401  (registers)
from app.database.db import session_scope           # noqa: E402
from app.database.models import Opportunity         # noqa: E402
from app.scrapers.registry import get_scrapers      # noqa: E402

# Rows stored under the old name -> the scraper that owns the site now.
RENAMES: dict[str, str] = {
    # The JSON duplicate of indevjobs.org, which already had a purpose-built
    # scraper — the site was listed twice and crawled twice per run.
    "Indev Jobs": "IndevJobs",
    # Labels left behind when scrapers were renamed. Their rows are still good;
    # they were simply filed under a name the Source filter no longer offers,
    # so they were unreachable from the sidebar.
    "Openphilanthropy": "Coefficient Giving (Open Philanthropy)",
    "Open Philanthropy": "Coefficient Giving (Open Philanthropy)",
    "Mcknight": "McKnight Foundation",
    "Macfound": "Macarthur Foundation",
}


def main() -> int:
    apply = "--apply" in sys.argv
    known = {s.display_name for s in get_scrapers()}

    with session_scope() as db:
        counts = dict(
            db.execute(
                select(Opportunity.source_website, func.count())
                .group_by(Opportunity.source_website)
            ).all()
        )

        print(f"\nsource names in the database : {len(counts)}")
        print(f"scrapers currently registered: {len(known)}")

        orphans = {k: v for k, v in counts.items() if k and k not in known}
        print(f"\nnames with no scraper behind them: {len(orphans)}")
        for name, n in sorted(orphans.items(), key=lambda kv: -kv[1]):
            target = RENAMES.get(name)
            note = f"  -> rename to {target!r}" if target else "  (no mapping — left alone)"
            print(f"   {name:34} {n:6} rows{note}")

        # Same opportunity under two source names: identical title + deadline.
        dupes = db.execute(
            select(Opportunity.title, Opportunity.deadline, func.count(),
                   func.count(func.distinct(Opportunity.source_website)))
            .group_by(Opportunity.title, Opportunity.deadline)
            .having(func.count() > 1)
            .having(func.count(func.distinct(Opportunity.source_website)) > 1)
            .limit(200)
        ).all()
        print(f"\ntitles present under more than one source: {len(dupes)}"
              + (" (showing up to 200)" if len(dupes) == 200 else ""))
        for title, _dl, n, srcs in dupes[:10]:
            print(f"   x{n} across {srcs} sources: {str(title)[:66]}")

        if not apply:
            print("\n(report only — re-run with --apply to perform the renames)")
            return 0

        renamed = 0
        for old, new in RENAMES.items():
            rows = db.execute(
                select(Opportunity).where(Opportunity.source_website == old)
            ).scalars().all()
            for r in rows:
                r.source_website = new
                renamed += 1
        print(f"\nRenamed {renamed} rows.")
        print("Cross-source duplicates are reported, never deleted — the same call"
              "\nlisted on two boards is genuinely two leads with two links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
