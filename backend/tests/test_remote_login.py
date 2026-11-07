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
        # Wait for the server's close acknowledgement before TestClient tears
        # down its portal, rather than cancelling relay cleanup mid-flight.
        ws.close()
        assert ws.receive()["type"] == "websocket.close"
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


def test_stale_lockfile_removed_without_touching_saved_login(monkeypatch, tmp_path):
    (tmp_path / "lockfile").write_text("")
    (tmp_path / ".connected").write_text("connected")
    (tmp_path / "Cookies").write_text("saved login")
    monkeypatch.setattr(remote, "_profile_processes", lambda _p: [])
    remote._recover_profile_locks(tmp_path)
    assert not (tmp_path / "lockfile").exists()
    assert (tmp_path / ".connected").read_text() == "connected"
    assert (tmp_path / "Cookies").read_text() == "saved login"


def test_live_chrome_lock_is_never_removed(monkeypatch, tmp_path):
    (tmp_path / "lockfile").write_text("")
    monkeypatch.setattr(remote, "_profile_processes", lambda _p: [1234])
    with pytest.raises(RuntimeError, match="PID 1234"):
        remote._recover_profile_locks(tmp_path)
    assert (tmp_path / "lockfile").exists()


def test_uncertain_inspection_keeps_lock(monkeypatch, tmp_path):
    (tmp_path / "lockfile").write_text("")
    def fail(_p): raise RuntimeError("Could not check processes")
    monkeypatch.setattr(remote, "_profile_processes", fail)
    with pytest.raises(RuntimeError): remote._recover_profile_locks(tmp_path)
    assert (tmp_path / "lockfile").exists()


def test_browser_opening_during_check_keeps_lock(monkeypatch, tmp_path):
    (tmp_path / "lockfile").write_text("")
    results = iter([[], [1234]])
    monkeypatch.setattr(remote, "_profile_processes", lambda _p: next(results))
    with pytest.raises(RuntimeError, match="Chrome opened"):
        remote._recover_profile_locks(tmp_path)
    assert (tmp_path / "lockfile").exists()


@pytest.mark.parametrize("owner", ["dead", "alive", "unknown", "other-host"])
def test_singleton_owner_checked_before_cleanup(monkeypatch, tmp_path, owner):
    # Mock readlink so this Linux lock protocol can be tested on Windows too.
    (tmp_path / "SingletonLock").write_text("")
    monkeypatch.setattr(remote, "_profile_processes", lambda _p: [])
    monkeypatch.setattr(remote.socket, "gethostname", lambda: "this-host")
    monkeypatch.setattr(remote.os, "readlink", lambda _p: (
        "different-host-1234" if owner == "other-host" else "this-host-1234"))
    def signal_owner(pid, sig):
        assert (pid, sig) == (1234, 0)
        if owner == "dead": raise ProcessLookupError()
        if owner == "unknown": raise PermissionError()
    monkeypatch.setattr(remote.os, "kill", signal_owner)
    if owner == "dead":
        remote._recover_profile_locks(tmp_path)
        assert not (tmp_path / "SingletonLock").exists()
    else:
        with pytest.raises(RuntimeError): remote._recover_profile_locks(tmp_path)
        assert (tmp_path / "SingletonLock").exists()


def test_process_scan_matches_exact_profile_and_both_argument_styles(monkeypatch, tmp_path):
    monkeypatch.setattr(remote.os, "getuid", lambda: tmp_path.stat().st_uid, raising=False)
    profile = tmp_path / "profile"
    for pid, args in [(1, ["/opt/google/chrome/chrome", f"--user-data-dir={profile}"]),
                      (2, ["/opt/google/chrome/chrome", "--user-data-dir", str(profile)]),
                      (3, ["/opt/google/chrome/chrome", f"--user-data-dir={profile}-other"]),
                      (4, ["python", f"--user-data-dir={profile}"])]:
        folder = tmp_path / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in args))
    assert remote._profile_processes(profile, tmp_path) == [1, 2]


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


@pytest.mark.parametrize("result,action,expected", [(0, "finish", "connected"),
    (3, "finish", "unverified"), (0, "cancel", "cancelled")])
