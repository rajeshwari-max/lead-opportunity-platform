"""Weekly, evidence-backed platform-usage report.

The append-only ``user_activity_events`` ledger is the primary source.  During
the first week after deployment it may be empty for some users, so the report
falls back per user to the older timestamped workflow tables.  It never adds
the two sources together: doing that would double-count actions recorded in
both places.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, union_all

from app.core.config import settings
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


def send_weekly_usage_report() -> dict[str, int | str]:
    recipient = settings.weekly_usage_report_recipient.strip().lower()
    if not settings.weekly_usage_report_enabled or not recipient:
        log.info("Weekly usage report skipped: disabled or recipient missing")
        return {"sent": 0, "recipient": recipient}
    from app.services.email_service import send_alert

    snapshot = usage_snapshot()
    subject = (
        f"Lead gen weekly usage: {snapshot['engaged']} active user(s), "
        f"{snapshot['actions']} action(s)"
    )
    send_alert(subject, render_text(snapshot), to=recipient)
    log.info("Weekly usage report sent to %s", recipient)
    return {"sent": 1, "recipient": recipient}
