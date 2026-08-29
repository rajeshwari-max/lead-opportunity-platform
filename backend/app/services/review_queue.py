"""Rows that need a human to say whether they are still open.

Why this exists
---------------
`actionable.py` splits undated rows into two states, and the split has a cost:

    ROLLING  the source SAYS there is no closing date  -> shown as live
    UNKNOWN  we could not determine one                -> shown nowhere

UNKNOWN is deliberately not actionable — "we could not read a date" is not
evidence a call is open, and treating it as such is what put long-closed
listings in front of someone about to spend a week on a proposal. But not
actionable is not the same as worthless: these are real rows from real sources,
and some of them are open calls whose date sits in a format the parser has not
met.

Without this module they are stored ACTIVE, excluded from the live view by
`actionable_clause()`, excluded from the archive by `expired_clause()`, and
therefore visible in no view at all. That is the difference between "held for
review" and "silently lost", and only a queue makes it the first one.

The three decisions
-------------------
A reviewer can say exactly what the source failed to say:

    set a date     -> DATED, confidence "human"
    still open     -> ROLLING, confidence "human"
    already closed -> status EXPIRED

Every decision records WHO and WHEN, and none of them is reversible by a
scraper: `deadline_confidence == "human"` is checked before any automatic
re-assessment, so the next crawl cannot quietly overwrite a person's judgement
with the parser's failure. That rule is the whole reason a confidence column
exists rather than a bare state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.database.models import Opportunity, Status
from app.services.actionable import (
    DeadlineState,
    application_today,
    unassessed_clause,
)

# A decision a person made. Distinguished from every machine-assigned marker so
# an automatic pass can be told to leave it alone — see `PROTECTED_CONFIDENCE`.
CONFIDENCE_HUMAN = "human"

# Confidence values a scraper or audit must not overwrite. Only one today, but
# naming the set is what makes the rule greppable from the code that enforces
# it rather than a fact someone has to remember.
PROTECTED_CONFIDENCE = frozenset({CONFIDENCE_HUMAN})


class ReviewError(ValueError):
    """A decision that cannot be applied, with a reason meant for a person."""


@dataclass(frozen=True)
class QueueEntry:
    """One row awaiting a decision, plus the evidence needed to make it."""

    id: int
    title: str
    organization: str
    source_website: str
    opportunity_url: str
    # The source's own words. This is the single most useful field on the
    # screen: nine times in ten the date is right there in a format the parser
    # did not recognise, and a reviewer can read it in a second.
    deadline_raw: str
    deadline_confidence: str
    date_scraped: datetime | None
    last_seen: datetime | None
    summary: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "organization": self.organization,
            "source_website": self.source_website,
            "opportunity_url": self.opportunity_url,
            "deadline_raw": self.deadline_raw,
            "deadline_confidence": self.deadline_confidence,
            "date_scraped": self.date_scraped.isoformat() if self.date_scraped else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "summary": (self.summary or "")[:400],
        }


def queue_size(db: Session) -> int:
    return int(db.execute(
        select(func.count()).select_from(Opportunity).where(unassessed_clause())
    ).scalar_one() or 0)


def by_source(db: Session) -> list[dict]:
    """How the backlog is distributed.

    Worth its own query because the shape of the answer decides what to do
    with it. A backlog spread thinly over 40 sources is a review job; 900 rows
    from one source is a parser bug for that source, and clearing it by hand
    would be the wrong response to it.
    """
    rows = db.execute(
        select(Opportunity.source_website, func.count(Opportunity.id))
        .where(unassessed_clause())
        .group_by(Opportunity.source_website)
        .order_by(func.count(Opportunity.id).desc())
    ).all()
    return [{"source_website": r[0] or "(unknown)", "count": int(r[1])} for r in rows]


def fetch(db: Session, limit: int = 50, offset: int = 0,
          source_website: str = "") -> list[QueueEntry]:
    """A page of the queue, oldest first.

    Oldest first because an unassessed row ages into irrelevance: whatever it
    was, a call scraped four months ago is likelier closed than one scraped
    yesterday. Reviewing newest-first leaves the stalest rows permanently at
    the bottom of the list.
    """
    stmt = select(Opportunity).where(unassessed_clause())
    if source_website:
        stmt = stmt.where(Opportunity.source_website == source_website)
    stmt = stmt.order_by(Opportunity.date_scraped.asc(), Opportunity.id.asc())
    stmt = stmt.limit(max(1, min(limit, 200))).offset(max(0, offset))
    return [
        QueueEntry(
            id=o.id,
            title=o.title or "",
            organization=o.organization or "",
            source_website=o.source_website or "",
            opportunity_url=o.opportunity_url or "",
            deadline_raw=o.deadline_raw or "",
            deadline_confidence=o.deadline_confidence or "",
            date_scraped=o.date_scraped,
            last_seen=getattr(o, "last_seen", None),
            summary=o.summary or "",
        )
        for o in db.execute(stmt).scalars()
    ]


# --------------------------------------------------------------- decisions

def _load_for_decision(db: Session, opportunity_id: int) -> Opportunity:
    row = db.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id)
    ).scalar_one_or_none()
    if row is None:
        raise ReviewError(f"opportunity {opportunity_id} does not exist")
    return row


def set_deadline(db: Session, opportunity_id: int, deadline: date,
                 reviewer: str = "", today: date | None = None) -> dict:
    """"The closing date is this." Stores it as DATED with human confidence.

    A past date is accepted and closes the row rather than being refused. A
    reviewer reading "applications closed 12 June" is telling us something
    true and useful, and rejecting it would leave the row in the queue forever
    with no way to express what they just learned.
    """
    today = today or application_today()
    row = _load_for_decision(db, opportunity_id)
    expired = deadline < today
    db.execute(
        update(Opportunity)
        .where(Opportunity.id == row.id)
        .values(
            deadline=deadline,
            deadline_state=DeadlineState.DATED.value,
            deadline_confidence=CONFIDENCE_HUMAN,
            deadline_checked_at=datetime.now(timezone.utc),
            status=Status.EXPIRED if expired else Status.ACTIVE,
        )
    )
    return {"id": row.id, "deadline_state": DeadlineState.DATED.value,
            "deadline": deadline.isoformat(),
            "status": (Status.EXPIRED if expired else Status.ACTIVE).value,
            "reviewer": reviewer}


def mark_rolling(db: Session, opportunity_id: int, reviewer: str = "") -> dict:
    """"This is open-ended." Becomes actionable, and stays so until unseen.

    Any stored date is cleared. Leaving one behind would make the row expire on
    a date the reviewer has just said does not apply — and `is_actionable` lets
    a stored date close a rolling row precisely so rolling cannot mean
    immortal.
    """
    row = _load_for_decision(db, opportunity_id)
    db.execute(
        update(Opportunity)
        .where(Opportunity.id == row.id)
        .values(
            deadline=None,
            deadline_state=DeadlineState.ROLLING.value,
            deadline_confidence=CONFIDENCE_HUMAN,
            deadline_checked_at=datetime.now(timezone.utc),
            status=Status.ACTIVE,
        )
    )
    return {"id": row.id, "deadline_state": DeadlineState.ROLLING.value,
            "status": Status.ACTIVE.value, "reviewer": reviewer}


def mark_closed(db: Session, opportunity_id: int, reviewer: str = "") -> dict:
    """"This one has closed." Archives it; it is never deleted.

    The brief is explicit that expired or invalid rows are archived or
    quarantined rather than deleted unless deletion is separately approved. The
    row keeps its UNKNOWN deadline state, which stays true — nobody ever
    established a date — while EXPIRED status moves it out of the queue and
    into the archive view.
    """
    row = _load_for_decision(db, opportunity_id)
    db.execute(
        update(Opportunity)
        .where(Opportunity.id == row.id)
        .values(
            status=Status.EXPIRED,
            deadline_confidence=CONFIDENCE_HUMAN,
            deadline_checked_at=datetime.now(timezone.utc),
        )
    )
    return {"id": row.id, "deadline_state": row.deadline_state,
            "status": Status.EXPIRED.value, "reviewer": reviewer}


def is_human_decided(row) -> bool:
    """Has a person ruled on this row's deadline?

    The guard an automatic re-assessment checks before touching a row. Without
    it, the next crawl re-parses the same unreadable text, fails again, and
    overwrites a reviewer's decision with UNKNOWN — which would make the queue
    refill with rows someone had already cleared, and quietly teach everyone
    that reviewing them is pointless.
    """
    return (getattr(row, "deadline_confidence", "") or "") in PROTECTED_CONFIDENCE
