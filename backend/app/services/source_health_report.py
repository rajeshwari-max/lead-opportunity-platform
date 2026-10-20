"""Monthly operational source-health report, sent only to administrators."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.database.db import session_scope
from app.database.models import WorkspaceCredential
from app.services import email_service, scraper_health
from app.services.scrape_outcome import Outcome

log = logging.getLogger("scraper")


def admin_recipients(db) -> list[str]:
    """Return verified workspace administrators, never ordinary members.

    WorkspaceCredential is the same authority used by the dashboard's admin
    gate. Using TeamMember.auto_send here would accidentally send operational
    information to every digest recipient.
    """
    rows = db.execute(
        select(WorkspaceCredential.owner)
        .where(WorkspaceCredential.is_admin.is_(True))
        .order_by(WorkspaceCredential.owner)
    ).scalars().all()
    return sorted({str(email).strip().lower() for email in rows if "@" in str(email)})


def _bucket(entry) -> str:
    outcome = entry.last_outcome
    if outcome == Outcome.CONFIRMED_EMPTY.value:
        return "NO ACTIVE OPPORTUNITIES (source confirmed empty)"
    if outcome in (Outcome.NO_FETCH.value, Outcome.BLOCKED.value,
                   Outcome.AUTH_REQUIRED.value, Outcome.SESSION_EXPIRED.value):
        return "NO PAGE FETCHED / ACCESS FAILURE"
    if outcome in (Outcome.PARSE_ZERO.value, Outcome.STRUCTURE_CHANGED.value):
        return "PAGE FETCHED BUT NO RESULTS / STRUCTURE CHECK NEEDED"
    if entry.state in ("failing", "never_produced", "stale"):
        return "OTHER ATTENTION REQUIRED"
    return "HEALTHY"


def build_report(entries, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    counts = Counter(_bucket(e) for e in entries)
    lines = [
        "Lead Scanning Platform — Monthly Source Website Health",
        f"Generated: {generated_at.astimezone().strftime('%d %b %Y, %I:%M %p %Z')}",
        "",
        f"Sources checked: {len(entries)}",
    ]
    for label in (
        "HEALTHY",
        "NO ACTIVE OPPORTUNITIES (source confirmed empty)",
        "NO PAGE FETCHED / ACCESS FAILURE",
        "PAGE FETCHED BUT NO RESULTS / STRUCTURE CHECK NEEDED",
        "OTHER ATTENTION REQUIRED",
    ):
        lines.append(f"{label}: {counts[label]}")

    lines += [
        "",
        "Interpretation:",
        "- No page fetched means URL/network/access/login/bot protection must be checked.",
        "- Page fetched but no results means the parser or changed page structure must be checked.",
        "- Confirmed empty is healthy: the source explicitly showed no active opportunity and remains scheduled.",
    ]

    for label in (
        "NO PAGE FETCHED / ACCESS FAILURE",
        "PAGE FETCHED BUT NO RESULTS / STRUCTURE CHECK NEEDED",
        "OTHER ATTENTION REQUIRED",
        "NO ACTIVE OPPORTUNITIES (source confirmed empty)",
    ):
        rows = [e for e in entries if _bucket(e) == label]
        if not rows:
            continue
        lines += ["", label + ":"]
        for e in rows:
            detail = e.last_error_message or e.note or e.last_outcome or "No detail recorded"
            status = f"HTTP {e.last_http_status}; " if e.last_http_status else ""
            lines.append(
                f"- {e.display_name}: {status}{detail[:240]} "
                f"[last outcome: {e.last_outcome or 'unknown'}]"
            )
    return "\n".join(lines)


def send_monthly_report() -> dict[str, int]:
    """Build one snapshot and send it separately to each admin address."""
    with session_scope() as db:
        recipients = admin_recipients(db)
        entries = scraper_health.source_health(
            db,
            failure_streak=settings.health_failure_streak,
            stale_days=settings.health_stale_days,
        )
    if not recipients:
        log.warning("Monthly source-health report skipped: no admin account email found")
        return {"admins": 0, "sent": 0, "sources": len(entries)}

    body = build_report(entries)
    subject = datetime.now().strftime("Source website health — %B %Y")
    sent = 0
    for recipient in recipients:
        try:
            email_service.send_alert(subject=subject, body=body, to=recipient)
            sent += 1
        except Exception:  # one bad mailbox must not prevent other admins
            log.exception("Monthly source-health report failed for %s", recipient)
    log.info("Monthly source-health report sent to %s/%s admin(s)", sent, len(recipients))
    return {"admins": len(recipients), "sent": sent, "sources": len(entries)}
