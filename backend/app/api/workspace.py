"""Private application history; every resource is resolved through its owner."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import quote, unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import COOKIE_NAME, auth_required, current_user
from app.core.config import settings
from app.database.db import get_db
from app.database.models import (ApplicationJourney, JourneyAttachment, JourneyEvent,
    Opportunity, TeamMember, WorkspaceContact, WorkspaceCredential, WorkspaceProfile)
from app.services.actionable import strict_actionable_clause

def private_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/workspace", tags=["Personal workspace"], dependencies=[Depends(private_cache)])
STAGES = ["Saved", "Preparing", "Applied", "Shortlisted", "Accepted", "Unsuccessful", "Withdrawn"]
FACTORS = ["Funder relationship", "Relevant experience", "Registration/licence", "Eligibility gap", "Proposal quality", "Budget", "Competition", "Other"]


def account(request: Request, db: Session = Depends(get_db)) -> str:
    if request.method not in ("GET", "HEAD"):
        if settings.read_only:
            raise HTTPException(403, "Workspace is read-only")
        from app.core.auth import validate_origin
        validate_origin(request)
    if not auth_required():
        return "local-development"
    user = current_user(request.cookies.get(COOKIE_NAME), db)
    owner = (user.get("email") or "").strip().lower()
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == owner, TeamMember.active.is_(True)))
    if not user.get("authenticated") or not owner or not member:
        raise HTTPException(401, "Sign in with an active team account")
    return owner


def unlocked(request: Request, owner: str, db: Session) -> bool:
    return bool(current_user(request.cookies.get(COOKIE_NAME), db)["authenticated"])


def personal(owner: str = Depends(account)) -> str:
    return owner


def admin(request: Request, owner: str = Depends(account), db: Session = Depends(get_db)):
    if not current_user(request.cookies.get(COOKIE_NAME), db).get("is_admin"):
        raise HTTPException(403, "Admin access has not been granted to this account")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/session")
def session(request: Request, owner: str = Depends(account), db: Session = Depends(get_db)):
    return {"owner": owner, "unlocked": unlocked(request, owner, db), "configured": db.get(WorkspaceCredential, owner) is not None,
            "development": not auth_required(), "read_only": settings.read_only}


@router.get("/admin/summary", dependencies=[Depends(admin)])
def team_summary(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(ApplicationJourney.stage, func.count()).group_by(ApplicationJourney.stage)).all())
    return {"stages": counts, "workspaces": db.scalar(select(func.count()).select_from(WorkspaceProfile))}


class ProfileInput(Input):
    title: str = Field(default="My opportunity journey", min_length=1, max_length=120)
    accent: Literal["blue", "teal", "violet"] = "blue"
    keywords: str = Field(default="", max_length=2000)
    verticals: str = Field(default="", max_length=2000)
    countries: str = Field(default="", max_length=2000)
    experience: str = Field(default="", max_length=10000)
    registrations: str = Field(default="", max_length=10000)


def profile_data(row):
    return {key: getattr(row, key) for key in ProfileInput.model_fields} if row else ProfileInput().model_dump()


@router.get("/profile")
def profile(owner: str = Depends(personal), db: Session = Depends(get_db)):
    return profile_data(db.get(WorkspaceProfile, owner))


@router.put("/profile")
def save_profile(body: ProfileInput, owner: str = Depends(personal), db: Session = Depends(get_db)):
    row = db.get(WorkspaceProfile, owner) or WorkspaceProfile(owner=owner)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return profile_data(row)


def owned(db, owner, journey_id):
    row = db.scalar(select(ApplicationJourney).where(ApplicationJourney.id == journey_id, ApplicationJourney.owner == owner))
    if not row:
        raise HTTPException(404, "Application not found")
    return row


def opportunity_data(row):
    if not row:
        return {"title": "Opportunity no longer available"}
    return {"id": row.id, "title": row.title, "organization": row.organization, "deadline": row.deadline,
            "category": row.category.value, "country": row.country, "verticals": row.verticals,
            "summary": row.summary, "url": row.opportunity_url, "source": row.source_website}


def journey_data(row, db):
    return {"id": row.id, "opportunity": opportunity_data(db.get(Opportunity, row.opportunity_id)),
            "stage": row.stage, "notes": row.notes, "factors": json.loads(row.factors),
            "next_action": row.next_action, "next_action_date": row.next_action_date, "updated_at": row.updated_at}


@router.get("/journeys")
def journeys(owner: str = Depends(personal), db: Session = Depends(get_db)):
    return [journey_data(r, db) for r in db.scalars(select(ApplicationJourney).where(ApplicationJourney.owner == owner).order_by(ApplicationJourney.updated_at.desc()))]


class Track(Input):
    opportunity_id: int = Field(gt=0)


@router.post("/journeys")
def track(body: Track, owner: str = Depends(personal), db: Session = Depends(get_db)):
    opportunity = db.get(Opportunity, body.opportunity_id)
    if not opportunity or opportunity.unique_id.startswith("merged:"):
        raise HTTPException(404, "Opportunity unavailable; find the current catalogue entry")
    row = db.scalar(select(ApplicationJourney).where(ApplicationJourney.owner == owner, ApplicationJourney.opportunity_id == body.opportunity_id))
    if not row:
        row = ApplicationJourney(owner=owner, opportunity_id=body.opportunity_id)
        db.add(row)
        try:
            db.flush()
            db.add(JourneyEvent(journey_id=row.id, stage="Saved", note="Added to my journey"))
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(select(ApplicationJourney).where(ApplicationJourney.owner == owner, ApplicationJourney.opportunity_id == body.opportunity_id))
            if not row:
                raise
    return journey_data(row, db)


class JourneyInput(Input):
    stage: Literal["Saved", "Preparing", "Applied", "Shortlisted", "Accepted", "Unsuccessful", "Withdrawn"]
    notes: str = Field(default="", max_length=20000)
    factors: list[str] = Field(default_factory=list, max_length=8)
    next_action: str = Field(default="", max_length=2000)
    next_action_date: date | None = None


@router.put("/journeys/{journey_id}")
def update_journey(journey_id: int, body: JourneyInput, owner: str = Depends(personal), db: Session = Depends(get_db)):
    row = owned(db, owner, journey_id)
    if any(f not in FACTORS for f in body.factors):
        raise HTTPException(422, "Unknown outcome factor")
    changed = row.stage != body.stage
    for key, value in body.model_dump().items():
        setattr(row, key, json.dumps(value) if key == "factors" else value)
    row.updated_at = datetime.now(timezone.utc)
    db.add(JourneyEvent(journey_id=row.id, stage=row.stage, note="Stage changed" if changed else "Application details updated"))
    db.commit()
    return journey_data(row, db)


@router.get("/journeys/{journey_id}/details")
def details(journey_id: int, owner: str = Depends(personal), db: Session = Depends(get_db)):
    owned(db, owner, journey_id)
    return {"events": [{"id": r.id, "stage": r.stage, "note": r.note, "created_at": r.created_at} for r in db.scalars(select(JourneyEvent).where(JourneyEvent.journey_id == journey_id).order_by(JourneyEvent.id.desc()))],
            "attachments": [{"id": r.id, "filename": r.filename} for r in db.execute(select(JourneyAttachment.id, JourneyAttachment.filename).where(JourneyAttachment.journey_id == journey_id))]}


@router.post("/journeys/{journey_id}/attachments")
async def upload(journey_id: int, request: Request, owner: str = Depends(personal), db: Session = Depends(get_db)):
    owned(db, owner, journey_id)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 5 * 1024 * 1024:
            raise HTTPException(413, "Each attachment must be 5 MB or smaller")
    if not chunks:
        raise HTTPException(422, "File is empty")
    # Serialize quota check and insert on SQLite, including concurrent uploads.
    db.rollback()
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    owned(db, owner, journey_id)
    count = db.scalar(select(func.count()).select_from(JourneyAttachment).where(JourneyAttachment.journey_id == journey_id))
    used = db.scalar(select(func.sum(func.length(JourneyAttachment.content))).join(ApplicationJourney, ApplicationJourney.id == JourneyAttachment.journey_id).where(ApplicationJourney.owner == owner)) or 0
    if count >= 10 or used + len(chunks) > 50 * 1024 * 1024:
        raise HTTPException(413, "Limit: 10 files per application and 50 MB per workspace")
    name = unquote(request.headers.get("x-filename", "attachment")).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable())[:240] or "attachment"
    row = JourneyAttachment(journey_id=journey_id, filename=name, content=bytes(chunks))
    db.add(row)
    db.add(JourneyEvent(journey_id=journey_id, stage=owned(db, owner, journey_id).stage, note="Attachment added: " + name))
    db.commit()
    return {"id": row.id, "filename": row.filename}


@router.get("/journeys/{journey_id}/attachments/{attachment_id}")
def download(journey_id: int, attachment_id: int, owner: str = Depends(personal), db: Session = Depends(get_db)):
    owned(db, owner, journey_id)
    row = db.scalar(select(JourneyAttachment).where(JourneyAttachment.id == attachment_id, JourneyAttachment.journey_id == journey_id))
    if not row:
        raise HTTPException(404, "File not found")
    return Response(row.content, media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row.filename, safe=""), "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.delete("/journeys/{journey_id}/attachments/{attachment_id}")
def delete_attachment(journey_id: int, attachment_id: int, owner: str = Depends(personal), db: Session = Depends(get_db)):
    owned(db, owner, journey_id)
    row = db.scalar(select(JourneyAttachment).where(JourneyAttachment.id == attachment_id, JourneyAttachment.journey_id == journey_id))
    if not row:
        raise HTTPException(404, "File not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


class ContactInput(Input):
    name: str = Field(min_length=1, max_length=200)
    organization: str = Field(default="", max_length=300)
    role: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    tags: str = Field(default="", max_length=2000)
    strength: Literal["Known", "Warm", "Strong"] = "Known"
    notes: str = Field(default="", max_length=10000)


@router.get("/contacts")
def contacts(owner: str = Depends(personal), db: Session = Depends(get_db)):
    return [{"id": r.id, **{k: getattr(r, k) for k in ContactInput.model_fields}} for r in db.scalars(select(WorkspaceContact).where(WorkspaceContact.owner == owner).order_by(WorkspaceContact.name))]


@router.post("/contacts")
def add_contact(body: ContactInput, owner: str = Depends(personal), db: Session = Depends(get_db)):
    row = WorkspaceContact(owner=owner, **body.model_dump())
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.put("/contacts/{contact_id}")
def edit_contact(contact_id: int, body: ContactInput, owner: str = Depends(personal), db: Session = Depends(get_db)):
    row = db.scalar(select(WorkspaceContact).where(WorkspaceContact.id == contact_id, WorkspaceContact.owner == owner))
    if not row:
        raise HTTPException(404, "Contact not found")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return {"id": row.id}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: int, owner: str = Depends(personal), db: Session = Depends(get_db)):
    row = db.scalar(select(WorkspaceContact).where(WorkspaceContact.id == contact_id, WorkspaceContact.owner == owner))
    if not row:
        raise HTTPException(404, "Contact not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


def terms(value):
    return [x.strip().casefold() for x in (value or "").split(",") if len(x.strip()) > 1][:30]


def rank(opportunity, profile, history, network):
    haystack = " ".join([opportunity.title, opportunity.summary or "", opportunity.verticals or ""]).casefold()
    reasons, score, matches = [], 0, []
    for field, text in [("keywords", haystack), ("verticals", (opportunity.verticals or "").casefold()), ("countries", (opportunity.country or "").casefold())]:
        matched = [t for t in terms(profile[field]) if t in text]
        if matched:
            score += min(len(matched), 3) * 10
            reasons.append(field.capitalize() + ": " + ", ".join(matched))
    for contact in network:
        organization_match = bool(contact.organization.strip()) and contact.organization.strip().casefold() == (opportunity.organization or "").strip().casefold()
        tag_match = [t for t in terms(contact.tags) if t in haystack]
        if organization_match or tag_match:
            matches.append({"id": contact.id, "name": contact.name, "organization": contact.organization,
                            "reason": "Same offering organisation" if organization_match else "Related expertise: " + ", ".join(tag_match)})
    if matches:
        score += 15
        reasons.append("Relevant contacts in your network; relationship does not confirm eligibility")
    key = ((opportunity.organization or "").strip().casefold(), opportunity.category.value)
    wins, total = history.get(key, (0, 0))
    if total >= 5:
        score += round(20 * wins / total)
        reasons.append(f"Your history with this organisation and type: {wins} accepted / {total} decided applications")
    return {"score": score, "reasons": reasons or ["No personal match yet; ordered by closing date"], "contacts": matches[:5], "history": {"accepted": wins, "decided": total}}


@router.get("/recommendations")
def recommendations(q: str = "", owner: str = Depends(personal), db: Session = Depends(get_db)):
    if len(q) > 200:
        raise HTTPException(422, "Search must be 200 characters or shorter")
    p = profile_data(db.get(WorkspaceProfile, owner))
    query = select(Opportunity).where(strict_actionable_clause())
    if q.strip():
        query = query.where(or_(Opportunity.title.contains(q.strip(), autoescape=True), Opportunity.organization.contains(q.strip(), autoescape=True)))
    candidates = db.scalars(query.order_by(Opportunity.deadline, Opportunity.id).limit(1000)).all()
    history = {}
    for stage, org, category in db.execute(select(ApplicationJourney.stage, Opportunity.organization, Opportunity.category).join(Opportunity, Opportunity.id == ApplicationJourney.opportunity_id).where(ApplicationJourney.owner == owner, ApplicationJourney.stage.in_(["Accepted", "Unsuccessful"]))):
        if not org.strip():
            continue
        key = (org.strip().casefold(), category.value)
        wins, total = history.get(key, (0, 0))
        history[key] = (wins + int(stage == "Accepted"), total + 1)
    network = db.scalars(select(WorkspaceContact).where(WorkspaceContact.owner == owner)).all()
    results = [{**opportunity_data(o), **rank(o, p, history, network)} for o in candidates]
    results.sort(key=lambda r: -r["score"])
    return {"items": results[:40], "considered": len(candidates), "method": "Explainable preference, network and outcome ranking; not a predicted probability. Searches rank up to 1,000 nearest-deadline active opportunities."}
