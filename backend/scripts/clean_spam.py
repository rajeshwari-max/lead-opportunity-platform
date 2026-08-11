"""Remove advertisement listings from the database.

    python scripts/clean_spam.py           # list what would go
    python scripts/clean_spam.py --apply   # delete them

New scrapes reject these before saving; this clears what was stored before that
filter existed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.spam import find_spam, purge_spam   # noqa: E402


def main() -> int:
    rows = find_spam()
    print(f"{len(rows)} advertisement listing(s) found\n")
    for _, source, title in rows:
        print(f"  [{source}] {title}")
    if not rows:
        return 0
    if "--apply" not in sys.argv:
        print("\n(preview — re-run with --apply to delete them)")
        return 0
    print(f"\nDeleted {purge_spam()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
