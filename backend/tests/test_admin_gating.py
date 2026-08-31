from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import router
from app.core.auth import COOKIE_NAME, make_session_token
from app.core.config import settings
from app.database.db import get_db
from app.database.models import Base


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", "user-password")
    monkeypatch.setattr(settings, "admin_password", "admin-password")
    monkeypatch.setattr(settings, "approval_secret", "test-secret")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    session.close()


def cookie(is_admin: bool) -> dict[str, str]:
    return {COOKIE_NAME: make_session_token(
        "person@catalysts.org", "Person", is_admin=is_admin)}


@pytest.mark.parametrize("path", [
    "/api/review-queue",
    "/api/opportunities/unclassified",
    "/api/opportunities/unclassified/ids",
])
def test_review_gets_are_forbidden_to_ordinary_users(client, path):
    assert client.get(path, cookies=cookie(False)).status_code == 403


@pytest.mark.parametrize("path", [
    "/api/review-queue",
    "/api/opportunities/unclassified",
    "/api/opportunities/unclassified/ids",
])
def test_review_gets_are_available_to_admins(client, path):
    assert client.get(path, cookies=cookie(True)).status_code == 200


def test_review_write_is_forbidden_to_ordinary_user(client):
    response = client.post(
        "/api/review-queue/1", json={"decision": "closed"},
        cookies=cookie(False))
    assert response.status_code == 403


def test_vertical_write_is_forbidden_to_ordinary_user(client):
    response = client.post(
        "/api/opportunities/verticals/bulk",
        json={"opportunity_ids": [], "verticals": []},
        cookies=cookie(False))
    assert response.status_code == 403


def test_include_undated_is_an_admin_only_query_flag(client):
    assert client.get(
        "/api/opportunities?include_undated=true",
        cookies=cookie(False)).status_code == 403


def test_frontend_cards_are_rendered_only_inside_admin_gate():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.tsx").read_text(
        encoding="utf-8")
    assert "{isAdmin && <ReviewQueueCard" in source
    assert "{isAdmin && <UnclassifiedCard" in source
