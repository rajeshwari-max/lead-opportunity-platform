"""Company-level eligibility, recommendation, history and feedback API."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import COOKIE_NAME, current_user, validate_origin
from app.core.config import settings
from app.database.db import get_db
from app.database.models import ExperienceEvent, HumanFeedback, Opportunity
from app.services.actionable import strict_actionable_clause
from app.services.company_intelligence import (
    ARCHETYPES, BRANDS, DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS, DEVSOL_VERTICALS,
    HistoricalIndex, analyze_opportunity, get_or_create_profile,
    learning_summary, profile_dict, update_profile,
)

router = APIRouter(prefix="/intelligence", tags=["Company intelligence"])


def user(request: Request, db: Session = Depends(get_db)) -> dict:
    identity = current_user(request.cookies.get(COOKIE_NAME), db)
    if not identity.get("authenticated"):
        raise HTTPException(401, "Sign in to view company intelligence")
    return identity


def admin(identity: dict = Depends(user)) -> dict:
    if not identity.get("is_admin"):
        raise HTTPException(403, "Administrator access required")
    return identity


def writable(request: Request) -> None:
    validate_origin(request)
    if settings.read_only:
        raise HTTPException(403, "Company intelligence is read-only on this server")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompanyProfileInput(StrictModel):
    company_name: str = Field(default="", max_length=256)
    countries_of_operation: list[str] = Field(default_factory=list, max_length=100)
    industries: list[str] = Field(default_factory=list, max_length=100)
    sectors: list[str] = Field(default_factory=list, max_length=100)
    focus_areas: list[str] = Field(default_factory=list, max_length=100)
    organization_types: list[str] = Field(default_factory=list, max_length=30)
    company_size: str = Field(default="", max_length=128)
    years_of_operation: int | None = Field(default=None, ge=0, le=500)
    capabilities: list[str] = Field(default_factory=list, max_length=200)
    services: list[str] = Field(default_factory=list, max_length=200)
    project_types: list[str] = Field(default_factory=list, max_length=100)
    target_beneficiaries: list[str] = Field(default_factory=list, max_length=100)
    geographic_focus: list[str] = Field(default_factory=list, max_length=100)
    certifications: list[str] = Field(default_factory=list, max_length=100)
    partnership_types: list[str] = Field(default_factory=list, max_length=100)
    funding_types_of_interest: list[str] = Field(default_factory=list, max_length=100)
    recommendation_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    recommendation_thresholds: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    @field_validator(
        "countries_of_operation", "industries", "sectors", "focus_areas",
        "organization_types", "capabilities", "services", "project_types",
        "target_beneficiaries", "geographic_focus", "certifications",
        "partnership_types", "funding_types_of_interest",
    )
    @classmethod
    def bounded_strings(cls, values: list[str]) -> list[str]:
        if any(len(value) > 200 for value in values):
            raise ValueError("Profile entries must be 200 characters or shorter")
        return values


@router.get("/profile", dependencies=[Depends(admin)])
def company_profile(db: Session = Depends(get_db)) -> dict:
    return profile_dict(get_or_create_profile(db))


@router.put("/profile", dependencies=[Depends(writable)])
def save_company_profile(body: CompanyProfileInput, identity: dict = Depends(admin),
                         db: Session = Depends(get_db)) -> dict:
    try:
        profile = update_profile(db, body.model_dump(), identity.get("email") or "local-admin")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return profile_dict(profile)


@router.get("/opportunities/{opportunity_id}")
def opportunity_intelligence(opportunity_id: int, _identity: dict = Depends(user),
                             db: Session = Depends(get_db)) -> dict:
    opportunity = db.get(Opportunity, opportunity_id)
    if not opportunity or opportunity.unique_id.startswith("merged:"):
        raise HTTPException(404, "Opportunity not found")
    return analyze_opportunity(db, opportunity)


class FeedbackInput(StrictModel):
    decision: Literal["accept", "reject", "correct"]
    corrected_verticals: list[str] = Field(default_factory=list, max_length=6)
    corrected_brands: list[str] = Field(default_factory=list, max_length=7)
    corrected_archetypes: list[str] = Field(default_factory=list, max_length=2)
    eligibility_override: Literal["", "HIGH", "MEDIUM", "LOW", "UNKNOWN"] = ""
    reason: str = Field(default="", max_length=1000)
    comment: str = Field(default="", max_length=10000)

    @field_validator("corrected_verticals")
    @classmethod
    def valid_verticals(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(DEVSOL_VERTICALS))
        if unknown:
            raise ValueError("Unknown Devsol vertical(s): " + ", ".join(unknown))
        return list(dict.fromkeys(values))

    @field_validator("corrected_brands")
    @classmethod
    def valid_brands(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(BRANDS))
        if unknown:
            raise ValueError("Unknown brand(s): " + ", ".join(unknown))
        return list(dict.fromkeys(values))

    @field_validator("corrected_archetypes")
    @classmethod
    def valid_archetypes(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(ARCHETYPES))
        if unknown:
            raise ValueError("Unknown archetype(s): " + ", ".join(unknown))
        return list(dict.fromkeys(values))


@router.post("/opportunities/{opportunity_id}/feedback", dependencies=[Depends(writable)])
def feedback(opportunity_id: int, body: FeedbackInput, request: Request,
             identity: dict = Depends(user), db: Session = Depends(get_db)) -> dict:
    opportunity = db.get(Opportunity, opportunity_id)
    if not opportunity or opportunity.unique_id.startswith("merged:"):
        raise HTTPException(404, "Opportunity not found")
    classification_fields = {
        "corrected_verticals", "corrected_brands", "corrected_archetypes",
    }
    if body.decision == "correct" and not (
        body.model_fields_set & classification_fields or body.eligibility_override
    ):
        raise HTTPException(422, "A correction must include a changed classification or eligibility result")
    def current(value: str | None) -> list[str]:
        return [part.strip() for part in (value or "").split(",") if part.strip()]

    fields = body.model_fields_set
    classification_changed = bool(fields & classification_fields)
    brands = (list(body.corrected_brands) if "corrected_brands" in fields
              else current(opportunity.brands))
    archetypes = (list(body.corrected_archetypes) if "corrected_archetypes" in fields
                  else current(opportunity.archetypes))
    verticals = (list(body.corrected_verticals) if "corrected_verticals" in fields
                 else current(opportunity.verticals))
    if verticals:
        if "CMS" not in brands:
            brands.append("CMS")
        if "Devsol" not in archetypes:
            archetypes.append("Devsol")
    if "Social Business" in archetypes:
        if "CMS" not in brands:
            brands.append("CMS")
        # Compatibility with the current dashboard filter contract.
        if "Social Business" not in verticals:
            verticals.append("Social Business")
    elif "corrected_archetypes" in fields:
        verticals = [value for value in verticals if value != "Social Business"]
    row = HumanFeedback(
        opportunity_id=opportunity_id,
        owner=identity.get("email") or "local-admin",
        decision=body.decision,
        corrected_verticals=json.dumps(verticals),
        corrected_brands=json.dumps(brands),
        corrected_archetypes=json.dumps(archetypes),
        eligibility_override=body.eligibility_override,
        reason=body.reason.strip(), comment=body.comment.strip(),
    )
    db.add(row)
    if body.decision == "correct" and classification_changed:
        opportunity.verticals = ", ".join(verticals)
        opportunity.brands = ", ".join(brands)
        opportunity.archetypes = ", ".join(archetypes)
        opportunity.verticals_source = "human"
        opportunity.verticals_labeled_by = identity.get("email") or "local-admin"
        opportunity.verticals_labeled_at = datetime.now(timezone.utc)
        opportunity.classification_source = "human"
        opportunity.classification_status = "classified" if (verticals or brands or archetypes) else "unclassified"
        opportunity.classification_version = "human-feedback"
        opportunity.classified_at = datetime.now(timezone.utc)
    db.add(ExperienceEvent(
        opportunity_id=opportunity_id, event_type="human_feedback",
        actor=identity.get("email") or "local-admin",
        reviewer_decision=body.decision, reason=body.reason.strip(),
        payload=json.dumps(body.model_dump(), ensure_ascii=False),
    ))
    db.commit()
    return {"id": row.id, "decision": row.decision, "stored": True,
            "human_labels_protected": body.decision == "correct"}


@router.get("/learning", dependencies=[Depends(admin)])
def learning(db: Session = Depends(get_db)) -> dict:
    return learning_summary(db)


class RecalculateInput(StrictModel):
    limit: int = Field(default=500, ge=1, le=5000)


@router.post("/recalculate", dependencies=[Depends(writable)])
def recalculate(body: RecalculateInput, _identity: dict = Depends(admin),
                db: Session = Depends(get_db)) -> dict:
    opportunities = list(db.scalars(
        select(Opportunity).where(strict_actionable_clause())
        .order_by(Opportunity.deadline, Opportunity.id).limit(body.limit)
    ))
    index = HistoricalIndex.from_db(db)
    for number, opportunity in enumerate(opportunities, 1):
        analyze_opportunity(db, opportunity, index=index, commit=False)
        if number % 100 == 0:
            db.commit()
    db.commit()
    return {"processed": len(opportunities), "historical_leads": len(index.rows)}
