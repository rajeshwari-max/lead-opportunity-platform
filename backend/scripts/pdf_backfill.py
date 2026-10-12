"""Look for the source's own PDF on opportunities not checked yet.

    python scripts/pdf_backfill.py                 # up to 400 rows, 15 minutes
    python scripts/pdf_backfill.py --limit 2000 --budget 3600

The same job runs automatically after every scrape; use this once after
deploying to cover the opportunities that were already live. It only writes
the three pdf_* columns, never touches anything else, and can be re-run: rows
already checked are skipped. Plain HTTP only — no login, no browser, and a
bot check is recorded as "blocked", not retried.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--budget", type=float, default=900.0, help="seconds")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--include-archived", action="store_true", help="Also inspect expired and archived records")
    ap.add_argument("--retry-missing", action="store_true", help="Recheck one batch of records without a source PDF")
    ap.add_argument("--all", action="store_true", help="Process unchecked records in resumable batches until exhausted")
    args = ap.parse_args()
    if args.all and args.retry_missing:
        ap.error("Use --retry-missing separately; --all processes unchecked records only")
    if args.limit < 1 or args.budget <= 0 or args.concurrency < 1:
        ap.error("limit, budget and concurrency must be positive")

    import app.scrapers  # noqa: F401  (registry: which sources need a login)
    from app.database.db import init_db
    from app.services.opportunity_pdf import discover_pdfs

    init_db()   # adds the pdf_* columns if this database predates them
    while True:
        result = discover_pdfs(args.limit, args.concurrency, args.budget,
                              include_archived=args.include_archived,
                              retry_missing=args.retry_missing)
        print(json.dumps(result, indent=1), flush=True)
        if not args.all or not result["unchecked_left_for_next_run"]:
            break
        if not any(result.get(status, 0) for status in ("found", "none", "blocked", "error", "skipped")):
            print("No progress in this batch; rerun later.", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
