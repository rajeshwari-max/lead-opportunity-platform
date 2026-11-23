"""Review page for the weekly usage report (see services/weekly_usage_report).

    GET  /api/usage-report/review/{token}         shows the report and a "Yes, send it" button
    GET  /api/usage-report/review/{token}/excel   downloads the Excel sheet (sends nothing)
    POST /api/usage-report/review/{token}         sends it to the recipient (once)

The link is emailed to the reviewer (the admin). It is exempt from the login
gate in main.py, like the digest approval links: the HMAC-signed token names
one stored report and expires, so holding the link is the authorisation.
Opening the page (GET) never sends anything, so a mail scanner that pre-fetches
links cannot approve the report; only the button (POST) does.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.services import weekly_usage_report as report

router = APIRouter(tags=["usage-report"])


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return value


def _page(title: str, message: str, body: str = "", button: str = "", ok: bool = True,
          status: int = 200, table: str = "") -> HTMLResponse:
    colour = "#2458d9" if ok else "#c0392b"
    form = (f'<form method="post"><button type="submit">{escape(button)}</button>'
            '<p class="note">Nothing is sent until you press this button.</p></form>') if button else ""
    pre = table or (f"<pre>{escape(body)}</pre>" if body else "")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>
body{{margin:0;background:#eaf0fa;color:#193052;font:15px/1.55 Arial,sans-serif}}
main{{max-width:760px;margin:32px auto;padding:0 16px}}
.card{{background:#fff;border-radius:14px;padding:24px;box-shadow:0 10px 30px #0c1d3814}}
h1{{font-size:20px;margin:0 0 8px;color:{colour}}}
pre{{white-space:pre-wrap;font:12.5px/1.5 Consolas,monospace;background:#f5f8fe;border:1px solid #dce4f1;
border-radius:10px;padding:14px;overflow-x:auto}}
table{{border-collapse:collapse;font-size:13.5px;margin-top:6px}}
th{{background:#2458d9;color:#fff;padding:7px 12px;text-align:left}}
td{{padding:7px 12px;border-bottom:1px solid #dce4f1}}
.excel{{display:inline-block;margin:14px 0 4px;color:#2458d9;font-weight:bold}}
button{{background:#2458d9;color:#fff;border:0;border-radius:9px;padding:12px 20px;font-size:15px;
font-weight:bold;cursor:pointer}}
.note{{font-size:12.5px;color:#61718a}}
</style></head><body><main><div class="card"><h1>{escape(title)}</h1><p>{message}</p>{form}{pre}</div></main>
</body></html>""", status_code=status)


@router.get("/usage-report/review/{token}", response_class=HTMLResponse)
def review_report(token: str) -> HTMLResponse:
    try:
        rid = report.read_review_token(token)
    except report.InvalidReviewLink as exc:
        return _page("Link not valid", escape(str(exc)), ok=False, status=400)
    record = report.load_report(rid)
    if record is None:
        return _page("Report not found", "This report is no longer stored on the server. "
                     "Nothing was sent.", ok=False, status=404)
    recipient = escape(record["recipient"])
    if record.get("status") == "sent":
        return _page("Already sent", f"This report was sent to {recipient} on "
                     f"{escape(_when(record.get('sent_at')))}. It will not be sent again.",
                     body=record["body"])
    return _page(f"Send this report to {record['recipient']}?",
                 f"Check the report below. If it looks right, press the button to send it to "
                 f"{recipient} with the Excel sheet attached. If not, close this page: nothing is sent.",
                 body=record["body"], button=f"Yes, send it to {record['recipient']}",
                 table=_table(record, token))


def _table(record: dict, token: str) -> str:
    """Summary table plus a link to the Excel sheet, when the record has them."""
    users = record.get("users")
    if users is None:
        return ""
    rows = "".join(
        f"<tr><td>{escape(u['name'])}</td><td style='text-align:center'>{int(u['leads_viewed'])}</td>"
        f"<td style='font-weight:bold;color:{'#006100' if u['active'] else '#9c0006'}'>"
        f"{'Active' if u['active'] else 'Inactive'}</td></tr>" for u in users
    ) or "<tr><td colspan='3'>No team members have platform access yet.</td></tr>"
    return (f"<p><b>Active users (viewed at least one lead in the last 7 days): "
            f"{int(record.get('active', 0))} of {int(record.get('total', 0))}</b></p>"
            f"<table><tr><th>Name</th><th>Leads viewed</th><th>Status</th></tr>{rows}</table>"
            f"<a class='excel' href='{escape(token)}/excel'>Download the Excel sheet "
            f"({escape(record.get('filename', 'report.xlsx'))})</a>")


@router.get("/usage-report/review/{token}/excel")
def download_excel(token: str) -> Response:
    try:
        rid = report.read_review_token(token)
    except report.InvalidReviewLink as exc:
        return _page("Link not valid", escape(str(exc)), ok=False, status=400)
    record = report.load_report(rid)
    path = report.excel_path(rid)
    if record is None or not path.exists():
        return _page("Report not found", "The Excel file is no longer stored on the server.",
                     ok=False, status=404)
    return Response(path.read_bytes(), media_type=report.XLSX_TYPE, headers={
        "Content-Disposition": f'attachment; filename="{record.get("filename", "report.xlsx")}"'})


@router.post("/usage-report/review/{token}", response_class=HTMLResponse)
def approve_report(token: str) -> HTMLResponse:
    try:
        rid = report.read_review_token(token)
    except report.InvalidReviewLink as exc:
        return _page("Link not valid", escape(str(exc)), ok=False, status=400)
    try:
        record, sent_now = report.approve_and_send(rid)
    except report.ReportNotSendable as exc:
        return _page("Not sent", escape(str(exc)), ok=False, status=409)
    except Exception:
        return _page("Not sent", "The email could not be sent. Nothing went out; open the link "
                     "again later to retry.", ok=False, status=502)
    recipient = escape(record["recipient"])
    if not sent_now:
        return _page("Already sent", f"This report was already sent to {recipient} on "
                     f"{escape(_when(record.get('sent_at')))}. It was not sent again.")
    return _page("Sent", f"The weekly usage report has been sent to {recipient}.")
