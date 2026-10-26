"""Founders workflow regression tests; isolated SQLite and mocked Wrike."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.api import directories, leads, workspace, wrike
from app.core.auth import COOKIE_NAME, hash_password, make_session_token, password_version
from app.core.config import settings
from app.database.db import get_db
from app.database.models import (Base, Category, DirectoryEntry, DirectoryEvent, LeadMetadata,
    Opportunity, TeamMember, WorkspaceCredential, WrikeTaskLink, WrikeWorkflowSnapshot)
from app.services.actionable import application_today
from app.services import verticals, wrike_tasks


@pytest.fixture
def context(monkeypatch):
    for key, value in {"personal_login": True, "read_only": False, "approval_secret": "founders-tests", "ml_classifier_enabled": False}.items():
        monkeypatch.setattr(settings, key, value)
    original = verticals._COMPILED
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    credential = WorkspaceCredential(owner="alice@example.org", password_hash=hash_password("test-password"), is_admin=True)
    bob = WorkspaceCredential(owner="bob@example.org", password_hash=hash_password("test-password"), is_admin=False)
    db.add_all([credential, bob, TeamMember(name="Alice", email=credential.owner), TeamMember(name="Bob", email=bob.owner)])
    db.commit()
    app = FastAPI()
    for router in (directories.router, leads.router, workspace.router, wrike.router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        def login(row=credential):
            client.cookies.clear()
            client.cookies.set(COOKIE_NAME, make_session_token(row.owner, row.owner, row.is_admin, password_version(row.password_hash)))
        login()
        yield client, db, login, bob
    verticals._COMPILED = original
    db.close()
    engine.dispose()


def test_directory_reviews_and_drop_are_audited(context):
    client, db, _, _ = context
    result = client.post("/api/directories/website", json={"name": "Candidate funder", "url": "https://example.org", "review_date": application_today().isoformat()})
    assert result.status_code == 201, result.text
    row = result.json()
    assert client.get("/api/directories/website").json()[0]["review_due"]
    path = f"/api/directories/website/{row['id']}"
    assert client.post(path + "/events", json={"action": "pinged", "note": "Asked about future calls"}).status_code == 200
    assert client.post(path + "/events", json={"action": "no_response", "note": "No reply after review"}).status_code == 200
    assert client.put(path, json={"name": row["name"], "status": "dropped"}).status_code == 422
    assert client.put(path, json={"name": row["name"], "status": "dropped", "notes": "Reviewed repeated non-response"}).status_code == 200
    assert [r["action"] for r in client.get(path + "/events").json()] == ["updated", "no_response", "pinged", "created"]
    assert db.scalar(select(func.count()).select_from(DirectoryEvent)) == 4


def test_keyword_edits_feed_rules_without_restarting(context):
    client, _, _, _ = context
    assert "Health" not in verticals.classify_verticals("healingcommunity grant")
    created = client.post("/api/directories/keyword", json={"name": "healingcommunity", "scope": "Health"})
    assert created.status_code == 201, created.text
    assert "Health" in verticals.classify_verticals("healingcommunity grant")
    assert client.put(f"/api/directories/keyword/{created.json()['id']}", json={"name": "healingcommunity", "scope": "Health", "status": "paused"}).status_code == 200
    assert "Health" not in verticals.classify_verticals("healingcommunity grant")
    assert client.post("/api/directories/keyword", json={"name": "testterm", "scope": "Wrong vertical"}).status_code == 422


def test_directory_authentication_and_read_only(context, monkeypatch):
    client, _, login, bob = context
    login(bob)
    assert client.get("/api/directories/organisation").status_code == 200
    assert client.post("/api/directories/organisation", json={"name": "Funder", "role": "whitelist"}).status_code == 403
    assert client.put("/api/my-leads/awards/1", json={"award_type": "monetary"}).status_code == 403
    login()
    monkeypatch.setattr(settings, "read_only", True)
    assert client.post("/api/directories/website", json={"name": "Funder"}).status_code == 403
    assert client.post("/api/my-leads/manual", json={"title": "A new lead"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/directories/website").status_code == 401


def test_manual_lead_reappears_and_preserves_duplicate_details(context):
    client, db, login, bob = context
    body = {"title": "Community Health Award", "category": "Award", "opportunity_url": "https://example.org/call/12",
            "award_type": "both", "award_evidence": "Cash prize and a trophy", "next_action": "Contact funder",
            "next_action_date": application_today().isoformat()}
    created = client.post("/api/my-leads/manual", json=body)
    assert created.status_code == 201, created.text
    assert not created.json()["reused_existing"]
    assert len(client.get("/api/my-leads/due").json()) == 1
    assert client.post("/api/my-leads/manual", json={**body, "title": "Changed title", "award_type": "recognition"}).json()["reused_existing"]
    assert db.scalar(select(func.count()).select_from(Opportunity)) == 1
    metadata = db.get(LeadMetadata, created.json()["opportunity"]["id"])
    assert metadata.award_type == "both"
    assert db.get(Opportunity, metadata.opportunity_id).deadline_state == "unknown"
    login(bob)
    assert client.get("/api/my-leads/due").json() == []
    assert client.get("/api/my-leads").json() == []


def test_manual_lead_validates_awards_deadlines_urls_and_resources(context):
    client, db, _, _ = context
    for extra in ({"opportunity_url": "javascript:alert(1)"}, {"rolling": True, "deadline": application_today().isoformat()},
                  {"award_type": "monetary"}, {"category": "Award", "award_type": "monetary"}, {"resource_organisation_id": 999}):
        assert client.post("/api/my-leads/manual", json={"title": "New opportunity", **extra}).status_code == 422
    resource = client.post("/api/directories/organisation", json={"name": "Partner", "role": "resource", "institution": "University", "location": "India"}).json()
    result = client.post("/api/my-leads/manual", json={"title": "Resource partner lead", "resource_organisation_id": resource["id"], "next_action_date": (application_today() + timedelta(days=2)).isoformat()})
    assert result.status_code == 201, result.text
    assert db.get(LeadMetadata, result.json()["opportunity"]["id"]).resource_organisation_id == resource["id"]
    assert client.get("/api/my-leads/due").json() == []


def test_paused_and_dropped_sources_never_start(context, monkeypatch):
    _, db, _, _ = context
    from app.services import scraper_manager as mod
    db.add(DirectoryEntry(kind="website", key="test", name="Test", scope="example", status="dropped"))
    db.commit()
    @contextmanager
    def sessions():
        yield db
    monkeypatch.setattr(mod, "session_scope", sessions)
    monkeypatch.setattr(mod, "get_scrapers", lambda *_: [SimpleNamespace(name="example")])
    with pytest.raises(RuntimeError):
        asyncio.run(mod.ScraperManager()._start(["example"]))


def test_wrike_labels_mirror_remote_status_and_block_local_edits(context, monkeypatch):
    client, db, _, _ = context
    lead = client.post("/api/my-leads/manual", json={"title": "New community grant"}).json()
    oid = lead["opportunity"]["id"]
    db.add(WrikeTaskLink(opportunity_id=oid, task_id="TASK123", status="created", folder_id="FOLDER", created_by="alice@example.org"))
    db.commit()
    def remote(_db, path, params=None):
        if path.startswith("/tasks/"):
            return {"data": [{"id": "TASK123", "status": "Active", "customStatusId": "STATUS1"}]}
        return {"data": [{"name": "Catalyst pipeline", "customStatuses": [{"id": "STATUS1", "name": "Founder review", "group": "Active"}]}]}
    monkeypatch.setattr(wrike_tasks, "_wrike_get", remote)
    result = client.get(f"/api/wrike/opportunities/{oid}/workflow")
    assert result.status_code == 200, result.text
    assert result.json()["status_name"] == "Founder review"
    assert db.get(WrikeWorkflowSnapshot, oid).workflow_name == "Catalyst pipeline"
    assert client.get("/api/my-leads").json()[0]["workflow_stage"] == "Founder review"
    assert client.put(f"/api/my-leads/{lead['id']}", json={"stage": "Applied"}).status_code == 409
    assert client.put(f"/api/my-leads/{lead['id']}", json={"stage": "Saved", "notes": "Keep private notes"}).status_code == 200
    monkeypatch.setattr(wrike_tasks, "_wrike_get", lambda _db, path, params=None: {"data": [{"id": "TASK123", "customStatusId": "SPACE_ONLY", "status": "Active"}]} if path.startswith("/tasks/") else {"data": []})
    unresolved = client.get(f"/api/wrike/opportunities/{oid}/workflow").json()
    assert not unresolved["resolved"]
    assert unresolved["status_name"] == ""


def test_seed_preserves_edits(context, monkeypatch):
    _, db, _, _ = context
    from app.services.directories import seed_directories
    from app.scrapers.registry import SCRAPER_REGISTRY
    monkeypatch.setattr("app.services.keyword_inventory.VERTICAL_KEYWORDS", {"Health": ["testterm"]})
    monkeypatch.setitem(SCRAPER_REGISTRY, "testsource", SimpleNamespace(display_name="Test source", website="https://example.org"))
    seed_directories(db)
    row = db.scalar(select(DirectoryEntry).where(DirectoryEntry.kind == "keyword"))
    row.status = "paused"
    db.commit()
    count = db.scalar(select(func.count()).select_from(DirectoryEntry))
    seed_directories(db)
    assert db.scalar(select(func.count()).select_from(DirectoryEntry)) == count
    assert row.status == "paused"
