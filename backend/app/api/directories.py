"""Team-readable directories; mutations require an administrator."""
from datetime import date, datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.accounts import administrator
from app.api.workspace import personal, private_cache
from app.database.db import get_db
from app.database.models import DirectoryEntry, DirectoryEvent, ScrapeRun
from app.services.directories import audit, entry_data, entry_key, refresh_keywords
from app.services.verticals import VERTICALS
from app.services.actionable import application_today

Kind = Literal["keyword", "website", "organisation"]
router = APIRouter(prefix="/directories", tags=["Directories"], dependencies=[Depends(personal), Depends(private_cache)])


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    scope: str = Field(default="", max_length=256)
    url: str = Field(default="", max_length=2048)
    institution: str = Field(default="", max_length=256)
    location: str = Field(default="", max_length=256)
    role: Literal["", "whitelist", "resource"] = ""
    status: Literal["active", "review", "paused", "dropped"] = "active"
    notes: str = Field(default="", max_length=10000)
    review_date: date | None = None

    @field_validator("name", "scope", "institution", "location")
    @classmethod
    def trim(cls, value):
        return value.strip()

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("url")
    @classmethod
    def web_url(cls, value):
        from pydantic import HttpUrl, TypeAdapter
        return str(TypeAdapter(HttpUrl).validate_python(value)) if value else ""


def validate_entry(kind, body, db, current=None):
    if kind == "keyword" and body.scope not in VERTICALS:
        raise HTTPException(422, "Choose a canonical vertical for this keyword")
    if kind == "keyword" and len(body.name) < 4:
        raise HTTPException(422, "Keywords must contain at least four characters")
    if kind == "organisation" and not body.role:
        raise HTTPException(422, "Choose whitelist or resource organisation")
    if body.status == "dropped" and not body.notes.strip():
        raise HTTPException(422, "Record the review reason before dropping an entry")
    if kind == "website" and body.scope:
        from app.scrapers.registry import SCRAPER_REGISTRY
        if body.scope not in SCRAPER_REGISTRY:
            raise HTTPException(422, "Scraper key must name an installed scraper; leave it blank for a candidate website")
        duplicate = db.scalar(select(DirectoryEntry).where(DirectoryEntry.kind == kind, DirectoryEntry.scope == body.scope))
        if duplicate and (current is None or duplicate.id != current.id):
            raise HTTPException(409, "This scraper already has a directory entry")
    if kind == "website" and current and current.scope and body.scope != current.scope:
        raise HTTPException(422, "The installed scraper key is fixed; add another website entry for a different source")


@router.get("/{kind}")
def listing(kind: Kind, db: Session = Depends(get_db)):
    rows = db.scalars(select(DirectoryEntry).where(DirectoryEntry.kind == kind).order_by(DirectoryEntry.name)).all()
    stats = {}
    if kind == "website":
        for key, count, found, saved, last in db.execute(select(
            ScrapeRun.source_key, func.count(), func.sum(ScrapeRun.found),
            func.sum(ScrapeRun.saved), func.max(ScrapeRun.started_at)).group_by(ScrapeRun.source_key)):
            stats[key] = {"runs": count, "found": found or 0, "saved": saved or 0, "last_attempt": last}
    return [{**entry_data(r), "review_due": r.review_date is not None and r.review_date <= application_today(),
             "performance": stats.get(r.scope, {"runs": 0, "found": 0, "saved": 0, "last_attempt": None})}
            for r in rows]


@router.post("/{kind}", status_code=201)
def create(kind: Kind, body: EntryInput, identity=Depends(administrator), db: Session = Depends(get_db)):
    validate_entry(kind, body, db)
    row = DirectoryEntry(kind=kind, key=entry_key(kind, body.name, body.scope), **body.model_dump())
    db.add(row)
    try:
        db.flush()
        audit(db, row, "created", identity.get("email", ""), body.notes, body.model_dump())
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This entry already exists")
    if kind == "keyword":
        refresh_keywords(db)
    return entry_data(row)


@router.put("/{kind}/{entry_id}")
def update(kind: Kind, entry_id: int, body: EntryInput, identity=Depends(administrator), db: Session = Depends(get_db)):
    row = db.get(DirectoryEntry, entry_id)
    if not row or row.kind != kind:
        raise HTTPException(404, "Directory entry not found")
    validate_entry(kind, body, db, row)
    before = entry_data(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    row.key = entry_key(kind, body.name, body.scope)
    audit(db, row, "updated", identity.get("email", ""), body.notes, {"before": before, "after": body.model_dump()})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This entry already exists")
    if kind == "keyword":
        refresh_keywords(db)
    return entry_data(row)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pinged", "reviewed", "no_response", "response_received"]
    note: str = Field(min_length=1, max_length=10000)
    review_date: date | None = None


@router.post("/{kind}/{entry_id}/events")
def record_review(kind: Kind, entry_id: int, body: ReviewInput, identity=Depends(administrator), db: Session = Depends(get_db)):
    row = db.get(DirectoryEntry, entry_id)
    if not row or row.kind != kind:
        raise HTTPException(404, "Directory entry not found")
    if not body.note.strip():
        raise HTTPException(422, "Add a review note")
    if body.action == "reviewed":
        row.reviewed_at = datetime.now(timezone.utc)
    row.review_date = body.review_date
    audit(db, row, body.action, identity.get("email", ""), body.note)
    db.commit()
    return entry_data(row)


@router.get("/{kind}/{entry_id}/events")
def history(kind: Kind, entry_id: int, db: Session = Depends(get_db)):
    row = db.get(DirectoryEntry, entry_id)
    if not row or row.kind != kind:
        raise HTTPException(404, "Directory entry not found")
    return [{"action": r.action, "note": r.note, "actor": r.actor, "created_at": r.created_at}
            for r in db.scalars(select(DirectoryEvent).where(DirectoryEvent.entry_id == entry_id).order_by(DirectoryEvent.id.desc()))]
