"""Weekly, evidence-backed platform-usage report.

The append-only ``user_activity_events`` ledger is the primary source.  During
the first week after deployment it may be empty for some users, so the report
falls back per user to the older timestamped workflow tables.  It never adds
the two sources together: doing that would double-count actions recorded in
both places.

Approval before it goes out
---------------------------
Since 2026-11-23 the email carries an Excel sheet: each person with platform
access, how many leads they viewed in the last 7 days, and Active (viewed at
least one) or Inactive (see lead_views_snapshot).

The report is not sent straight to its recipient. Each Monday it is emailed to
the reviewer (LOP_WEEKLY_USAGE_REPORT_REVIEWER, the admin) with a link: "Is
this good to send?". The link opens a page showing the exact report and a
"Yes, send it" button; only that button sends it to the recipient
(LOP_WEEKLY_USAGE_REPORT_RECIPIENT). Opening the link sends nothing, so a mail
scanner that pre-fetches links cannot approve it. The report is frozen when
the review mail goes out (backend/data/usage_reports/<id>.json and .xlsx),
so the
recipient gets exactly what the reviewer read, and it can be sent only once.
The link is HMAC-signed with a key derived from the approval secret and works
for LOP_WEEKLY_USAGE_REPORT_REVIEW_DAYS days. Doing nothing sends nothing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import Counter
from html import escape
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, union_all

from app.core.config import BASE_DIR, settings
from app.database.db import session_scope
from app.database.models import (
    ApplicationJourney,
    HumanFeedback,
    JourneyEvent,
    LeadActivity,
    TeamMember,
    UserActivityEvent,
    WorkspaceCredential,
)

log = logging.getLogger("scraper")


def _window(now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end - timedelta(days=7), end


def _legacy_counts(db, start: datetime, end: datetime) -> Counter:
    """Timestamped pre-ledger evidence, grouped by authenticated owner."""
    start_naive = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_naive = end.astimezone(timezone.utc).replace(tzinfo=None)
    activity = union_all(
        select(func.lower(LeadActivity.owner).label("actor"), LeadActivity.viewed_at.label("at"))
        .where(LeadActivity.viewed_at >= start_naive, LeadActivity.viewed_at < end_naive),
        select(func.lower(LeadActivity.owner), LeadActivity.reviewed_at)
        .where(LeadActivity.reviewed_at >= start_naive, LeadActivity.reviewed_at < end_naive),
        select(func.lower(LeadActivity.owner), LeadActivity.source_opened_at)
        .where(LeadActivity.source_opened_at >= start_naive, LeadActivity.source_opened_at < end_naive),
        select(func.lower(ApplicationJourney.owner), ApplicationJourney.updated_at)
        .where(ApplicationJourney.updated_at >= start_naive, ApplicationJourney.updated_at < end_naive),
        select(func.lower(HumanFeedback.owner), HumanFeedback.created_at)
        .where(HumanFeedback.created_at >= start_naive, HumanFeedback.created_at < end_naive),
        select(func.lower(ApplicationJourney.owner), JourneyEvent.created_at)
        .join(JourneyEvent, JourneyEvent.journey_id == ApplicationJourney.id)
        .where(JourneyEvent.created_at >= start_naive, JourneyEvent.created_at < end_naive),
    ).subquery()
    return Counter(dict(db.execute(
        select(activity.c.actor, func.count()).group_by(activity.c.actor)
    ).all()))


def usage_snapshot(now: datetime | None = None) -> dict:
    start, end = _window(now)
    with session_scope() as db:
        members = list(db.scalars(
            select(TeamMember).order_by(func.lower(TeamMember.name), func.lower(TeamMember.email))
        ))
        credentials = {
            row.owner.strip().lower(): bool(row.password_hash)
            for row in db.scalars(select(WorkspaceCredential))
        }
        ledger_rows = db.execute(
            select(
                func.lower(UserActivityEvent.actor),
                UserActivityEvent.event_type,
                func.count(UserActivityEvent.id),
                func.max(UserActivityEvent.occurred_at),
            )
            .where(UserActivityEvent.occurred_at >= start,
                   UserActivityEvent.occurred_at < end)
            .group_by(func.lower(UserActivityEvent.actor), UserActivityEvent.event_type)
        ).all()
        ledger_by_user: dict[str, Counter] = {}
        last_by_user: dict[str, datetime] = {}
        for actor, event_type, count, latest in ledger_rows:
            if not actor:
                continue
            ledger_by_user.setdefault(actor, Counter())[event_type] += count
            if latest and (actor not in last_by_user or latest > last_by_user[actor]):
                last_by_user[actor] = latest
        legacy = _legacy_counts(db, start, end)

    rows = []
    event_totals = Counter()
    for member in members:
        email = member.email.strip().lower()
        breakdown = ledger_by_user.get(email, Counter())
        if breakdown:
            actions = sum(breakdown.values())
            source = "append-only event ledger"
            event_totals.update(breakdown)
        else:
            actions = legacy.get(email, 0)
            source = "timestamped workflow fallback" if actions else "no recorded action"
        rows.append({
            "name": member.name,
            "email": email,
            "member_active": bool(member.active),
            "account_ready": credentials.get(email, False),
            "actions": int(actions),
            "source": source,
            "breakdown": dict(breakdown),
            "last_activity": last_by_user.get(email),
        })
    rows.sort(key=lambda row: (-row["actions"], row["name"].casefold()))
    return {
        "start": start,
        "end": end,
        "users": rows,
        "engaged": sum(row["actions"] > 0 for row in rows),
        "inactive": sum(row["actions"] == 0 for row in rows),
        "actions": sum(row["actions"] for row in rows),
        "event_types": dict(event_totals),
    }


def render_text(snapshot: dict) -> str:
    tz = ZoneInfo(settings.weekly_usage_report_timezone)
    start = snapshot["start"].astimezone(tz).strftime("%d %b %Y, %I:%M %p")
    end = snapshot["end"].astimezone(tz).strftime("%d %b %Y, %I:%M %p")
    lines = [
        "Lead Scanning Platform — weekly usage report",
        f"Period: {start} to {end} ({settings.weekly_usage_report_timezone})",
        "",
        f"Users engaged: {snapshot['engaged']}",
        f"Users with no recorded activity: {snapshot['inactive']}",
        f"Recorded actions: {snapshot['actions']}",
        "",
        "User activity:",
    ]
    for row in snapshot["users"]:
        account = "account ready" if row["account_ready"] else "account not ready"
        member = "enabled" if row["member_active"] else "disabled"
        lines.append(
            f"  - {row['name']} <{row['email']}>: {row['actions']} action(s) "
            f"[{member}; {account}; {row['source']}]"
        )
        if row["breakdown"]:
            detail = ", ".join(
                f"{name.replace('_', ' ')} {count}"
                for name, count in sorted(row["breakdown"].items())
            )
            lines.append(f"      {detail}")
    lines += [
        "",
        "Evidence note: authenticated server-side append-only events are used "
        "when available. Older timestamped lead, journey and feedback records "
        "are used only as a per-user fallback and are not added to ledger totals.",
    ]
    return "\n".join(lines)


# ------------------------------------------------- lead-view report (Excel)
#
# What the weekly email carries since 2026-11-23: one row per person with
# platform access, how many leads they viewed in the last 7 days, and Active /
# Inactive. Active = viewed at least one lead in the last 7 days.
# "Viewed" = opened a lead's brief on the dashboard (opportunity_viewed in the
# event ledger, or LeadActivity.viewed_at for the older record). A lead counts
# once per person however often it was opened; the two sources are combined
# as a set of lead ids, so nothing is counted twice.

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def lead_views_snapshot(now: datetime | None = None) -> dict:
    start, end = _window(now)
    start_naive = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_naive = end.astimezone(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        members = list(db.scalars(
            select(TeamMember).order_by(func.lower(TeamMember.name), func.lower(TeamMember.email))))
        events = db.execute(
            select(func.lower(UserActivityEvent.actor), UserActivityEvent.opportunity_id,
                   UserActivityEvent.occurred_at)
            .where(UserActivityEvent.event_type == "opportunity_viewed",
                   UserActivityEvent.opportunity_id.is_not(None),
                   UserActivityEvent.occurred_at >= start,
                   UserActivityEvent.occurred_at < end)
        ).all()
        legacy = db.execute(
            select(func.lower(LeadActivity.owner), LeadActivity.opportunity_id, LeadActivity.viewed_at)
            .where(LeadActivity.viewed_at >= start_naive, LeadActivity.viewed_at < end_naive)
        ).all()

    viewed: dict[str, set[int]] = {}
    last: dict[str, datetime] = {}
    for actor, opportunity_id, at in [*events, *legacy]:
        if not actor or opportunity_id is None:
            continue
        viewed.setdefault(actor, set()).add(int(opportunity_id))
        at = _utc(at)
        if at and (actor not in last or at > last[actor]):
            last[actor] = at

    rows = []
    for member in members:
        if not member.active:
            continue            # access switched off: not expected to use it
        email = member.email.strip().lower()
        count = len(viewed.get(email, ()))
        rows.append({"name": member.name, "email": email, "leads_viewed": count,
                     "active": count > 0, "last_viewed": last.get(email)})
    rows.sort(key=lambda r: (not r["active"], -r["leads_viewed"], r["name"].casefold()))
    return {
        "start": start, "end": end, "users": rows,
        "active": sum(r["active"] for r in rows),
        "inactive": sum(not r["active"] for r in rows),
        "total": len(rows),
        "leads_viewed": sum(r["leads_viewed"] for r in rows),
        "disabled_excluded": sum(not m.active for m in members),
    }


def _local(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    if value is None:
        return ""
    return _utc(value).astimezone(ZoneInfo(settings.weekly_usage_report_timezone)).strftime(fmt)


def _period(snapshot: dict) -> str:
    return f"{_local(snapshot['start'], '%d %b %Y')} to {_local(snapshot['end'], '%d %b %Y')}"


def report_filename(snapshot: dict) -> str:
    return f"lead-platform-weekly-usage-{_local(snapshot['end'], '%Y-%m-%d')}.xlsx"


def render_xlsx(snapshot: dict) -> bytes:
    """The Excel sheet: Name, Email, Leads viewed, Status, Last lead viewed."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Weekly usage"
    ws["A1"] = "Lead Scanning Platform: weekly usage"
    ws["A1"].font = Font(bold=True, size=14, color="193052")
    ws["A2"] = f"Period: last 7 days, {_period(snapshot)} ({settings.weekly_usage_report_timezone})"
    ws["A3"] = f"Active users (viewed at least one lead): {snapshot['active']} of {snapshot['total']}"
    ws["A4"] = f"Inactive users (no lead viewed in the last 7 days): {snapshot['inactive']}"
    for cell in ("A3", "A4"):
        ws[cell].font = Font(bold=True)

    header = ["Name", "Email", "Leads viewed (last 7 days)", "Status", "Last lead viewed"]
    head_row = 6
    thin = Side(style="thin", color="DCE4F1")
    for col, title in enumerate(header, start=1):
        c = ws.cell(row=head_row, column=col, value=title)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2458D9")
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
    green = (PatternFill("solid", fgColor="C6EFCE"), Font(bold=True, color="006100"))
    red = (PatternFill("solid", fgColor="FFC7CE"), Font(bold=True, color="9C0006"))
    for i, row in enumerate(snapshot["users"], start=head_row + 1):
        values = [row["name"], row["email"], row["leads_viewed"],
                  "Active" if row["active"] else "Inactive", _local(row["last_viewed"]) or "—"]
        for col, value in enumerate(values, start=1):
            c = ws.cell(row=i, column=col, value=value)
            c.border = Border(bottom=thin)
        fill, font = green if row["active"] else red
        ws.cell(row=i, column=4).fill = fill
        ws.cell(row=i, column=4).font = font
        ws.cell(row=i, column=3).alignment = Alignment(horizontal="center")
    last_row = head_row + max(1, len(snapshot["users"]))
    if not snapshot["users"]:
        ws.cell(row=head_row + 1, column=1, value="No team members have platform access yet.")
    note = last_row + 2
    ws.cell(row=note, column=1, value=(
        "Active = viewed at least one lead in the last 7 days. A lead counts once per person, "
        "however many times it was opened. Viewed = opened the lead's brief on the dashboard."))
    ws.cell(row=note, column=1).font = Font(italic=True, color="61718A")
    if snapshot["disabled_excluded"]:
        ws.cell(row=note + 1, column=1, value=(
            f"{snapshot['disabled_excluded']} member(s) whose access is switched off are not listed."))
        ws.cell(row=note + 1, column=1).font = Font(italic=True, color="61718A")
    for col, width in zip("ABCDE", (28, 34, 16, 12, 24)):
        ws.column_dimensions[col].width = width
    ws.row_dimensions[head_row].height = 30
    ws.freeze_panes = ws.cell(row=head_row + 1, column=1)
    ws.auto_filter.ref = f"A{head_row}:E{last_row}"
    # Prints on one page width.
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def render_summary_text(snapshot: dict) -> str:
    lines = [
        "Hello,",
        "",
        f"Please find attached this week's usage report for the Lead Scanning Platform "
        f"(last 7 days, {_period(snapshot)}).",
        "",
        f"Active users (viewed at least one lead): {snapshot['active']} of {snapshot['total']}",
        f"Inactive users (no lead viewed in the last 7 days): {snapshot['inactive']}",
        "",
    ]
    for r in snapshot["users"]:
        lines.append(f"  - {r['name']}: {r['leads_viewed']} lead(s) viewed — "
                     f"{'Active' if r['active'] else 'Inactive'}")
    lines += ["", "The attached Excel sheet has the full list with each person's email and "
              "when they last viewed a lead."]
    return "\n".join(lines)


