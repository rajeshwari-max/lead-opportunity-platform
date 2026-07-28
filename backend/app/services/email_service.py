"""Email service — sends opportunity digests over SMTP (Gmail App Password friendly)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.database.models import Opportunity, TeamMember

log = logging.getLogger("scraper")


class EmailNotConfiguredError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def _digest_html(member: TeamMember, opportunities: list[Opportunity]) -> str:
    rows = []
    for o in opportunities:
        deadline = o.deadline.strftime("%d %b %Y") if o.deadline else "—"
        rows.append(f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
            <a href="{o.opportunity_url}" style="color:#4f46e5;font-weight:600;text-decoration:none;">{o.title}</a>
            <div style="color:#6b7280;font-size:13px;margin-top:2px;">
              {o.organization or o.source_website}{' · ' + o.location if o.location else ''}
            </div>
          </td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;white-space:nowrap;font-size:13px;">{o.category.value}</td>
          <td style="padding:10px;border-bottom:1px solid #e5e7eb;white-space:nowrap;font-size:13px;color:#b45309;">{deadline}</td>
        </tr>""")
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;margin:auto;">
      <h2 style="color:#111827;">Hi {member.name},</h2>
      <p style="color:#374151;">Here {'is' if len(opportunities) == 1 else 'are'} <b>{len(opportunities)}</b>
      funding {'opportunity' if len(opportunities) == 1 else 'opportunities'} matching your interests
      ({member.keywords or 'all topics'}):</p>
      <table style="border-collapse:collapse;width:100%;background:#ffffff;">
        <tr style="text-align:left;color:#6b7280;font-size:12px;text-transform:uppercase;">
          <th style="padding:10px;">Opportunity</th><th style="padding:10px;">Type</th><th style="padding:10px;">Deadline</th>
        </tr>
        {''.join(rows)}
      </table>
      <p style="color:#9ca3af;font-size:12px;margin-top:16px;">Sent by Lead Opportunity Platform</p>
    </div>"""


def send_digest(member: TeamMember, opportunities: list[Opportunity]) -> None:
    """Blocking SMTP send — call via asyncio.to_thread from async contexts."""
    if not is_configured():
        raise EmailNotConfiguredError(
            "SMTP is not configured. Set LOP_SMTP_USER and LOP_SMTP_PASSWORD "
            "(see backend/.env.example)."
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(opportunities)} funding opportunit{'y' if len(opportunities) == 1 else 'ies'} for you"
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = member.email
    msg.attach(MIMEText(_digest_html(member, opportunities), "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("Emailed %s opportunities to %s <%s>", len(opportunities), member.name, member.email)


def send_alert(subject: str, body: str, to: str | None = None) -> None:
    """Blocking SMTP send for a plain-text operational alert (e.g. a scraper's
    saved login session has expired). Separate from send_digest because it
    isn't tied to a TeamMember or a list of Opportunity rows.

    Logs and returns quietly if SMTP isn't configured yet, rather than raising —
    callers (scrapers) shouldn't fail a scrape just because alerting is unset up.
    """
    if not is_configured():
        log.warning("[alert] SMTP not configured — skipping alert: %s", subject)
        return
    recipient = to or settings.smtp_user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("[alert] sent '%s' to %s", subject, recipient)
