"""Browser login boundaries: ownership, websocket auth, profile safety and cleanup."""
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import remote_login as api
from app.core.config import settings
from app.scrapers import devaid_auth
from app.services import remote_login as remote


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "personal_login", True)
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(api, "current_user", lambda _token: {
        "authenticated": True, "is_admin": True, "email": "admin@example.org"})
    monkeypatch.setattr(remote, "desktop", remote.Desktop(owner="admin@example.org", state="ready"))
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_admin_can_open_only_their_viewer_and_finish(client):
    s = remote.desktop
    page = client.get(f"/api/remote-login/{s.id}/viewer")
    assert page.status_code == 200
    assert "no-store" in page.headers["cache-control"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/api/remote-login/not-the-session/viewer").status_code == 404
    assert client.post(f"/api/remote-login/{s.id}/finish").status_code == 200
    assert s.finish.is_set()


def test_other_admin_cannot_read_control_or_view_session(client, monkeypatch):
    s = remote.desktop
    monkeypatch.setattr(api, "current_user", lambda _token: {
        "authenticated": True, "is_admin": True, "email": "other@example.org"})
    for suffix in ("status", "viewer", "assets/core/rfb.js"):
        assert client.get(f"/api/remote-login/{s.id}/{suffix}").status_code == 404
    for action in ("finish", "cancel"):
        assert client.post(f"/api/remote-login/{s.id}/{action}").status_code == 404


@pytest.mark.parametrize("authenticated,is_admin", [(False, False), (True, False)])
def test_viewer_denies_anonymous_and_nonadmin(client, monkeypatch, authenticated, is_admin):
    monkeypatch.setattr(api, "current_user", lambda _token: {
        "authenticated": authenticated, "is_admin": is_admin, "email": "admin@example.org"})
    assert client.get(f"/api/remote-login/{remote.desktop.id}/viewer").status_code == 403


def test_login_disabled_and_read_only_fail_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "personal_login", False)
    assert client.post("/api/remote-login/start").status_code == 403
    monkeypatch.setattr(settings, "personal_login", True)
    monkeypatch.setattr(settings, "read_only", True)
    assert client.post("/api/remote-login/start").status_code == 403


def test_cross_origin_controls_are_blocked(client):
    assert client.post(f"/api/remote-login/{remote.desktop.id}/finish",
                       headers={"origin": "https://attacker.example"}).status_code == 403


def test_remote_login_requires_https(client):
    assert client.post("http://testserver/api/remote-login/start").status_code == 403


@pytest.mark.parametrize("origin,owner,state,attached", [
    ("https://attacker.example", "admin@example.org", "ready", False),
    ("http://testserver", "other@example.org", "ready", False),
    ("http://testserver", "admin@example.org", "expired", False),
    ("http://testserver", "admin@example.org", "ready", True),
    (None, "admin@example.org", "ready", False),
])
def test_websocket_rejects_unauthorised_connection(client, origin, owner, state, attached):
    from starlette.websockets import WebSocketDisconnect
    s = remote.desktop
    s.owner, s.state, s.attached = owner, state, attached
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(f"/api/remote-login/{s.id}/socket",
                                      headers={"origin": origin} if origin else {}):
            pass
    assert caught.value.code == 1008


def test_assets_cannot_read_outside_novnc(client, monkeypatch, tmp_path):
    monkeypatch.setattr(remote, "NOVNC", tmp_path)
    (tmp_path / "core").mkdir()
    (tmp_path / "core/rfb.js").write_text("export default class RFB {}")
    (tmp_path / "secret.json").write_text("private")
    base = f"/api/remote-login/{remote.desktop.id}/assets"
    assert client.get(base + "/core/rfb.js").status_code == 200
    assert client.get(base + "/secret.json").status_code == 404
    assert client.get(base + "/core/%2e%2e/%2e%2e/secret.js").status_code == 404


def test_websocket_relays_binary_and_releases_single_viewer(client, monkeypatch):
    async def scenario_connection(*args):
        queue = asyncio.Queue()
        class Reader:
            async def read(self, _size): return await queue.get()
        class Writer:
            def write(self, data): queue.put_nowait(data)
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass
        assert args == ("127.0.0.1", remote.desktop.port)
        return Reader(), Writer()
    monkeypatch.setattr(asyncio, "open_connection", scenario_connection)
    s = remote.desktop
    with client.websocket_connect(f"/api/remote-login/{s.id}/socket",
                                  headers={"origin": "http://testserver"}) as ws:
        ws.send_bytes(b"RFB-test-binary\x00\xff")
        assert ws.receive_bytes() == b"RFB-test-binary\x00\xff"
    assert not s.attached


