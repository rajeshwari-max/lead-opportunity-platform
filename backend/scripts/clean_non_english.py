"""Delete opportunities whose title is not in a Latin script.

    python scripts/clean_non_english.py           # list them
    python scripts/clean_non_english.py --apply   # delete them

These are real opportunities, not spam — UNDP grant competitions in Russian, a
Tunisian call for local associations, Moldovan small-grants programmes. Deleting
them is a deliberate editorial choice: the team works in English and cannot act
on a call it cannot read.

The database is backed up first, because unlike the "English only" display
toggle this cannot be undone.
"""
from __future__ import annotations

import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select                      # noqa: E402

from app.core.config import settings                       # noqa: E402
from app.database.db import session_scope                  # noqa: E402
from app.database.models import Opportunity                # noqa: E402
from app.services.spam import _mostly_non_latin            # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    with session_scope() as db:
        rows = [
            (o.id, o.source_website, (o.title or "")[:70])
            for o in db.execute(select(Opportunity)).scalars()
            if _mostly_non_latin(o.title or "")
        ]

    print(f"{len(rows)} non-English opportunit(ies)\n")
    for source, n in Counter(r[1] for r in rows).most_common():
        print(f"  {n:5}  {source}")
    print("\nexamples:")
    for _, source, title in rows[:8]:
        print(f"  [{source[:20]:20}] {title}")

    if not rows:
        return 0
    if not apply:
        print("\n(preview — re-run with --apply to delete them)")
        return 0

    db_path = Path(settings.database_url.split("///")[-1])
    backup = db_path.with_name(f"{db_path.stem}.before-nonenglish-{datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(db_path, backup)
    print(f"\nbackup: {backup}")

    ids = [r[0] for r in rows]
    with session_scope() as db:
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            db.execute(delete(Opportunity).where(Opportunity.id.in_(chunk)))
    print(f"Deleted {len(ids)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
