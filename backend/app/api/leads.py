"""Account-owned saved leads inside the dashboard; explicit admin oversight."""
import json
from datetime import date, datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from app.api import workspace as storage
from app.api.accounts import administrator
from app.database.db import get_db
from app.database.models import (ApplicationJourney, TeamMember, DashboardPreferences,
    LeadActivity, Opportunity, Category, Status, LeadMetadata, DirectoryEntry,
    JourneyEvent, ExperienceEvent)

router = APIRouter(prefix="/my-leads", dependencies=[Depends(storage.private_cache)])


class ManualLeadInput(storage.Input):
    title: str = Field(min_length=3, max_length=1000)
    organization: str = Field(default="", max_length=512)
    opportunity_url: str = Field(default="", max_length=2048)
    category: Category = Category.OTHER
    summary: str = Field(default="", max_length=20000)
    country: str = Field(default="", max_length=128)
    location: str = Field(default="", max_length=512)
    eligibility: str = Field(default="", max_length=10000)
    funding_amount: str = Field(default="", max_length=256)
    deadline: date | None = None
    rolling: bool = False
    next_action: str = Field(default="", max_length=2000)
    next_action_date: date | None = None
    resource_organisation_id: int | None = Field(default=None, gt=0)
    award_type: Literal["unknown", "monetary", "recognition", "both"] = "unknown"
    award_evidence: str = Field(default="", max_length=10000)


@router.post("/manual", status_code=201)
def manual_lead(body: ManualLeadInput, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    from pydantic import HttpUrl, TypeAdapter, ValidationError
    from app.services.deduplication import make_unique_id
    from app.services.ml_hierarchy import classify_hierarchy
    from app.services.actionable import application_today
    from sqlalchemy.exc import IntegrityError
    title = body.title.strip()
    if len(title) < 3:
        raise HTTPException(422, "Enter a lead title")
    if body.rolling and body.deadline:
        raise HTTPException(422, "Choose either a deadline or rolling applications")
    url = body.opportunity_url.strip()
    if url:
        try:
            url = str(TypeAdapter(HttpUrl).validate_python(url))
        except ValidationError:
            raise HTTPException(422, "Enter a valid HTTP or HTTPS source URL")
    if body.award_type != "unknown" and body.category != Category.AWARD:
        raise HTTPException(422, "Award benefit applies only to Award leads")
    if body.award_type != "unknown" and not body.award_evidence.strip():
        raise HTTPException(422, "Record evidence for the award benefit")
    if body.resource_organisation_id:
        resource = db.get(DirectoryEntry, body.resource_organisation_id)
        if not resource or resource.kind != "organisation" or resource.role != "resource" or resource.status != "active":
            raise HTTPException(422, "Choose an active resource organisation")
    uid = make_unique_id(title, body.organization, body.deadline, url)
    opportunity = db.scalar(select(Opportunity).where(Opportunity.unique_id == uid))
    reused = opportunity is not None
    if not opportunity:
        prediction = classify_hierarchy(title, body.summary, body.eligibility,
            body.organization, body.location, body.country, category_hint=body.category)
        opportunity = Opportunity(unique_id=uid, title=title, organization=body.organization.strip(),
            opportunity_url=url, source_website="Manual entry", summary=body.summary,
            country=body.country, location=body.location, eligibility=body.eligibility,
            funding_amount=body.funding_amount, category=body.category, deadline=body.deadline,
            deadline_state="dated" if body.deadline else "rolling" if body.rolling else "unknown",
            deadline_raw=body.deadline.isoformat() if body.deadline else "Rolling" if body.rolling else "",
            deadline_confidence="human", status=Status.EXPIRED if body.deadline and body.deadline < application_today() else Status.ACTIVE,
            verticals=", ".join(prediction.verticals), brands=", ".join(prediction.brands),
            archetypes=", ".join(prediction.archetypes), classification_status=prediction.status,
            classification_source=prediction.source, classification_version=prediction.version,
            classification_evidence=prediction.evidence_json())
        db.add(opportunity)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            opportunity = db.scalar(select(Opportunity).where(Opportunity.unique_id == uid))
            if opportunity is None:
                raise
            reused = True
    row = db.scalar(select(ApplicationJourney).where(ApplicationJourney.owner == owner, ApplicationJourney.opportunity_id == opportunity.id))
    if not row:
        row = ApplicationJourney(owner=owner, opportunity_id=opportunity.id,
            next_action=body.next_action, next_action_date=body.next_action_date)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "This lead was just saved. Refresh My leads.")
        db.add(JourneyEvent(journey_id=row.id, stage="Saved", note="Added manually" if not reused else "Saved existing catalogue lead"))
    row.saved = True
    if not reused:
        db.add(LeadMetadata(opportunity_id=opportunity.id, entered_by=owner,
            award_type=body.award_type, award_evidence=body.award_evidence,
            resource_organisation_id=body.resource_organisation_id))
        db.add(ExperienceEvent(opportunity_id=opportunity.id, event_type="manual_entry", actor=owner))
    db.commit()
    return {**storage.journey_data(row, db), "reused_existing": reused}


@router.get("/due")
def due_leads(owner=Depends(storage.personal), db: Session=Depends(get_db)):
    from app.services.actionable import application_today
    return [storage.journey_data(r, db) for r in db.scalars(select(ApplicationJourney).where(
        ApplicationJourney.owner == owner, ApplicationJourney.saved.is_(True),
        ApplicationJourney.next_action_date <= application_today()).order_by(ApplicationJourney.next_action_date))]


class AwardInput(storage.Input):
    award_type: Literal["unknown", "monetary", "recognition", "both"]
    award_evidence: str = Field(default="", max_length=10000)


@router.get("/awards/{opportunity_id}")
def award_details(opportunity_id: int, owner=Depends(storage.personal), db: Session=Depends(get_db)):
    if not db.get(Opportunity, opportunity_id):
        raise HTTPException(404, "Opportunity not found")
    row = db.get(LeadMetadata, opportunity_id)
    return {"award_type": row.award_type if row else "unknown", "award_evidence": row.award_evidence if row else ""}


@router.put("/awards/{opportunity_id}")
def update_award(opportunity_id: int, body: AwardInput, identity=Depends(administrator), db: Session=Depends(get_db)):
    opportunity = db.get(Opportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(404, "Opportunity not found")
    if opportunity.category != Category.AWARD:
        raise HTTPException(422, "Only Award opportunities have an award benefit")
    if body.award_type != "unknown" and not body.award_evidence.strip():
        raise HTTPException(422, "Record evidence for the award benefit")
    row = db.get(LeadMetadata, opportunity_id) or LeadMetadata(opportunity_id=opportunity_id)
    row.award_type, row.award_evidence = body.award_type, body.award_evidence.strip()
    db.add(row)
    db.add(ExperienceEvent(opportunity_id=opportunity_id, event_type="award_review", actor=identity.get("email", ""), payload=json.dumps(body.model_dump())))
    db.commit()
    return body.model_dump()

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
