"""Matching Engine — finds active opportunities relevant to a team member.

A member matches an opportunity when:
  * any of their keywords appears in title/summary/vertical/eligibility (case-insensitive), AND
  * the opportunity's category is in their category list (empty list = all categories), AND
  * the opportunity belongs to one of their verticals (empty list = all verticals), AND
  * it hasn't already been sent to them (SentLog), unless include_sent=True.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database.models import Category, Opportunity, SentLog, Status, TeamMember
from app.services.actionable import actionable_clause


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class MatchingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def matches_for(
        self, member: TeamMember, include_sent: bool = False, limit: int | None = None
    ) -> list[Opportunity]:
        """Every unsent match for this member. `limit=None` means no cap.

        This used to default to 100, which silently truncated both the count
        shown against each member and the digest actually emailed — a member
        with 900 matches saw "100 new" and received 100.
        """
        # Was a third copy of the "still open" rule, and it had drifted from
        # the other two: `deadline IS NULL` counted as open, so every row whose
        # date could not be parsed was emailed as a live opportunity.
        stmt = select(Opportunity).where(actionable_clause())

        keywords = _csv(member.keywords)
        if keywords:
            clauses = []
            for kw in keywords:
                like = f"%{kw.lower()}%"
                clauses.append(func.lower(Opportunity.title).like(like))
                clauses.append(func.lower(Opportunity.summary).like(like))
                clauses.append(func.lower(Opportunity.vertical).like(like))
                clauses.append(func.lower(Opportunity.eligibility).like(like))
            stmt = stmt.where(or_(*clauses))

        categories = _csv(member.categories)
        if categories:
            valid = [Category(c) for c in categories if c in Category._value2member_map_]
            if valid:
                stmt = stmt.where(Opportunity.category.in_(valid))

        # Vertical routing: only email opportunities in the member's selected
        # verticals (empty = all verticals, preserving pre-vertical behaviour).
        verticals = _csv(getattr(member, "verticals", "") or "")
        if verticals:
            stmt = stmt.where(
                or_(*[Opportunity.verticals.like(f"%{s}%") for s in verticals])
            )

        if not include_sent:
            sent = select(SentLog.opportunity_id).where(SentLog.member_id == member.id)
            stmt = stmt.where(Opportunity.id.not_in(sent))

        stmt = stmt.order_by(Opportunity.deadline.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def mark_sent(self, member: TeamMember, opportunities: list[Opportunity]) -> None:
        """Record what went out, refreshing the timestamp on anything resent.

        SentLog has a unique constraint on (member_id, opportunity_id), so a
        plain insert raises IntegrityError the moment a resend includes an
        opportunity the member already had — which is every resend. Existing
        rows get their sent_at bumped instead, keeping "when did they last see
        this" accurate.
        """
        if not opportunities:
            return
        ids = [o.id for o in opportunities]
        already = {
            row.opportunity_id: row
            for row in self.db.execute(
                select(SentLog).where(
                    SentLog.member_id == member.id, SentLog.opportunity_id.in_(ids)
                )
            ).scalars()
        }
        now = datetime.now(timezone.utc)
        for opp in opportunities:
            existing = already.get(opp.id)
            if existing is not None:
                existing.sent_at = now
            else:
                self.db.add(SentLog(member_id=member.id, opportunity_id=opp.id, sent_at=now))
