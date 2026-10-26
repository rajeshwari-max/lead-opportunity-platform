"""Socket-free checks for the operational workflows and policy guards."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from app.api import directories as api, leads, workspace, wrike
from app.database.models import (Base, DirectoryEntry, DirectoryEvent, LeadMetadata,
    Opportunity, WrikeTaskLink, WrikeWorkflowSnapshot)
from app.services.actionable import application_today
from app.services import verticals, wrike_tasks
from app.services.directories import blocked_sources, seed_directories


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(leads.storage.settings, "ml_classifier_enabled", False)
    monkeypatch.setattr(leads.storage.settings, "read_only", False)
    original = verticals._COMPILED
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    verticals._COMPILED = original
    engine.dispose()


ACTOR = {"email": "admin@example.org"}


def test_review_history_and_drop_reason(db):
    result = api.create("website", api.EntryInput(name="Test website", review_date=application_today()), ACTOR, db)
    assert api.listing("website", db)[0]["review_due"]
    for action in ("pinged", "no_response", "reviewed"):
        api.record_review("website", result["id"], api.ReviewInput(action=action, note="Recorded contact outcome"), ACTOR, db)
    with pytest.raises(HTTPException) as exc:
        api.update("website", result["id"], api.EntryInput(name="Test website", status="dropped"), ACTOR, db)
    assert exc.value.status_code == 422
    api.update("website", result["id"], api.EntryInput(name="Test website", status="dropped", notes="Review complete; no response"), ACTOR, db)
    assert [r["action"] for r in api.history("website", result["id"], db)] == ["updated", "reviewed", "no_response", "pinged", "created"]
    assert db.get(DirectoryEntry, result["id"]).reviewed_at is not None


def test_keyword_mutation_changes_rules_without_restart(db):
    assert "Health" not in verticals.classify_verticals("healingcommunity grant")
    row = api.create("keyword", api.EntryInput(name="healingcommunity", scope="Health"), ACTOR, db)
    assert "Health" in verticals.classify_verticals("healingcommunity grant")
    api.update("keyword", row["id"], api.EntryInput(name="healingcommunity", scope="Health", status="paused"), ACTOR, db)
    assert "Health" not in verticals.classify_verticals("healingcommunity grant")


def test_duplicate_directory_and_invalid_scope_are_rejected(db):
    api.create("keyword", api.EntryInput(name="testterm", scope="Health"), ACTOR, db)
    for value, code in ((api.EntryInput(name="TESTTERM", scope="Health"), 409), (api.EntryInput(name="testterm", scope="invalid"), 422)):
        with pytest.raises(HTTPException) as exc:
            api.create("keyword", value, ACTOR, db)
        assert exc.value.status_code == code


def test_manual_leads_due_ownership_and_deduplication(db):
    data = dict(title="Community Health Award", category="Award", opportunity_url="https://example.org/calls/12",
                award_type="both", award_evidence="Cash and trophy", next_action="Review again", next_action_date=application_today())
    result = leads.manual_lead(leads.ManualLeadInput(**data), "alice", db)
    assert not result["reused_existing"]
    assert len(leads.due_leads("alice", db)) == 1
    assert not leads.due_leads("bob", db)
    assert not leads.saved("bob", db)
    assert leads.manual_lead(leads.ManualLeadInput(**{**data, "title": "Updated title"}), "alice", db)["reused_existing"]
    assert db.scalar(select(func.count()).select_from(Opportunity)) == 1
    assert db.get(LeadMetadata, result["opportunity"]["id"]).award_type == "both"
    assert db.get(Opportunity, result["opportunity"]["id"]).deadline_state == "unknown"


def test_manual_input_requires_source_evidence_and_valid_resource(db):
    for extra in ({"opportunity_url": "javascript:alert(1)"}, {"rolling": True, "deadline": application_today()},
                  {"award_type": "monetary"}, {"category": "Award", "award_type": "monetary"}, {"resource_organisation_id": 1234}):
        with pytest.raises(HTTPException) as exc:
            leads.manual_lead(leads.ManualLeadInput(title="Test new opportunity", **extra), "alice", db)
        assert exc.value.status_code == 422
    resource = api.create("organisation", api.EntryInput(name="Resource partner", role="resource", institution="University", location="India"), ACTOR, db)
    result = leads.manual_lead(leads.ManualLeadInput(title="Resource opportunity", resource_organisation_id=resource["id"], next_action_date=application_today() + timedelta(days=1)), "alice", db)
    assert result["resource_organisation"]["institution"] == "University"
    assert not leads.due_leads("alice", db)


def test_award_review_does_not_infer_money_from_title(db):
    result = leads.manual_lead(leads.ManualLeadInput(title="Million dollar award", category="Award"), "alice", db)
    oid = result["opportunity"]["id"]
    assert leads.award_details(oid, "alice", db)["award_type"] == "unknown"
    leads.update_award(oid, leads.AwardInput(award_type="recognition", award_evidence="The source promises a trophy"), ACTOR, db)
    assert leads.award_details(oid, "alice", db)["award_type"] == "recognition"


def test_source_retirement_is_reversible_and_does_not_delete_history(db, monkeypatch):
    from app.scrapers.registry import SCRAPER_REGISTRY
    monkeypatch.setitem(SCRAPER_REGISTRY, "testsource", SimpleNamespace(display_name="Test source", website="https://example.org"))
    row = api.create("website", api.EntryInput(name="Test source", scope="testsource"), ACTOR, db)
    assert not blocked_sources(db)
    for status in ("paused", "dropped", "active"):
        api.update("website", row["id"], api.EntryInput(name="Test source", scope="testsource", status=status, notes="Reviewed source"), ACTOR, db)
        assert ("testsource" in blocked_sources(db)) == (status != "active")
    assert len(api.history("website", row["id"], db)) == 4


def test_wrike_mirrors_exact_labels_and_rejects_local_stage_change(db, monkeypatch):
    lead = leads.manual_lead(leads.ManualLeadInput(title="Community research grant"), "alice", db)
    oid = lead["opportunity"]["id"]
    db.add(WrikeTaskLink(opportunity_id=oid, task_id="TASK123", status="created", folder_id="FOLDER", created_by="alice"))
    db.commit()
    def remote(_db, path, params=None):
        if path.startswith("/tasks/"):
            return {"data": [{"id": "TASK123", "status": "Active", "customStatusId": "STATUS1"}]}
        return {"data": [{"name": "Catalyst pipeline", "customStatuses": [{"id": "STATUS1", "name": "Founder review", "group": "Active"}]}]}
    monkeypatch.setattr(wrike_tasks, "_wrike_get", remote)
    result = wrike.current_workflow(oid, db, "alice")
    assert result["status_name"] == "Founder review"
    assert db.get(WrikeWorkflowSnapshot, oid).status_name == "Founder review"
    assert leads.saved("alice", db)[0]["workflow_stage"] == "Founder review"
    with pytest.raises(HTTPException) as exc:
        workspace.update_journey(lead["id"], workspace.JourneyInput(stage="Applied"), "alice", db)
    assert exc.value.status_code == 409
    assert workspace.update_journey(lead["id"], workspace.JourneyInput(stage="Saved", notes="Keep notes"), "alice", db)["notes"] == "Keep notes"
    monkeypatch.setattr(wrike_tasks, "_wrike_get", lambda _db, path, params=None: {"data": [{"id": "TASK123", "status": "Active", "customStatusId": "SPACE_ONLY"}]} if path.startswith("/tasks/") else {"data": []})
    result = wrike.current_workflow(oid, db, "alice")
    assert not result["resolved"] and not result["status_name"]


def test_seed_is_idempotent_and_preserves_decisions(db, monkeypatch):
    from app.scrapers.registry import SCRAPER_REGISTRY
    monkeypatch.setattr("app.services.keyword_inventory.VERTICAL_KEYWORDS", {"Health": ["testterm"]})
    monkeypatch.setitem(SCRAPER_REGISTRY, "testsource", SimpleNamespace(display_name="Test source", website="https://example.org"))
    seed_directories(db)
    row = db.scalar(select(DirectoryEntry).where(DirectoryEntry.kind == "keyword"))
    row.status = "paused"
    row.name = "renamedterm"
    from app.services.directories import entry_key
    row.key = entry_key("keyword", row.name, row.scope)
    db.commit()
    count = db.scalar(select(func.count()).select_from(DirectoryEntry))
    seed_directories(db)
    assert db.scalar(select(func.count()).select_from(DirectoryEntry)) == count
    assert row.status == "paused"
    assert not db.scalar(select(DirectoryEntry).where(DirectoryEntry.name == "testterm"))