@pytest.mark.parametrize("hour,minute,blocked", [(7,59,True), (8,0,False), (21,59,True), (22,0,True), (23,0,True)])
def test_nightly_window_boundaries(monkeypatch, hour, minute, blocked):
    monkeypatch.setattr(settings, "scrape_window_start_at", "22:00")
    monkeypatch.setattr(settings, "scrape_hard_stop_at", "08:00")
    monkeypatch.setattr(settings, "scrape_window_timezone", "Asia/Kolkata")
    now = datetime(2026, 11, 2, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
    if blocked:
        with pytest.raises(RuntimeError):
            remote.login_seconds(now)
    else:
        assert remote.login_seconds(now) == 900


def test_login_shortens_before_nightly_scrape(monkeypatch):
    monkeypatch.setattr(settings, "scrape_window_start_at", "22:00")
    monkeypatch.setattr(settings, "scrape_hard_stop_at", "08:00")
    monkeypatch.setattr(settings, "scrape_window_timezone", "Asia/Kolkata")
    assert remote.login_seconds(datetime(2026,11,2,21,50,tzinfo=ZoneInfo("Asia/Kolkata"))) == 540


def test_profile_is_reserved_until_cleanup_finishes(monkeypatch):
    async def scenario():
        task = asyncio.create_task(asyncio.Event().wait())
        s = remote.Desktop(owner="admin@example.org", state="connected", task=task)
        monkeypatch.setattr(remote, "desktop", s)
        with pytest.raises(RuntimeError, match="login window"):
            remote.require_profile_free()
        from app.services.scraper_manager import ScraperManager
        with pytest.raises(RuntimeError, match="login window"):
            await ScraperManager().start()
        await remote.shutdown()
        assert task.done()
        remote.require_profile_free()
    asyncio.run(scenario())


def test_login_rejects_running_scrape_before_launch(monkeypatch):
    from app.services.scraper_manager import manager
    monkeypatch.setattr(remote, "desktop", None)
    monkeypatch.setattr(manager, "state", "running")
    with pytest.raises(RuntimeError, match="scrape is running"):
        asyncio.run(remote.start("admin@example.org"))


def test_hard_deadline_cancels_worker_and_runs_cleanup(monkeypatch):
    cleaned = []
    async def worker(_s):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)
    monkeypatch.setattr(remote, "_run_desktop", worker)
    s = remote.Desktop(owner="a", deadline=time.monotonic())
    asyncio.run(remote._run(s))
    assert s.state == "expired" and cleaned == [True]


@pytest.mark.parametrize("signed_in,expected", [(True, "connected"), (False, "unverified")])
def test_session_saved_only_after_positive_evidence_and_chrome_closed(monkeypatch, tmp_path, signed_in, expected):
    events = []
    class Process:
        returncode = None
        stdout = None
        def __init__(self):
            self.stdout = self
        async def readline(self): return b"91\n"
        def terminate(self): self.returncode = 0; events.append("terminate")
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        events.append(args)
        return Process()
    class Page:
        async def goto(self, *args, **kwargs): pass
        def is_closed(self): return False
        async def evaluate(self, _js): return {"logout": signed_in, "signin": not signed_in}
    class Context:
        pages = [Page()]
        async def storage_state(self): return {"cookies": [{"name":"session", "value":"test"}]}
        async def close(self): events.append("chrome closed")
    class Playwright:
        def __init__(self): self.chromium = self
        async def start(self): return self
        async def stop(self): events.append("playwright stopped")
        async def launch_persistent_context(self, *args, **kwargs):
            assert kwargs["env"]["DISPLAY"] == ":91"
            return Context()
    class Writer:
        def close(self): pass
        async def wait_closed(self): pass
    async def connect(*args): return None, Writer()
    async def no_sleep(_seconds): pass
    import playwright.async_api
    monkeypatch.setattr(playwright.async_api, "async_playwright", Playwright)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(devaid_auth, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(devaid_auth, "_CONNECTED_MARKER", tmp_path / ".connected")
    monkeypatch.setattr(devaid_auth, "import_session_state", lambda _s: events.append("saved"))
    s = remote.Desktop(owner="a", deadline=time.monotonic() + 900)
    s.finish.set()
    asyncio.run(remote._run(s))
    assert s.state == expected
    assert events.count("terminate") == 2
    assert "chrome closed" in events and "playwright stopped" in events
    assert (tmp_path / ".connected").exists() == signed_in
    if signed_in:
        assert events.index("chrome closed") < events.index("saved")
    else:
        assert "saved" not in events
    vnc = next(e for e in events if isinstance(e, tuple) and e[0] == "x11vnc")
    assert "-localhost" in vnc
