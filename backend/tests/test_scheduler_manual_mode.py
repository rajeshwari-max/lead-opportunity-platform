"""A scrape starts at a configured time, or because someone clicked Start.

Nothing else — and specifically not "the application restarted". Before this,
`ScrapeScheduler.start()` could launch a full ~85-source scrape the moment the
process came up, which meant a deploy, a crash-loop or a supervisor restart at
the wrong hour began an unbounded run nobody initiated.

These tests exercise the decision, not APScheduler: the scheduler object is
replaced with a recorder so no jobs are really registered and no event loop is
required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# --------------------------------------------------------------------- doubles

class FakeJob:
    next_run_time = None


class RecordingScheduler:
    """Records what the real APScheduler would have been asked to do."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running = False
        self.jobs: dict[str, object] = {}

    def start(self) -> None:
        self.running = True

    def add_job(self, func, trigger=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = (func, trigger)
        return FakeJob()

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        del self.jobs[job_id]

    def get_job(self, job_id):
        return FakeJob() if job_id in self.jobs else None

    def shutdown(self, wait=False):
        self.running = False


@pytest.fixture
def sched(monkeypatch, tmp_path):
    """A ScrapeScheduler wired to the recorder, with state persisted to tmp."""
    from app.services import scheduler as mod

    monkeypatch.setattr(mod, "AsyncIOScheduler", RecordingScheduler)
    monkeypatch.setattr(mod, "_STATE_FILE", tmp_path / "schedule.json")
    s = mod.ScrapeScheduler()
    # Neither of these should run during a start() under test.
    monkeypatch.setattr(s, "apply_email_settings", lambda: None)
    monkeypatch.setattr(s, "_install_deadline_audit", lambda: None)
    return s


def _weekly_monday_0200():
    from app.schemas.opportunity import ScheduleRequest

    return ScheduleRequest(mode="weekly", hour=2, minute=0)


# ----------------------------------------------------------------------- tests

def test_manual_mode_registers_no_scrape_job(sched):
    from app.schemas.opportunity import ScheduleRequest

    sched.configure(ScheduleRequest(mode="manual"))
    sched.start()
    assert "scheduled-scrape" not in sched._scheduler.jobs, (
        "manual mode must mean no automatic scrape, registered or otherwise"
    )


def test_restart_does_not_start_a_scrape_by_default(sched, monkeypatch):
    """The regression this phase exists for."""
    started: list[str] = []
    monkeypatch.setattr(sched, "_scrape_all", lambda: started.append("scrape"))
    monkeypatch.setattr(sched, "_missed_run_today", lambda: True)   # slot was missed
    sched.current = _weekly_monday_0200()

    sched.start()

    assert started == [], (
        "restarting the application must not begin a scrape — catch-up is off"
    )


def test_catchup_runs_only_when_explicitly_enabled(sched, monkeypatch):
    import asyncio

    from app.core.config import settings

    monkeypatch.setattr(settings, "scheduler_catchup_on_restart", True)
    created: list[object] = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro))
    monkeypatch.setattr(sched, "_scrape_all", lambda: "coro")
    monkeypatch.setattr(sched, "_missed_run_today", lambda: True)
    sched.current = _weekly_monday_0200()

    sched.start()

    assert created == ["coro"], "opting in must restore the old behaviour exactly"


def test_scheduled_slot_still_registers_its_job(sched):
    """Turning catch-up off must not turn scheduling off."""
    sched.current = _weekly_monday_0200()
    sched.start()
    assert "scheduled-scrape" in sched._scheduler.jobs


def test_job_defaults_are_stated_not_inherited(sched):
    """max_instances / coalesce / misfire_grace_time are load-bearing here."""
    from app.core.config import settings

    defaults = sched._scheduler.kwargs["job_defaults"]
    assert defaults["max_instances"] == 1
    assert defaults["coalesce"] is True, (
        "several missed slots must produce ONE run on recovery, not one each"
    )
    assert defaults["misfire_grace_time"] == settings.scheduler_misfire_grace_s
    assert defaults["misfire_grace_time"] > 1, (
        "APScheduler's 1s default drops a job whose loop was briefly busy — "
        "and eight full-table backfills run at startup"
    )


def test_missed_run_today_compares_clocks_consistently(sched, monkeypatch):
    """last_run is stored as naive UTC; the comparison must read UTC too.

    On a UTC+5:30 host a run at 19:00 local is stamped with the NEXT UTC date.
    Comparing that against a naive local `now.date()` reports 'not run today'
    for a run that happened minutes ago — and with catch-up enabled, that starts
    a second scrape on top of the first.
    """
    from app.services import scheduler as mod

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc.replace(tzinfo=timezone.utc) if tz else now_utc + timedelta(hours=5, minutes=30)

    monkeypatch.setattr(mod, "datetime", FixedDatetime)
    sched.current = _weekly_monday_0200()
    sched.last_run = now_utc               # ran seconds ago, in UTC

    if sched._missed_run_today():
        pytest.fail(
            "a run recorded seconds ago was reported as missed — the UTC/local "
            "mismatch is back"
        )
