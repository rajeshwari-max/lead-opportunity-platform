"""Small, append-only user activity audit helpers.

Events are written by authenticated server endpoints.  The browser never
supplies the actor, which keeps the activity source attributable and prevents
one user from claiming another user's actions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import UserActivityEvent


def record_user_event(
    db: Session,
    actor: str,
    event_type: str,
    *,
    opportunity_id: int | None = None,
    journey_id: int | None = None,
    source: str = "server_api",
    details: dict[str, Any] | None = None,
) -> UserActivityEvent:
    row = UserActivityEvent(
        actor=(actor or "").strip().lower(),
        event_type=event_type,
        opportunity_id=opportunity_id,
        journey_id=journey_id,
        source=source,
        details=json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
    )
    db.add(row)
    return row


def user_event_summary(db: Session, actor: str) -> dict:
    actor = actor.strip().lower()
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(
            UserActivityEvent.event_type,
            func.count(UserActivityEvent.id),
            func.max(UserActivityEvent.occurred_at),
        )
        .where(UserActivityEvent.actor == actor)
        .group_by(UserActivityEvent.event_type)
    ).all()
    all_events = list(db.scalars(
        select(UserActivityEvent)
        .where(UserActivityEvent.actor == actor)
        .order_by(UserActivityEvent.occurred_at.desc(), UserActivityEvent.id.desc())
        .limit(20)
    ))
    return {
        "total": sum(count for _, count, _ in rows),
        "last_7_days": db.scalar(select(func.count(UserActivityEvent.id)).where(
            UserActivityEvent.actor == actor,
            UserActivityEvent.occurred_at >= now - timedelta(days=7),
        )) or 0,
        "last_30_days": db.scalar(select(func.count(UserActivityEvent.id)).where(
            UserActivityEvent.actor == actor,
            UserActivityEvent.occurred_at >= now - timedelta(days=30),
        )) or 0,
        "by_type": {event_type: count for event_type, count, _ in rows},
        "last_activity_at": max((latest for _, _, latest in rows if latest), default=None),
        "recent": [
            {
                "id": row.id,
                "event_type": row.event_type,
                "opportunity_id": row.opportunity_id,
                "journey_id": row.journey_id,
                "source": row.source,
                "occurred_at": row.occurred_at,
            }
            for row in all_events
        ],
        "tracking_note": (
            "Append-only server events. Actor comes from the authenticated session; "
            "tracking begins when this feature is deployed."
        ),
    }
