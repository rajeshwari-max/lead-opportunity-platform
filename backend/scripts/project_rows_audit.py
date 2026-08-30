"""How many stored World Bank / ADB rows are projects rather than notices?

The fix only helps future scrapes
---------------------------------
Two faults let project records in, and both are now closed:

  * no scraper populated `record_type`, so the manifests excluding
    `contract_award` and `project` could never fire;
  * World Bank's title chain fell back to `project_name`, so a record with no
    bid description was titled with the project it belongs to.

Rows already in the database were stored before either fix. This finds them
from what WAS captured — the summary text, which carries "Notice type: X" for
both sources, and the shape of the title.

    python scripts/project_rows_audit.py
    python scripts/project_rows_audit.py --source "World Bank"
    python scripts/project_rows_audit.py --archive       # quarantine them

`--archive` sets status=Expired. It never deletes: the brief is explicit that
invalid rows are archived or quarantined unless deletion is separately
approved, and a row wrongly archived can be brought back while a deleted one
cannot.

Read-only unless --archive is given.
"""
from __future__ import annotations

import argparse
import re
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
from app.services.notice_types import record_type_for          # noqa: E402
from app.services.source_manifest import (                     # noqa: E402
    RecordType, contract_for, record_is_in_scope,
)

# Both scrapers write "Notice type: <value> | ..." into the summary. That is
# the source's own word, captured at scrape time — the most reliable evidence
# available for a row stored before record_type existed.
_NOTICE_IN_SUMMARY = re.compile(r"Notice type:\s*([^|]+)", re.IGNORECASE)

# Sources whose manifests actually exclude projects and awards. Others are left
# alone: judging a row against a contract that does not exist would be the
# guessing this whole mechanism replaced.
SOURCES = {"World Bank": "world_bank", "ADB Tenders": "adb_tenders"}


def audit(only_source: str, archive: bool, examples: int) -> int:
    with session_scope() as db:
        names = [only_source] if only_source else list(SOURCES)
        print("=" * 78)
        print("PROJECT-ROW AUDIT — rows stored before the scrapers passed their type")
        print("=" * 78)

        grand = []
        for display in names:
            key = SOURCES.get(display)
            if key is None:
                print(f"{display!r} has no contract excluding projects; skipping.",
                      file=sys.stderr)
                continue
            contract = contract_for(key)
            rows = db.execute(
                select(Opportunity).where(
                    Opportunity.source_website == display,
                    Opportunity.status == Status.ACTIVE,
                )
            ).scalars().all()

            out_of_scope = []
            reasons: Counter = Counter()
            unlabelled = 0
            for r in rows:
                m = _NOTICE_IN_SUMMARY.search(r.summary or "")
                notice = (m.group(1).strip() if m else "")
                if not notice:
                    unlabelled += 1
                    continue
                rt = record_type_for(notice)
                if not rt:
                    continue
                keep, why = record_is_in_scope(contract, record_type=rt,
                                               source_status=notice)
                if not keep:
                    out_of_scope.append((r, notice, rt))
                    reasons[f"{notice.strip()} -> {rt}"] += 1

            print()
            print(f"{display}: {len(rows):,} Active rows")
            print(f"    {unlabelled:,} carry no 'Notice type:' in their summary — "
                  f"scraped before that was recorded, so this cannot judge them.")
            print(f"    {len(out_of_scope):,} are out of scope by the source's own word.")
            for label, n in reasons.most_common(10):
                print(f"        {label:<46} {n:>7,}")
            for r, notice, rt in out_of_scope[:examples]:
                print(f"          . [{notice.strip()}] {(r.title or '')[:52]}")
            grand.extend(out_of_scope)

        print()
        print("-" * 78)
        print(f"{len(grand):,} Active rows would be archived.")
        if not archive:
            print()
            print("DRY RUN — nothing was written.")
            print("Re-run with --archive once the reasons above look right.")
            return 0

        if not grand:
            print("Nothing to archive.")
            return 0

        ids = [r.id for r, _, _ in grand]
        db.execute(
            update(Opportunity).where(Opportunity.id.in_(ids))
            .values(status=Status.EXPIRED)
        )
        print(f"ARCHIVED — {len(ids):,} rows moved to Expired. None were deleted;")
        print("they remain in the archive view and can be restored.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default="", help="one source_website value")
    ap.add_argument("--archive", action="store_true",
                    help="quarantine the out-of-scope rows (default: dry run)")
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()
    return audit(a.source, a.archive, max(0, a.examples))


if __name__ == "__main__":
    raise SystemExit(main())
