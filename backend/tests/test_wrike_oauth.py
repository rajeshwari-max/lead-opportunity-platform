import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import wrike
from app.services import wrike_service
from app.core.config import settings
from app.database.db import get_db
from app.database.models import Base, WrikeConnection
from app.services.wrike_service import decrypt_token, encrypt_token


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(settings, "wrike_client_id", "test-client")
    monkeypatch.setattr(settings, "wrike_client_secret", "test-secret")
    monkeypatch.setattr(
        settings, "wrike_redirect_uri", "http://localhost:8000/api/wrike/oauth/callback"
    )
    monkeypatch.setattr(settings, "wrike_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "dashboard_url", "http://localhost:5173/")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    app = FastAPI()
    app.include_router(wrike.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[wrike.require_wrike_admin] = lambda: {
        "email": "admin@example.org", "is_admin": True
    }
    with TestClient(app) as client:
        yield client, session, app
    session.close()
    engine.dispose()


def _start(client):
    response = client.get("/api/wrike/connect", follow_redirects=False)
    assert response.status_code == 302
    assert "wrike_oauth_state" in response.headers["set-cookie"]
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def test_callback_saves_only_encrypted_tokens(setup, monkeypatch):
    client, session, _ = setup
    state = _start(client)
    monkeypatch.setattr(wrike, "exchange_authorization_code", lambda code: {
        "access_token": "access-test-only",
        "refresh_token": "refresh-test-only",
        "token_type": "bearer",
        "host": "www.wrike.com",
        "expires_in": "3600",
    })

    response = client.get(
        "/api/wrike/oauth/callback",
        params={"state": state, "code": "test-code"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:5173/"
    assert "wrike_oauth_state" in response.headers["set-cookie"]
    assert "access-test-only" not in str(response.headers)
    assert "refresh-test-only" not in str(response.headers)

    saved = session.get(WrikeConnection, 1)
    assert saved is not None
    assert saved.access_token_encrypted != "access-test-only"
    assert saved.refresh_token_encrypted != "refresh-test-only"
    assert decrypt_token(saved.access_token_encrypted) == "access-test-only"
    assert decrypt_token(saved.refresh_token_encrypted) == "refresh-test-only"
    assert saved.host == "www.wrike.com"
    assert saved.connected_by == "admin@example.org"
    assert client.get("/api/wrike/status").json()["connected"] is True


def test_callback_rejects_wrong_state_before_exchange(setup, monkeypatch):
    client, session, _ = setup
    _start(client)

    def unexpected_exchange(_code):
        raise AssertionError("The token endpoint must not be called")

    monkeypatch.setattr(wrike, "exchange_authorization_code", unexpected_exchange)
    response = client.get(
        "/api/wrike/oauth/callback",
        params={"state": "wrong", "code": "test-code"},
    )
    assert response.status_code == 400
    assert session.get(WrikeConnection, 1) is None


def test_callback_rejects_untrusted_token_host(setup, monkeypatch):
    client, session, _ = setup
    state = _start(client)
    monkeypatch.setattr(wrike, "exchange_authorization_code", lambda code: {
        "access_token": "access-test-only",
        "refresh_token": "refresh-test-only",
        "token_type": "bearer",
        "host": "www.wrike.com.evil.example",
        "expires_in": 3600,
    })
    response = client.get(
        "/api/wrike/oauth/callback",
        params={"state": state, "code": "test-code"},
    )
    assert response.status_code == 502
    assert session.get(WrikeConnection, 1) is None


def test_connect_rejects_auth_disabled(setup, monkeypatch):
    client, _, app = setup
    del app.dependency_overrides[wrike.require_wrike_admin]
    monkeypatch.setattr(settings, "personal_login", False)
    response = client.get("/api/wrike/connect", follow_redirects=False)
    assert response.status_code == 403


def _save_connection(session, *, expires_at):
    session.add(WrikeConnection(
        id=1,
        access_token_encrypted=encrypt_token("old-access"),
        refresh_token_encrypted=encrypt_token("old-refresh"),
        host="www.wrike.com",
        expires_at=expires_at,
        connected_by="admin@example.org",
    ))
    session.commit()


def _http_response(status_code, payload, url):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", url),
    )


def test_verify_uses_saved_token_without_refresh(setup, monkeypatch):
    client, session, _ = setup
    _save_connection(session, expires_at=int(time.time()) + 3600)

    def unexpected_refresh(*args, **kwargs):
        raise AssertionError("A valid access token must not be refreshed")

    def fake_get(url, *, params, headers, timeout):
        assert url == "https://www.wrike.com/api/v4/contacts"
        assert params == {"me": "true"}
        assert headers == {"Authorization": "Bearer old-access"}
        return _http_response(200, {"kind": "contacts", "data": [{"id": "TEST1234"}]}, url)

    monkeypatch.setattr(wrike_service.httpx, "post", unexpected_refresh)
    monkeypatch.setattr(wrike_service.httpx, "get", fake_get)
    assert client.post("/api/wrike/verify").json() == {"reachable": True}


def test_verify_rotates_both_tokens_before_read(setup, monkeypatch):
    client, session, _ = setup
    _save_connection(session, expires_at=0)

    def fake_post(url, *, data, timeout):
        assert url == "https://www.wrike.com/oauth2/token"
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "old-refresh"
        return _http_response(200, {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
            "expires_in": "3600",
            "host": "www.wrike.com",
        }, url)

    def fake_get(url, *, params, headers, timeout):
        assert headers["Authorization"] == "Bearer new-access"
        return _http_response(200, {"kind": "contacts", "data": [{"id": "TEST1234"}]}, url)

    monkeypatch.setattr(wrike_service.httpx, "post", fake_post)
    monkeypatch.setattr(wrike_service.httpx, "get", fake_get)
    assert client.post("/api/wrike/verify").json() == {"reachable": True}
    session.expire_all()
    saved = session.get(WrikeConnection, 1)
    assert decrypt_token(saved.access_token_encrypted) == "new-access"
    assert decrypt_token(saved.refresh_token_encrypted) == "new-refresh"
    assert saved.expires_at > int(time.time())


def test_verify_retries_once_after_unauthorized(setup, monkeypatch):
    client, session, _ = setup
    _save_connection(session, expires_at=int(time.time()) + 3600)
    seen = []

    def fake_post(url, *, data, timeout):
        return _http_response(200, {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "bearer",
            "expires_in": 3600,
        }, url)

    def fake_get(url, *, params, headers, timeout):
        seen.append(headers["Authorization"])
        if len(seen) == 1:
            return _http_response(401, {"error": "not_authorized"}, url)
        return _http_response(200, {"kind": "contacts", "data": [{"id": "TEST1234"}]}, url)

    monkeypatch.setattr(wrike_service.httpx, "post", fake_post)
    monkeypatch.setattr(wrike_service.httpx, "get", fake_get)
    assert client.post("/api/wrike/verify").json() == {"reachable": True}
    assert seen == ["Bearer old-access", "Bearer new-access"]


def test_verify_requires_real_sign_in(setup, monkeypatch):
    client, _, app = setup
    del app.dependency_overrides[wrike.require_wrike_admin]
    monkeypatch.setattr(settings, "personal_login", False)
    assert client.post("/api/wrike/verify").status_code == 403
