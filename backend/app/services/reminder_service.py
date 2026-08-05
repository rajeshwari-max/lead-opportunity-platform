"""Deadline reminders for opportunities already sent to a team member.

A lead is only useful if someone acts before it closes, so each opportunity a
member received is followed up twice: once with a week to go and once with two
days left. Reminders go only to the member the opportunity was actually sent to,
and each (member, opportunity, offset) fires at most once — tracked in
`reminder_log` so a restart, a re-run, or two scrapes in one day can't produce
duplicates.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models import Base, Opportunity, SentLog, Status, TeamMember

log = logging.getLogger("scraper")

# Fallback only. The live values come from the dashboard (email_settings.json)
# so the schedule can be changed without editing a file and restarting.
# 10 days is the first nudge: long enough to actually decide whether to bid and
# assemble a proposal, where 7 already forces a rushed answer.
REMINDER_OFFSETS: tuple[int, ...] = (10, 7, 2)


def _offsets() -> tuple[int, ...]:
    """Reminder offsets currently configured in the dashboard."""
    try:
        from app.services.email_settings import load

        days = tuple(load().reminder_days)
        return days or REMINDER_OFFSETS
    except Exception:            # settings unreadable — never skip reminders
        log.exception("Could not read reminder settings — using defaults")
        return REMINDER_OFFSETS


class ReminderLog(Base):
    """One row per reminder actually sent — the idempotency guard."""

    __tablename__ = "reminder_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(index=True)
    opportunity_id: Mapped[int] = mapped_column(index=True)
    days_before: Mapped[int] = mapped_column(index=True)
    sent_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    kind: Mapped[str] = mapped_column(String(16), default="deadline")

    __table_args__ = (
        UniqueConstraint("member_id", "opportunity_id", "days_before",
                         name="uq_reminder_once"),
    )


def _due_today(db, offset: int, today: date) -> list[tuple[TeamMember, Opportunity]]:
    """Opportunities closing in exactly `offset` days that a member was sent."""
    target = today + timedelta(days=offset)
    rows = db.execute(
        select(TeamMember, Opportunity)
        .join(SentLog, SentLog.member_id == TeamMember.id)
        .join(Opportunity, Opportunity.id == SentLog.opportunity_id)
        .where(
            Opportunity.deadline == target,
            Opportunity.status == Status.ACTIVE,
            TeamMember.active == True,          # noqa: E712
        )
    ).all()
    return [(m, o) for m, o in rows]


def send_due_reminders(today: date | None = None) -> int:
    """Send every reminder due today. Safe to call repeatedly."""
    from app.database.db import session_scope
    from app.services import email_service

    if not email_service.is_configured():
        log.info("Reminders skipped — SMTP not configured")
        return 0

    today = today or date.today()
    sent_count = 0
    with session_scope() as db:
        for offset in _offsets():
            # Group by member so each person gets one email per offset rather
            # than one per opportunity.
            per_member: dict[int, tuple[TeamMember, list[Opportunity]]] = {}
            for member, opp in _due_today(db, offset, today):
                already = db.execute(
                    select(ReminderLog.id).where(
                        ReminderLog.member_id == member.id,
                        ReminderLog.opportunity_id == opp.id,
                        ReminderLog.days_before == offset,
                    )
                ).scalar_one_or_none()
                if already is not None:
                    continue
                per_member.setdefault(member.id, (member, []))[1].append(opp)

            for member, opps in per_member.values():
                if not opps:
                    continue
                try:
                    email_service.send_reminder(member, opps, offset)
                except Exception:
                    log.exception("Reminder email failed for %s — not marking sent",
                                  member.email)
                    continue
                for opp in opps:
                    db.add(ReminderLog(member_id=member.id, opportunity_id=opp.id,
                                       days_before=offset))
                sent_count += len(opps)
                log.info("Reminder: %s opportunity(ies) closing in %s days -> %s",
                         len(opps), offset, member.email)
    return sent_count
