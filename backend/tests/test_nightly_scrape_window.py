"""Nightly scrape window: health check at 06:00, hard stop at 08:00."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pytest


class _Job:
    next_run_time = None


class _Recorder:
    def __init__(self, **kwargs):
        self.running = False
        self.jobs = {}

    def start(self):
        self.running = True

    def add_job(self, func, trigger=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = (func, trigger)
        return _Job()

    def remove_job(self, job_id):
        self.jobs.pop(job_id)

    def get_job(self, job_id):
        return _Job() if job_id in self.jobs else None


@pytest.fixture
def sched(monkeypatch, tmp_path):
    from app.services import scheduler as mod
    monkeypatch.setattr(mod, "AsyncIOScheduler", _Recorder)
    monkeypatch.setattr(mod, "_STATE_FILE", tmp_path / "schedule.json")
    s = mod.ScrapeScheduler()
    monkeypatch.setattr(s, "apply_email_settings", lambda: None)
    monkeypatch.setattr(s, "_install_deadline_audit", lambda: None)
    return s


class _FakeManager:
    def __init__(self, state, idle_s):
        self.state = state
        self.idle_s = idle_s
        self.stopped = False

    def health_report(self, stall_s):
        return {"state": self.state, "elapsed_min": 360.0,
                "idle_min": round(self.idle_s / 60, 1),
                "stalled": self.state == "running" and self.idle_s >= stall_s,
                "statuses": {"completed": 60, "running": 5, "queued": 20},
                "total": 85, "saved": 412,
                "running": [{"source": "ADB", "pages": 40, "saved": 3, "idle_min": 1.0}]}

    async def stop(self):
        self.stopped = True


@pytest.fixture
def alerts(monkeypatch):
    from app.services import email_service
    sent = []
    monkeypatch.setattr(email_service, "send_alert",
                        lambda subject, body, to=None: sent.append((subject, body)))
    return sent


def test_window_jobs_run_at_six_and_eight_india_time(sched):
    sched.start()
    for job_id, hour in (("scrape-health-check", "6"), ("scrape-hard-stop", "8")):
        _, trigger = sched._scheduler.jobs[job_id]
        assert str(trigger.timezone) == "Asia/Kolkata"
        assert f"hour='{hour}'" in str(trigger) and "minute='0'" in str(trigger)


def test_blank_time_turns_a_window_job_off(sched, monkeypatch):
    from app.services import scheduler as mod
    monkeypatch.setattr(mod.settings, "scrape_health_check_at", "")
    sched.start()
    assert "scrape-health-check" not in sched._scheduler.jobs
    assert "scrape-hard-stop" in sched._scheduler.jobs


def test_health_check_lets_a_progressing_scrape_continue(sched, monkeypatch, alerts):
    from app.services import scheduler as mod
    fake = _FakeManager("running", idle_s=60)
    monkeypatch.setattr(mod, "manager", fake)
    asyncio.run(sched._window_health_check())
    assert not fake.stopped
    assert len(alerts) == 1 and "Healthy" in alerts[0][1]
    assert "ADB: page 40" in alerts[0][1]


def test_health_check_stops_a_stuck_scrape(sched, monkeypatch, alerts):
    from app.services import scheduler as mod
    monkeypatch.setattr(mod.settings, "scrape_stall_minutes", 30)
    fake = _FakeManager("running", idle_s=45 * 60)
    monkeypatch.setattr(mod, "manager", fake)
    asyncio.run(sched._window_health_check())
    assert fake.stopped
    assert "STUCK" in alerts[0][1]


def test_health_check_is_silent_when_the_scrape_already_finished(sched, monkeypatch, alerts):
    from app.services import scheduler as mod
    fake = _FakeManager("idle", idle_s=0)
    monkeypatch.setattr(mod, "manager", fake)
    asyncio.run(sched._window_health_check())
    assert not fake.stopped and alerts == []


def test_hard_stop_stops_a_running_scrape(sched, monkeypatch, alerts):
    from app.services import scheduler as mod
    fake = _FakeManager("running", idle_s=10)
    monkeypatch.setattr(mod, "manager", fake)
    asyncio.run(sched._window_hard_stop())
    assert fake.stopped and "window closed" in alerts[0][0]


def test_hard_stop_does_nothing_when_idle(sched, monkeypatch, alerts):
    from app.services import scheduler as mod
    fake = _FakeManager("idle", idle_s=0)
    monkeypatch.setattr(mod, "manager", fake)
    asyncio.run(sched._window_hard_stop())
    assert not fake.stopped and alerts == []


def test_health_report_tells_stuck_from_slow():
    from app.services.scraper_manager import ScraperManager
    m = ScraperManager()
    m.state = "running"
    m.started_at = time.monotonic() - 3600
    m.progress = {"adb": {"display_name": "ADB", "status": "running", "pages": 4, "saved": 1},
                  "bond": {"display_name": "Bond", "status": "completed", "saved": 2}}
    m.last_activity = time.monotonic() - 40 * 60
    m.source_activity = {"adb": m.last_activity}
    report = m.health_report(stall_s=30 * 60)
    assert report["stalled"] and report["saved"] == 3
    assert report["statuses"] == {"running": 1, "completed": 1}
    assert report["running"][0]["source"] == "ADB"
    m.last_activity = time.monotonic()
    assert not m.health_report(stall_s=30 * 60)["stalled"]


def test_sources_not_completed_recently_go_first(monkeypatch):
    from app.services import scraper_manager as mod

    rows = [("adb", datetime(2026, 10, 13, 1)), ("adb", datetime(2026, 10, 14, 1)),
            ("bond", datetime(2026, 10, 12, 1))]

    class _Result:
        def all(self):
            return rows

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt):
            return _Result()

    monkeypatch.setattr(mod, "session_scope", lambda: _Session())
    srcs = [SimpleNamespace(name=n) for n in ("adb", "bond", "unpp", "worldbank")]
    ordered = [s.name for s in mod.ScraperManager._least_recent_first(srcs)]
    # Never completed first (registry order kept), then oldest completion.
    assert ordered == ["unpp", "worldbank", "bond", "adb"]
