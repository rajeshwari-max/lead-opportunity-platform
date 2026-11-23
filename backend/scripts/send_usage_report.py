"""Weekly usage report, by hand (the Monday job runs the same thing).

    python scripts/send_usage_report.py --preview   # print it and save the Excel sheet; send nothing
    python scripts/send_usage_report.py             # email it to the REVIEWER for approval

The report never goes straight to the recipient: it is emailed to
LOP_WEEKLY_USAGE_REPORT_REVIEWER with a "Review and approve" link, and only the
"Yes, send it" button on that page sends it to LOP_WEEKLY_USAGE_REPORT_RECIPIENT.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings                              # noqa: E402
from app.services import weekly_usage_report as report            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", action="store_true", help="print the report; send nothing")
    args = ap.parse_args()
    if args.preview:
        snapshot = report.lead_views_snapshot()
        print(report.render_summary_text(snapshot))
        report.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = report.REPORTS_DIR / f"preview-{report.report_filename(snapshot)}"
        out.write_bytes(report.render_xlsx(snapshot))
        print(f"\nExcel sheet saved (not sent): {out}")
        return 0
    result = report.send_weekly_usage_report()
    if result.get("review_sent"):
        print(f"Sent to the reviewer for approval ({result['reviewer']}). "
              f"It goes to {result['recipient']} only after you press 'Yes, send it'.")
    elif result.get("sent"):
        print(f"Sent to {result['recipient']} (approval is switched off).")
    else:
        print("Nothing was sent. Check in backend/.env: "
              "LOP_WEEKLY_USAGE_REPORT_RECIPIENT, LOP_WEEKLY_USAGE_REPORT_REVIEWER and the SMTP settings."
              f" (recipient set: {bool(settings.weekly_usage_report_recipient.strip())},"
              f" reviewer set: {bool(settings.weekly_usage_report_reviewer.strip())})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