def test_native_login_closes_chrome_before_separate_verification(monkeypatch, tmp_path, result, action, expected):
    events = []
    processes = {}
    class Process:
        returncode = None
        stdout = None
        def __init__(self, name):
            self.name = name
            self.stdout = self
            self.pid = 1000 + len(processes)
            processes[self.pid] = self
        async def readline(self): return b"91\n"
        def terminate(self):
            self.returncode = 0
            events.append(self.name + " closed")
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        events.append(args)
        proc = Process(args[0])
        if args[0] == "google-chrome":
            assert args == ("google-chrome", f"--user-data-dir={tmp_path}",
                            "--no-first-run", devaid_auth.LOGIN_URL)
            assert kwargs["env"]["DISPLAY"] == ":91"
            assert kwargs["start_new_session"] is True
        elif "--verify" in args:
            assert "google-chrome closed" in events
            assert args[1:] == ("-m", "app.services.remote_login", "--verify")
            assert kwargs["start_new_session"] is True
            proc.returncode = result
        return proc
    class Writer:
        def close(self): pass
        async def wait_closed(self): pass
    async def connect(*args): return None, Writer()
    async def no_sleep(_seconds): pass
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    # On Linux final cleanup addresses Chrome's owned process group instead of
    # only its parent process. Make that platform-specific path observable to
    # this fake process just as terminate() is on Windows.
    monkeypatch.setattr(remote.os, "killpg",
                        lambda pid, _sig: processes[pid].terminate(), raising=False)
    monkeypatch.setattr(devaid_auth, "PROFILE_DIR", tmp_path)
    s = remote.Desktop(owner="a", deadline=time.monotonic() + 900)
    getattr(s, action).set()
    asyncio.run(remote._run(s))
    assert s.state == expected
    assert "google-chrome closed" in events
    assert "Xvfb closed" in events and "x11vnc closed" in events
    assert any(isinstance(e, tuple) and "--verify" in e for e in events) == (action == "finish")
    vnc = next(e for e in events if isinstance(e, tuple) and e[0] == "x11vnc")
    assert "-localhost" in vnc


def test_dedicated_profile_never_loads_old_uploaded_session(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(remote, "desktop", None)
    monkeypatch.setattr(devaid_auth, "PROFILE_DIR", tmp_path)
    monkeypatch.setattr(devaid_auth, "_CONNECTED_MARKER", tmp_path / ".connected")
    monkeypatch.setattr(devaid_auth, "has_session_file", lambda: True)
    # Must not consult the personal Chrome override when verifying this login.
    monkeypatch.setattr(devaid_auth, "_own_chrome_dir", lambda: pytest.fail("personal Chrome used"))
    monkeypatch.setattr(devaid_auth, "_context_from_session", lambda *a: pytest.fail("old session used"))
    monkeypatch.setattr(devaid_auth, "_mask_headless", lambda c: c)
    launches = []
    context = object()
    def launch(profile, **kwargs):
        launches.append(profile)
        return context
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    assert devaid_auth.open_persistent(pw, dedicated_profile=True) is context
    assert launches == [str(tmp_path)]


def test_done_signals_chrome_parent_before_group_cleanup(monkeypatch):
    from types import SimpleNamespace
    events = []
    monkeypatch.setattr(remote, "os", SimpleNamespace(name="posix",
        killpg=lambda pid, sig: events.append((pid, sig))))
    class Process:
        pid = 1234
        returncode = None
        def terminate(self):
            events.append("parent terminated")
            self.returncode = 0
        async def wait(self): return 0
    proc = Process()
    asyncio.run(remote._stop_process(proc, group=True, graceful=True))
    assert events == ["parent terminated"]
    # Even with the parent gone, the final cleanup addresses only its owned
    # group, in case a helper outlived it.
    asyncio.run(remote._stop_process(proc, group=True))
    assert events[-1] == (1234, remote.signal.SIGTERM)


@pytest.mark.parametrize("state", ["in", "out", "unknown"])
def test_post_login_verifier_saves_only_confirmed_fresh_profile(monkeypatch, tmp_path, state):
    from types import SimpleNamespace
    from contextlib import nullcontext
    import playwright.sync_api
    events = []
    class Page:
        def goto(self, *a, **k): pass
        def title(self): return "DevelopmentAid"
        def content(self): return "A normal rendered page"
        def wait_for_selector(self, *a, **k): pass
    context = SimpleNamespace(pages=[Page()], storage_state=lambda: {"cookies": [{"name": "test"}]})
    def open_profile(_pw, **kwargs):
        assert kwargs["dedicated_profile"] is True
        return context
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(devaid_auth, "open_persistent", open_profile)
    monkeypatch.setattr(devaid_auth, "membership_state", lambda _p: (state, "test evidence"))
    monkeypatch.setattr(devaid_auth, "import_session_state", lambda saved: events.append(saved))
    monkeypatch.setattr(devaid_auth, "_CONNECTED_MARKER", tmp_path / ".connected")
    monkeypatch.setattr(devaid_auth.site_auth, "close_owned", lambda _c: events.append("closed"))
    assert devaid_auth.verify_session(dedicated_profile=True, save=True) == (state == "in")
    assert (tmp_path / ".connected").exists() == (state == "in")
    assert any(isinstance(e, dict) for e in events) == (state == "in")
    assert events[-1] == "closed"
