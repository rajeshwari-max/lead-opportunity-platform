"""Remove a website's own files (e.g. DevelopmentAid's footer ISO certificate)
from opportunities' stored "official source documents".

These were stored when a DevelopmentAid detail page was read before it had
rendered, so the only PDF on the page was the site footer's quality
certificate. Rows are never deleted; only documents_json and the PDF fields
change. Dry-run by default; --apply requires --backup.

    python scripts/clean_site_chrome_documents.py
    python scripts/clean_site_chrome_documents.py --apply --backup data/backups/x.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select  # noqa: E402

from app.database.db import SessionLocal  # noqa: E402
from app.database.models import Opportunity  # noqa: E402
from app.services.site_chrome import is_site_chrome_document  # noqa: E402


def plan(session) -> list[tuple[Opportunity, list[dict], str | None]]:
    """(row, documents to keep, pdf_url to keep) for every affected row."""
    rows = session.execute(select(Opportunity).where(or_(
        Opportunity.documents_json.is_not(None), Opportunity.pdf_url.is_not(None),
    ))).scalars()
    out = []
    for row in rows:
        docs_dirty = is_site_chrome_document(row.documents_json or "")
        pdf_dirty = is_site_chrome_document(row.pdf_url or "")
        if not (docs_dirty or pdf_dirty):
            continue
        try:
            docs = json.loads(row.documents_json) if row.documents_json else []
        except (ValueError, TypeError):
            docs = []
        kept = [d for d in docs if isinstance(d, dict) and not is_site_chrome_document(d)]
        pdf = None if pdf_dirty else row.pdf_url
        pdf = pdf or next((d.get("url") for d in kept
                           if ".pdf" in str(d.get("url", "")).casefold()), None)
        out.append((row, kept, pdf))
    return out


def apply(changes) -> int:
    for row, kept, pdf in changes:
        # Empty list + unchecked: the next DevelopmentAid run re-reads the page.
        row.documents_json = json.dumps(kept, ensure_ascii=False) if kept else None
        row.pdf_url = pdf
        if not pdf:
            row.pdf_status = None
            row.pdf_checked_at = None
    return len(changes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default="")
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()
    if a.apply and not a.backup:
        ap.error("--apply requires --backup PATH")
    with SessionLocal() as session:
        changes = plan(session)
        print(f"{len(changes):,} opportunity row(s) carry the website's own files "
              f"as official documents.")
        for row, kept, pdf in changes[:max(0, a.examples)]:
            print(f"  - {(row.title or '')[:70]}  ({row.source_website}) -> "
                  f"{len(kept)} real document(s) kept")
        if not changes or not a.apply:
            print("Nothing written." if not changes else "DRY RUN — nothing written.")
            return 0
        from scripts.active_deadline_audit import backup_database
        print(f"Backup: {backup_database(Path(a.backup))[0]}")
        apply(changes)
        session.commit()
        print(f"Cleaned {len(changes):,} row(s). No row was deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