def render_summary_html(snapshot: dict) -> str:
    cells = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #dce4f1'>{escape(r['name'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #dce4f1;text-align:center'>{r['leads_viewed']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #dce4f1;font-weight:bold;"
        f"color:{'#006100' if r['active'] else '#9c0006'}'>{'Active' if r['active'] else 'Inactive'}</td></tr>"
        for r in snapshot["users"]) or (
        "<tr><td colspan='3' style='padding:6px 10px'>No team members have platform access yet.</td></tr>")
    return f"""<p>Please find attached this week's usage report for the Lead Scanning Platform
(last 7 days, {escape(_period(snapshot))}).</p>
<p><b>Active users (viewed at least one lead): {snapshot['active']} of {snapshot['total']}</b><br>
Inactive users (no lead viewed in the last 7 days): {snapshot['inactive']}</p>
<table style="border-collapse:collapse;font-size:13px">
<tr style="background:#2458d9;color:#fff"><th style="padding:6px 10px;text-align:left">Name</th>
<th style="padding:6px 10px">Leads viewed</th><th style="padding:6px 10px;text-align:left">Status</th></tr>
{cells}</table>
<p style="font-size:12px;color:#61718a">The attached Excel sheet has the full list with each person's
email and when they last viewed a lead.</p>"""


