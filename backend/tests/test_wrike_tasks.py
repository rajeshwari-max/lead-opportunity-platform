"""Wrike task creation tests: no live Wrike writes."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import wrike
from app.core.auth import COOKIE_NAME, hash_password, make_session_token, password_version
from app.core.config import settings
from app.database.db import get_db
from app.database.models import (
    Base, Opportunity, TeamMember, WorkspaceCredential, WrikeTaskLink,
)
from app.services import wrike_tasks


FOLDER_ID = "IEAAAKNQI4ATBNDO"
TASK_URL = "https://www.wrike.com/open.htm?id=123456"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "personal_login", True)
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "wrike_enabled", True)
    monkeypatch.setattr(settings, "wrike_folder_id", FOLDER_ID)
    monkeypatch.setattr(settings, "approval_secret", "wrike-test-session-secret")
    monkeypatch.setattr(settings, "wrike_token_encryption_key", Fernet.generate_key().decode())
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    alice = TeamMember(name="Alice", email="alice@example.org", active=True)
    bob = TeamMember(name="Bob", email="bob@example.org", active=True)
    opportunity = Opportunity(
        unique_id="wrike-example", title="Health opportunity", organization="Funder",
        source_website="Example", summary="A useful lead",
    )
    credential = WorkspaceCredential(
        owner="alice@example.org", password_hash=hash_password("test-password"), is_admin=True
    )
    db.add_all([alice, bob, opportunity, credential])
    db.commit()
    app = FastAPI()
    app.include_router(wrike.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(wrike, "get_folder", lambda _db, folder_id: {
        "id": folder_id, "title": "Leads for discussion in next OST"
    })
    monkeypatch.setattr(wrike, "contact_ids_by_email", lambda _db, emails: {
        "alice@example.org": {"KUAYH4OI"},
        "bob@example.org": {"KUAYH4OJ"},
    })
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, make_session_token(
            "alice@example.org", "Alice", True,
            password_version(credential.password_hash),
        ))
        yield client, db
    db.close()
    engine.dispose()


def test_folder_and_assignees_are_available(setup):
    client, _ = setup
    folder = client.get("/api/wrike/folder")
    assert folder.status_code == 200
    assert folder.json() == {
        "id": FOLDER_ID, "title": "Leads for discussion in next OST"
    }
    assignees = client.get("/api/wrike/assignees")
    assert assignees.status_code == 200
    assert len(assignees.json()) == 2
    assert all(member["available"] for member in assignees.json())
    assert "KUAYH4OI" not in assignees.text  # Contact IDs stay on the backend.


def test_one_task_per_opportunity(setup, monkeypatch):
    client, db = setup
    calls = []

    def fake_create(_db, **kwargs):
        calls.append(kwargs)
        assert db.scalar(select(WrikeTaskLink).where(WrikeTaskLink.opportunity_id == 1)).status == "creating"
        return {"task_id": "MAAAAAEQALwd", "permalink": TASK_URL, "assignment_warning": False}

    monkeypatch.setattr(wrike, "create_task", fake_create)
    first = client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1, 2]})
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "created"
    assert first.json()["permalink"] == TASK_URL
    assert calls[0]["folder_id"] == FOLDER_ID
    assert calls[0]["responsible_ids"] == ["KUAYH4OI", "KUAYH4OJ"]
    assert calls[0]["title"] == "Health opportunity"
    assert "Platform opportunity ID: 1" in calls[0]["description"]
    assert client.get("/api/wrike/opportunities/1").json()["permalink"] == TASK_URL
    second = client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]})
    assert second.status_code == 409
    assert len(calls) == 1
    assert json.loads(db.scalar(select(WrikeTaskLink)).responsible_ids) == ["KUAYH4OI", "KUAYH4OJ"]


@pytest.mark.parametrize("payload", [{}, {"member_ids": []}])
def test_task_can_be_created_without_assignees(setup, monkeypatch, payload):
    client, db = setup
    calls = []

    def fake_create(_db, **kwargs):
        calls.append(kwargs)
        return {"task_id": "MAAAAAEQALwd", "permalink": TASK_URL, "assignment_warning": False}

    monkeypatch.setattr(wrike, "create_task", fake_create)
    monkeypatch.setattr(wrike, "contact_ids_by_email", lambda *_: pytest.fail("Contacts should not be requested"))
    response = client.post("/api/wrike/opportunities/1/tasks", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["assignment_warning"] is False
    assert calls[0]["responsible_ids"] == []
    assert json.loads(db.scalar(select(WrikeTaskLink)).responsible_ids) == []
    assert client.post("/api/wrike/opportunities/1/tasks", json={}).status_code == 409


def test_definite_rejection_can_be_retried(setup, monkeypatch):
    client, db = setup

    def rejected(_db, **_kwargs):
        raise wrike_tasks.WrikeTaskRejected("Wrike rejected task creation (HTTP 403)")

    monkeypatch.setattr(wrike, "create_task", rejected)
    response = client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]})
    assert response.status_code == 502
    assert db.scalar(select(WrikeTaskLink)) is None


def test_uncertain_result_blocks_repeat_click(setup, monkeypatch):
    client, db = setup
    calls = []

    def uncertain(_db, **_kwargs):
        calls.append(1)
        raise wrike_tasks.WrikeTaskUncertain("timeout")

    monkeypatch.setattr(wrike, "create_task", uncertain)
    response = client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]})
    assert response.status_code == 502
    assert "Do not retry" in response.json()["detail"]
    assert client.get("/api/wrike/opportunities/1").json()["status"] == "uncertain"
    assert client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]}).status_code == 409
    assert len(calls) == 1
    assert db.scalar(select(WrikeTaskLink)).status == "uncertain"


def test_rejects_unmatched_or_inactive_assignee(setup, monkeypatch):
    client, db = setup
    monkeypatch.setattr(wrike, "contact_ids_by_email", lambda _db, _emails: {})
    assert client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]}).status_code == 422
    assert db.scalar(select(WrikeTaskLink)) is None
    db.get(TeamMember, 2).active = False
    db.commit()
    assert client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [2]}).status_code == 422


def test_read_only_and_auth_disabled_cannot_create(setup, monkeypatch):
    client, db = setup
    with monkeypatch.context() as patch:
        patch.setattr(settings, "read_only", True)
        assert client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]}).status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr(settings, "personal_login", False)
        assert client.post("/api/wrike/opportunities/1/tasks", json={"member_ids": [1]}).status_code == 403
    assert client.post(
        "/api/wrike/opportunities/1/tasks",
        json={"member_ids": [1]},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403
    assert db.scalar(select(WrikeTaskLink)) is None


def test_contact_mapping_requires_exact_active_person(setup, monkeypatch):
    _client, db = setup
    monkeypatch.setattr(wrike_tasks, "_wrike_get", lambda _db, _path, _params: {
        "kind": "contacts",
        "data": [
            {"id": "KUAYH4OI", "type": "Person", "deleted": False,
             "profiles": [{"email": "alice@example.org", "active": True}]},
            {"id": "KUAYH4OJ", "type": "Person", "deleted": False,
             "profiles": [{"email": "bob@example.org", "active": False}]},
            {"id": "KUAYH4OK", "type": "Group", "deleted": False,
             "profiles": [{"email": "bob@example.org", "active": True}]},
        ],
    })
    matches = wrike_tasks.contact_ids_by_email(db, ["alice@example.org", "bob@example.org"])
    assert matches == {"alice@example.org": {"KUAYH4OI"}, "bob@example.org": set()}


def test_wrike_post_uses_form_body_not_url_parameters(setup, monkeypatch):
    _client, db = setup
    monkeypatch.setattr(wrike_tasks, "_access_token", lambda _db: ("test-token", "www.wrike.com"))

    def fake_post(url, *, headers, data, timeout):
        assert url == f"https://www.wrike.com/api/v4/folders/{FOLDER_ID}/tasks"
        assert headers["Authorization"] == "Bearer test-token"
        assert json.loads(data["responsibles"]) == ["KUAYH4OI"]
        return httpx.Response(200, json={
            "kind": "tasks", "data": [{"id": "MAAAAAEQALwd", "permalink": TASK_URL,
                                      "parentIds": [FOLDER_ID], "responsibleIds": ["KUAYH4OI"]}],
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(wrike_tasks.httpx, "post", fake_post)
    result = wrike_tasks.create_task(
        db, folder_id=FOLDER_ID, title="Example", description="Description",
        responsible_ids=["KUAYH4OI"],
    )
    assert result == {"task_id": "MAAAAAEQALwd", "permalink": TASK_URL,
                      "assignment_warning": False}


def test_wrike_post_omits_responsibles_when_unassigned(setup, monkeypatch):
    _client, db = setup
    monkeypatch.setattr(wrike_tasks, "_access_token", lambda _db: ("test-token", "www.wrike.com"))

    def fake_post(url, *, headers, data, timeout):
        assert "responsibles" not in data
        return httpx.Response(200, json={
            "kind": "tasks", "data": [{"id": "MAAAAAEQALwd", "permalink": TASK_URL,
                                      "parentIds": [FOLDER_ID], "responsibleIds": []}],
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(wrike_tasks.httpx, "post", fake_post)
    result = wrike_tasks.create_task(
        db, folder_id=FOLDER_ID, title="Example", description="Description",
        responsible_ids=[],
    )
    assert result["assignment_warning"] is False
