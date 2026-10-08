"""Focused contract tests for the shared company-intelligence layer.

These tests intentionally exercise both the pure scoring service and the HTTP
boundary.  The hierarchy classifier and the recommendation engine are separate
systems: a human correction must update/protect the former while remaining
available as evidence to the latter.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.intelligence import router as intelligence_router
from app.core.auth import COOKIE_NAME, make_session_token, password_version
from app.core.config import settings
from app.database.db import get_db
from app.database.models import (
    Base,
    Category,
    CompanyProfile,
    ExperienceEvent,
    HistoricalLead,
    HumanFeedback,
    Opportunity,
    OpportunityIntelligence,
    Status,
    TeamMember,
    WorkspaceCredential,
)
from app.services.company_intelligence import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    analyze_opportunity,
    eligibility_matches,
    get_or_create_profile,
    learning_summary,
    profile_dict,
    update_profile,
)


PASSWORD_HASH = "test-salt:test-hash"


@pytest.fixture()
def intelligence(monkeypatch):
    monkeypatch.setattr(settings, "personal_login", True)
    monkeypatch.setattr(settings, "approval_secret", "company-intelligence-test-secret")
    monkeypatch.setattr(settings, "read_only", False)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    for email, is_admin in (
        ("person@catalysts.org", False),
        ("admin@catalysts.org", True),
    ):
        session.add(TeamMember(
            name="Admin" if is_admin else "Person",
            email=email,
            keywords="",
            categories="",
            verticals="",
            active=True,
        ))
        session.add(WorkspaceCredential(
            owner=email,
            password_hash=PASSWORD_HASH,
            is_admin=is_admin,
        ))
    session.commit()

    app = FastAPI()
    app.include_router(intelligence_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield {"client": client, "session": session}
    session.close()


def _cookie(*, admin: bool) -> dict[str, str]:
    email = "admin@catalysts.org" if admin else "person@catalysts.org"
    name = "Admin" if admin else "Person"
    return {
        COOKIE_NAME: make_session_token(
            email,
            name,
            is_admin=admin,
            version=password_version(PASSWORD_HASH),
        )
    }


def _opportunity(session: Session, *, suffix: str = "1", **overrides) -> Opportunity:
    values = {
        "unique_id": f"intelligence-{suffix}",
        "title": "India Maternal Health Research Grant",
        "organization": "Health Foundation",
        "country": "India",
        "region": "South Asia",
        "location": "India",
        "funding_type": "Grant",
        "category": Category.GRANT,
        "verticals": "Health",
        "archetypes": "Devsol",
        "brands": "CMS",
        "summary": "Maternal health research and proposal development for NGOs.",
        "eligibility": "Registered NGOs in India with at least 5 years of operation.",
        "funding_amount": "USD 100,000",
        "deadline": date(2099, 12, 31),
        "deadline_state": "dated",
        "website": "https://example.org/call",
        "opportunity_url": "https://example.org/call/1",
        "source_website": "Example",
        "status": Status.ACTIVE,
    }
    values.update(overrides)
    row = Opportunity(**values)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _profile_payload(**overrides) -> dict:
    payload = {
        "company_name": "Catalyst Group",
        "countries_of_operation": [" India ", "India"],
        "industries": ["Development"],
        "sectors": ["Health"],
        "focus_areas": ["Maternal health"],
        "organization_types": ["Non-profit / NGO"],
        "company_size": "100-250",
        "years_of_operation": 20,
        "capabilities": ["Research"],
        "services": ["Proposal development"],
        "project_types": [],
        "target_beneficiaries": [],
        "geographic_focus": ["South Asia"],
        "certifications": ["ISO 9001"],
        "partnership_types": [],
        "funding_types_of_interest": ["Grant"],
        "recommendation_weights": dict(DEFAULT_WEIGHTS),
        "recommendation_thresholds": dict(DEFAULT_THRESHOLDS),
    }
    payload.update(overrides)
    return payload


def test_profile_is_admin_managed_versioned_and_validated(intelligence):
    client = intelligence["client"]
    session = intelligence["session"]

    assert client.get("/api/intelligence/profile").status_code == 401
    assert client.get(
        "/api/intelligence/profile", cookies=_cookie(admin=False)
    ).status_code == 403

    initial = client.get(
        "/api/intelligence/profile", cookies=_cookie(admin=True)
    )
    assert initial.status_code == 200
    assert initial.json()["version"] == 1
    assert initial.json()["recommendation_weights"] == DEFAULT_WEIGHTS

    updated = client.put(
        "/api/intelligence/profile",
        json=_profile_payload(),
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=True),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["countries_of_operation"] == ["India"]
    assert updated.json()["version"] == 2
    assert updated.json()["updated_by"] == "admin@catalysts.org"
    assert session.scalar(select(func.count()).select_from(CompanyProfile)) == 1

    invalid = client.put(
        "/api/intelligence/profile",
        json=_profile_payload(
            recommendation_thresholds={"medium": 80, "high": 60}
        ),
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=True),
    )
    assert invalid.status_code == 422
    assert "medium < high" in invalid.json()["detail"]


def test_eligibility_reports_match_mismatch_unknown_and_not_applicable(intelligence):
    session = intelligence["session"]
    opportunity = _opportunity(
        session,
        eligibility=(
            "Only registered NGOs in India with a minimum of 5 years of "
            "operation and ISO 9001 certification may apply."
        ),
    )
    profile = profile_dict(
        update_profile(session, _profile_payload(), "admin@catalysts.org")
    )

    matched = {row["criterion"]: row for row in eligibility_matches(opportunity, profile)}
    assert matched["Geography"]["status"] == "MATCH"
    assert matched["Organization type"]["status"] == "MATCH"
    assert matched["Years of operation"]["status"] == "MATCH"
    assert matched["Certification or registration"]["status"] == "MATCH"

    mismatch_profile = dict(profile)
    mismatch_profile.update({
        "countries_of_operation": ["Nepal"],
        "geographic_focus": [],
        "organization_types": ["For-profit"],
        "years_of_operation": 2,
        "certifications": [],
    })
    mismatched = {
        row["criterion"]: row
        for row in eligibility_matches(opportunity, mismatch_profile)
    }
    assert mismatched["Geography"]["status"] == "MISMATCH"
    assert mismatched["Organization type"]["status"] == "MISMATCH"
    assert mismatched["Years of operation"]["status"] == "MISMATCH"
    assert mismatched["Certification or registration"]["status"] == "UNKNOWN"

    no_requirements = _opportunity(
        session,
        suffix="no-requirements",
        country="",
        region="",
        location="",
        eligibility="Applications are open.",
    )
    statuses = {
        row["criterion"]: row["status"]
        for row in eligibility_matches(no_requirements, profile)
    }
    assert statuses == {
        "Geography": "UNKNOWN",
        "Organization type": "NOT_APPLICABLE",
        "Years of operation": "NOT_APPLICABLE",
        "Certification or registration": "NOT_APPLICABLE",
    }


def test_recommendation_is_weighted_explainable_and_audited_once(intelligence):
    session = intelligence["session"]
    update_profile(session, _profile_payload(), "admin@catalysts.org")
    opportunity = _opportunity(session)
    session.add(HistoricalLead(
        fingerprint="won-health-india",
        title="India maternal health research grant",
        opportunity_type="Grant",
        sector="Health",
        verticals="Health",
        brands="CMS",
        archetypes="Devsol",
        geography="India",
        focus_area="Maternal health research",
        status="Awarded / accepted",
        outcome="won",
        won_lost="won",
        reason="Strong strategic fit",
        event_date=date(2025, 1, 10),
    ))
    session.commit()

    result = analyze_opportunity(session, opportunity)
    weights = _profile_payload()["recommendation_weights"]
    expected = round(
        sum(result["components"][key] * weights[key] for key in weights)
        / sum(weights.values()),
        1,
    )
    assert result["recommendation_score"] == expected
    assert result["priority"] == "HIGH"
    assert result["eligibility_level"] == "HIGH"
    assert result["similar_leads"][0]["won_lost"] == "won"
    assert any("closest decided historical leads were won" in reason
               for reason in result["reasons"])
    assert session.scalar(
        select(func.count()).select_from(OpportunityIntelligence)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ExperienceEvent).where(
            ExperienceEvent.event_type == "prediction"
        )
    ) == 1

    # Re-reading an unchanged score refreshes the snapshot but must not flood
    # the immutable experience log with duplicate prediction events.
    analyze_opportunity(session, opportunity)
    assert session.scalar(
        select(func.count()).select_from(ExperienceEvent).where(
            ExperienceEvent.event_type == "prediction"
        )
    ) == 1


def test_human_correction_is_stored_and_protects_hierarchy_labels(intelligence):
    client = intelligence["client"]
    session = intelligence["session"]
    opportunity = _opportunity(
        session,
        verticals="Climate/Sustainability(ESG)",
        brands="Setu",
        archetypes="",
    )
    payload = {
        "decision": "correct",
        "corrected_verticals": ["Health"],
        "corrected_brands": [],
        "corrected_archetypes": [],
        "eligibility_override": "MEDIUM",
        "reason": "Reviewer checked the full call",
        "comment": "Health is the primary subject.",
    }
    response = client.post(
        f"/api/intelligence/opportunities/{opportunity.id}/feedback",
        json=payload,
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    )
    assert response.status_code == 200, response.text
    assert response.json()["human_labels_protected"] is True

    session.refresh(opportunity)
    assert opportunity.verticals == "Health"
    assert opportunity.brands == "CMS"
    assert opportunity.archetypes == "Devsol"
    assert opportunity.verticals_source == "human"
    assert opportunity.classification_source == "human"
    assert opportunity.verticals_labeled_by == "person@catalysts.org"
    stored = session.scalar(select(HumanFeedback))
    assert stored is not None
    assert json.loads(stored.corrected_verticals) == ["Health"]
    assert session.scalar(
        select(func.count()).select_from(ExperienceEvent).where(
            ExperienceEvent.event_type == "human_feedback"
        )
    ) == 1

    current = client.get(
        f"/api/intelligence/opportunities/{opportunity.id}",
        cookies=_cookie(admin=False),
    )
    assert current.status_code == 200
    assert current.json()["human_decision"] == "correct"
    assert current.json()["human_reason"] == "Reviewer checked the full call"

    invalid = client.post(
        f"/api/intelligence/opportunities/{opportunity.id}/feedback",
        json={**payload, "corrected_verticals": ["Not a real vertical"]},
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    )
    assert invalid.status_code == 422


def test_eligibility_only_correction_does_not_erase_existing_labels(intelligence):
    client = intelligence["client"]
    session = intelligence["session"]
    opportunity = _opportunity(
        session,
        verticals="Health",
        brands="CMS",
        archetypes="Devsol",
    )

    response = client.post(
        f"/api/intelligence/opportunities/{opportunity.id}/feedback",
        json={
            "decision": "correct",
            "eligibility_override": "LOW",
            "reason": "A mandatory registration is missing",
        },
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    )
    assert response.status_code == 200, response.text
    session.refresh(opportunity)
    assert opportunity.verticals == "Health"
    assert opportunity.brands == "CMS"
    assert opportunity.archetypes == "Devsol"
    effective = client.get(
        f"/api/intelligence/opportunities/{opportunity.id}",
        cookies=_cookie(admin=False),
    ).json()
    assert effective["eligibility_override"] == "LOW"
    assert effective["eligibility_level"] == "LOW"
    assert effective["components"]["eligibility_fit"] == 0.0


def test_explicit_empty_classification_lists_are_a_deliberate_clear(intelligence):
    client = intelligence["client"]
    session = intelligence["session"]
    opportunity = _opportunity(
        session,
        verticals="Health",
        brands="CMS",
        archetypes="Devsol",
    )

    response = client.post(
        f"/api/intelligence/opportunities/{opportunity.id}/feedback",
        json={
            "decision": "correct",
            "corrected_verticals": [],
            "corrected_brands": [],
            "corrected_archetypes": [],
            "reason": "This call belongs to none of the configured hierarchy",
        },
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    )
    assert response.status_code == 200, response.text
    session.refresh(opportunity)
    assert opportunity.verticals == ""
    assert opportunity.brands == ""
    assert opportunity.archetypes == ""
    assert opportunity.classification_status == "unclassified"
    assert opportunity.verticals_source == "human"


def test_feedback_permissions_read_only_and_learning_summary(intelligence, monkeypatch):
    client = intelligence["client"]
    session = intelligence["session"]
    opportunity = _opportunity(session)
    session.add_all([
        HistoricalLead(
            fingerprint="won-one", title="Won health call", status="Awarded",
            opportunity_type="Grant", verticals="Health", geography="India",
            won_lost="won", outcome="won", reason="Relevant",
        ),
        HistoricalLead(
            fingerprint="lost-one", title="Lost climate call", status="Rejected",
            opportunity_type="RFP", verticals="Climate/Sustainability(ESG)",
            geography="Nepal", won_lost="lost", outcome="lost",
            reason="Low feasibility",
        ),
    ])
    session.add(HumanFeedback(
        opportunity_id=opportunity.id,
        owner="person@catalysts.org",
        decision="accept",
    ))
    session.commit()

    assert client.get(
        "/api/intelligence/learning", cookies=_cookie(admin=False)
    ).status_code == 403
    learning = client.get(
        "/api/intelligence/learning", cookies=_cookie(admin=True)
    )
    assert learning.status_code == 200
    assert learning.json()["historical"] == {
        "total": 2,
        "decided": 2,
        "won": 1,
        "lost": 1,
        "positive_unverified": 0,
        "applied": 0,
        "shortlisted": 0,
        "rejected": 0,
        "win_rate": 50.0,
    }
    assert learning.json()["feedback"]["accepted"] == 1
    assert ["Health", 1] in learning.json()["successful_verticals"]

    assert client.post(
        "/api/intelligence/recalculate",
        json={"limit": 10},
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    ).status_code == 403

    monkeypatch.setattr(settings, "read_only", True)
    blocked = client.post(
        f"/api/intelligence/opportunities/{opportunity.id}/feedback",
        json={"decision": "accept"},
        headers={"Origin": "http://testserver"},
        cookies=_cookie(admin=False),
    )
    assert blocked.status_code == 403
    assert session.scalar(
        select(func.count()).select_from(HumanFeedback)
    ) == 1


def test_learning_summary_handles_an_empty_database(intelligence):
    assert learning_summary(intelligence["session"])["historical"] == {
        "total": 0,
        "decided": 0,
        "won": 0,
        "lost": 0,
        "positive_unverified": 0,
        "applied": 0,
        "shortlisted": 0,
        "rejected": 0,
        "win_rate": 0,
    }