# ----------------------------------------------------------- review step

REPORTS_DIR = BASE_DIR / "data" / "usage_reports"
_PURPOSE = b"weekly-usage-report-review"
_ID = re.compile(r"^[0-9]{8}-[0-9a-f]{12}$")
_lock = threading.Lock()


class InvalidReviewLink(Exception):
    """The review link was tampered with, is malformed or has expired."""


def _key() -> bytes:
    # A key of its own, derived from the approval secret, so a review link can
    # never be replayed as an opportunity-approval link or the other way round.
    return hmac.new(settings.approval_secret.encode(), _PURPOSE, hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_review_token(report_id: str, ttl: int | None = None) -> str:
    ttl = ttl if ttl is not None else max(1, settings.weekly_usage_report_review_days) * 86400
    body = _b64e(json.dumps({"rid": report_id, "exp": int(time.time()) + ttl},
                            separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_review_token(token: str) -> str:
    """Return the report id the link names, or raise InvalidReviewLink."""
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise InvalidReviewLink("This link is not complete. Open it from the email again.") from exc
    expected = _b64e(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise InvalidReviewLink("This link is not valid. Open it from the email again.")
    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:
        raise InvalidReviewLink("This link is not valid. Open it from the email again.") from exc
    if int(payload.get("exp", 0)) < time.time():
        raise InvalidReviewLink("This link has expired. Nothing was sent; next Monday's report "
                                "will come with a new link.")
    report_id = str(payload.get("rid", ""))
    if not _ID.match(report_id):
        raise InvalidReviewLink("This link is not valid. Open it from the email again.")
    return report_id


def review_url(token: str) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}{settings.api_prefix}/usage-report/review/{token}"


def _path(report_id: str) -> Path:
    if not _ID.match(report_id):
        raise InvalidReviewLink("Unknown report.")
    return REPORTS_DIR / f"{report_id}.json"


def excel_path(report_id: str) -> Path:
    return _path(report_id).with_suffix(".xlsx")


def _save(record: dict) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(record["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def load_report(report_id: str) -> dict | None:
    path = _path(report_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _review_mail(record: dict, url: str) -> tuple[str, str, str]:
    days = max(1, settings.weekly_usage_report_review_days)
    recipient = record["recipient"]
    subject = f"Review before sending: {record['subject']}"
    text = "\n".join([
        f"This week's usage report is ready (Excel sheet attached). It has NOT been sent to {recipient} yet.",
        "",
        f"Is it good to send to {recipient}?",
        f"Review it and approve it here: {url}",
        "",
        f"The link opens the report with a \"Yes, send it\" button. It works for {days} days. "
        "If you do nothing, nothing is sent.",
        "",
        "-" * 60,
        f"What {recipient} will receive:",
        "",
        record["body"],
    ])
    html = f"""<div style="font-family:Arial,sans-serif;color:#193052;max-width:720px">
<p style="font-size:15px">This week's usage report is ready (Excel sheet attached). It has <b>not</b>
been sent to {escape(recipient)} yet.</p>
<p style="font-size:15px"><b>Is it good to send to {escape(recipient)}?</b></p>
<p><a href="{escape(url)}" style="display:inline-block;background:#2458d9;color:#fff;
padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:bold">Review and approve</a></p>
<p style="font-size:12px;color:#61718a">The button opens the report with a "Yes, send it" button.
It works for {days} days. If you do nothing, nothing is sent.</p>
<hr style="border:0;border-top:1px solid #dce4f1">
<p style="font-size:12px;color:#61718a">What {escape(recipient)} will receive:</p>
{record["html"]}
</div>"""
    return subject, text, html


def send_weekly_usage_report() -> dict[str, int | str]:
    """Monday job. Emails the reviewer for approval; never the recipient directly
    (unless LOP_WEEKLY_USAGE_REPORT_REQUIRE_APPROVAL is turned off)."""
    recipient = settings.weekly_usage_report_recipient.strip().lower()
    if not settings.weekly_usage_report_enabled or not recipient:
        log.info("Weekly usage report skipped: disabled or recipient missing")
        return {"sent": 0, "recipient": recipient}
    from app.services.email_service import is_configured, send_alert

    snapshot = lead_views_snapshot()
    subject = (
        f"Lead gen weekly usage: {snapshot['active']} of {snapshot['total']} users active "
        f"({_local(snapshot['end'], '%d %b %Y')})"
    )
    body = render_summary_text(snapshot)
    html = render_summary_html(snapshot)
    xlsx = render_xlsx(snapshot)
    filename = report_filename(snapshot)

    if not settings.weekly_usage_report_require_approval:
        send_alert(subject, body, to=recipient, html=html,
                   attachments=[(filename, xlsx, XLSX_TYPE)])
        log.info("Weekly usage report sent to %s (approval switched off)", recipient)
        return {"sent": 1, "recipient": recipient}

    reviewer = settings.weekly_usage_report_reviewer.strip().lower()
    if not reviewer:
        log.warning("Weekly usage report NOT sent: approval is required but "
                    "LOP_WEEKLY_USAGE_REPORT_REVIEWER is not set")
        return {"sent": 0, "review_sent": 0, "recipient": recipient, "reviewer": ""}
    if not is_configured():
        log.warning("Weekly usage report NOT sent: email (SMTP) is not configured")
        return {"sent": 0, "review_sent": 0, "recipient": recipient, "reviewer": reviewer}

    record = {
        "id": f"{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(6)}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "body": body,
        "html": html,
        "filename": filename,
        "active": snapshot["active"],
        "total": snapshot["total"],
        "users": [{"name": r["name"], "leads_viewed": r["leads_viewed"], "active": r["active"]}
                  for r in snapshot["users"]],
        "recipient": recipient,
        "reviewer": reviewer,
        "status": "pending",
        "sent_at": None,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    excel_path(record["id"]).write_bytes(xlsx)
    _save(record)
    url = review_url(make_review_token(record["id"]))
    review_subject, text, review_html = _review_mail(record, url)
    send_alert(review_subject, text, to=reviewer, html=review_html,
               attachments=[(filename, xlsx, XLSX_TYPE)])
    log.info("Weekly usage report %s sent to %s for approval before %s",
             record["id"], reviewer, recipient)
    return {"sent": 0, "review_sent": 1, "recipient": recipient,
            "reviewer": reviewer, "report_id": record["id"]}


class ReportNotSendable(Exception):
    """The report cannot be sent (missing, or email is not set up)."""


def approve_and_send(report_id: str) -> tuple[dict, bool]:
    """Send a reviewed report to its recipient, once.

    Returns (record, sent_now). sent_now is False when it had already been
    sent (a second click, a forwarded link): it is never sent twice.
    """
    from app.services.email_service import is_configured, send_alert

    with _lock:
        record = load_report(report_id)
        if record is None:
            raise ReportNotSendable("This report is no longer stored on the server.")
        if record.get("status") == "sent":
            return record, False
        if not is_configured():
            raise ReportNotSendable("Email is not set up on the server, so it could not be sent.")
        xlsx = excel_path(report_id)
        if not xlsx.exists():
            raise ReportNotSendable("The Excel file for this report is missing on the server.")
        send_alert(record["subject"], record["body"], to=record["recipient"],
                   html=record.get("html"),
                   attachments=[(record["filename"], xlsx.read_bytes(), XLSX_TYPE)])
        record["status"] = "sent"
        record["sent_at"] = datetime.now(timezone.utc).isoformat()
        _save(record)
    log.info("Weekly usage report %s approved and sent to %s", report_id, record["recipient"])
    return record, True
