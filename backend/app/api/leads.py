"""Account-owned saved leads inside the dashboard; explicit admin oversight."""
import json
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from app.api import workspace as storage
from app.api.accounts import administrator
from app.database.db import get_db
from app.database.models import ApplicationJourney, TeamMember, DashboardPreferences, LeadActivity, Opportunity

router = APIRouter(prefix="/my-leads", dependencies=[Depends(storage.private_cache)])

class ActivityInput(BaseModel):
    opportunity_id: int = Field(gt=0)
    action: Literal["viewed", "reviewed", "source_opened"]

@router.post("/activity")
def record_activity(body: ActivityInput, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    opportunity = db.get(Opportunity, body.opportunity_id)
    if not opportunity or opportunity.unique_id.startswith("merged:"):
        raise HTTPException(404, "Opportunity unavailable")
    values = {body.action + "_at": datetime.now(timezone.utc)}
    statement = insert(LeadActivity).values(owner=owner, opportunity_id=body.opportunity_id, **values)
    db.execute(statement.on_conflict_do_update(index_elements=["owner", "opportunity_id"], set_=values))
    db.commit()
    return {"recorded": True}

def activity_summary(owner, db):
    rows = db.scalars(select(LeadActivity).where(LeadActivity.owner == owner)).all()
    counts = {key: sum(getattr(r, key + "_at") is not None for r in rows) for key in ("viewed", "reviewed", "source_opened")}
    recent = sorted(rows, key=lambda r: max(v for v in (r.viewed_at, r.reviewed_at, r.source_opened_at) if v), reverse=True)[:10]
    return {**counts, "recent": [{"opportunity": storage.opportunity_data(db.get(Opportunity, r.opportunity_id)),
        "viewed_at": r.viewed_at, "reviewed_at": r.reviewed_at, "source_opened_at": r.source_opened_at} for r in recent]}

@router.get("/activity")
def my_activity(owner=Depends(storage.personal), db: Session=Depends(get_db)):
    return activity_summary(owner, db)

class Preferences(BaseModel):
    filters: dict = Field(default_factory=dict)

@router.get("/preferences")
def preferences(owner=Depends(storage.personal), db: Session=Depends(get_db)):
    row = db.get(DashboardPreferences, owner)
    return {"filters": json.loads(row.filters) if row else {}}

@router.put("/preferences")
def save_preferences(body: Preferences, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    encoded = json.dumps(body.filters)
    if len(encoded) > 16000:
        raise HTTPException(422, "Too many filter preferences")
    row = db.get(DashboardPreferences, owner) or DashboardPreferences(owner=owner)
    row.filters = encoded
    db.add(row)
    db.commit()
    return {"filters": body.filters}

@router.get("")
def saved(owner=Depends(storage.personal), db: Session=Depends(get_db)):
    return [storage.journey_data(row, db) for row in db.scalars(select(ApplicationJourney).where(ApplicationJourney.owner == owner, ApplicationJourney.saved.is_(True)).order_by(ApplicationJourney.updated_at.desc()))]

@router.post("")
def save(body: storage.Track, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    result = storage.track(body, owner, db)
    row = storage.owned(db, owner, result["id"])
    row.saved = True
    db.commit()
    return result

@router.delete("/{lead_id}")
def unsave(lead_id: int, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    row = storage.owned(db, owner, lead_id)
    row.saved = False
    db.commit()
    return {"saved": False}

@router.put("/{lead_id}")
def update(lead_id: int, body: storage.JourneyInput, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    return storage.update_journey(lead_id, body, owner, db)

@router.get("/{lead_id}/history")
def history(lead_id: int, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    return storage.details(lead_id, owner, db)["events"]

@router.get("/team/activity", dependencies=[Depends(administrator)])
def activity(db: Session=Depends(get_db)):
    # Include users with no saved leads, so admins can see adoption too.
    rows = db.scalars(select(ApplicationJourney).where(ApplicationJourney.saved.is_(True)).order_by(ApplicationJourney.updated_at.desc())).all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.owner, []).append(storage.journey_data(row, db))
    return [{"email": m.email, "name": m.name, "active": m.active,
             "activity": activity_summary(m.email.lower(), db),
             "leads": grouped.get(m.email.lower(), [])}
            for m in db.scalars(select(TeamMember).order_by(TeamMember.name))]

@router.get("/team/history/{lead_id}", dependencies=[Depends(administrator)])
def team_history(lead_id: int, db: Session=Depends(get_db)):
    row = db.get(ApplicationJourney, lead_id)
    if not row:
        raise HTTPException(404, "Saved lead not found")
    return storage.details(lead_id, row.owner, db)["events"]
