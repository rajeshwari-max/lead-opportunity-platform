"""A scrape starts at a configured time, a once-per-day recovery, or manually.

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


def test_integrity_schedule_runs_in_manual_mode_in_india_time(sched, monkeypatch):
    from app.services import data_integrity, deadline_audit
    calls = []
    monkeypatch.delattr(sched, '_install_deadline_audit')
    monkeypatch.setattr(data_integrity, 'maintain_database', lambda: calls.append('identity'))
    monkeypatch.setattr(deadline_audit, 'audit_deadlines', lambda: calls.append('deadlines'))
    sched.start()
    run, trigger = sched._scheduler.jobs['deadline-audit']
    assert str(trigger.timezone) == 'Asia/Kolkata'
    # After the 00:00-08:00 scrape window, before the 09:00 digest.
    assert 'hour=\'8\'' in str(trigger) and 'minute=\'15\'' in str(trigger)
    run()
    assert calls == ['identity', 'deadlines']


class RecordingScheduler:
    """Records what the real APScheduler would have been asked to do."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running = False
        self.jobs: dict[str, object] = {}
        self.job_options: dict[str, dict] = {}

    def start(self) -> None:
        self.running = True

    def add_job(self, func, trigger=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = (func, trigger)
        self.job_options[id] = kw
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


def test_restart_recovers_one_missed_scheduled_run(sched, monkeypatch):
    import asyncio

    created: list[object] = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro))
    monkeypatch.setattr(sched, "_scrape_all", lambda: "coro")
    monkeypatch.setattr(sched, "_missed_run_today", lambda: True)
    sched.current = _weekly_monday_0200()

    sched.start()

    assert created == [], "restart must wait for the next configured slot"


def test_scheduled_slot_still_registers_its_job(sched, monkeypatch):
    sched.current = _weekly_monday_0200()
    # Registration only. Without this, on a real Monday after 02:00 start()
    # takes the catch-up branch and calls asyncio.create_task outside a running
    # loop, so this test failed one day a week (and blocked that day's deploy).
    monkeypatch.setattr(sched, "_missed_run_today", lambda: False)
    sched.start()
    assert "scheduled-scrape" in sched._scheduler.jobs
    options = sched._scheduler.job_options["scheduled-scrape"]
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert options["misfire_grace_time"] == 60


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


def test_two_pm_india_trigger_is_eight_thirty_utc(sched):
    from app.schemas.opportunity import ScheduleRequest
    sched.configure(ScheduleRequest(mode="daily", hour=14, timezone="Asia/Kolkata"))
    trigger = sched._scheduler.jobs["scheduled-scrape"][1]
    fire = trigger.get_next_fire_time(None, datetime(2026, 10, 12, 0, tzinfo=timezone.utc))
    assert fire.astimezone(timezone.utc) == datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc)


def test_invalid_cron_keeps_previous_job(sched):
    from app.schemas.opportunity import ScheduleRequest
    sched.configure(ScheduleRequest(mode="daily", hour=14, timezone="Asia/Kolkata"))
    previous = sched._scheduler.jobs["scheduled-scrape"]
    with pytest.raises(ValueError):
        sched.configure(ScheduleRequest(mode="cron", cron="bad"))
    assert sched._scheduler.jobs["scheduled-scrape"] == previous
    assert sched.current.hour == 14


@pytest.mark.parametrize("statuses, expected", [
    (["completed", "completed"], "success"),
    (["completed", "failed"], "partial_success"),
    (["failed", "failed"], "failed"),
    (["stopped"], "stopped"),
    ([], "skipped"),
])
def test_run_outcome_is_honest(statuses, expected):
    from app.services.scheduler import scrape_outcome
    assert scrape_outcome({str(i): {"status": s} for i, s in enumerate(statuses)}) == expected


def test_requested_two_slots_in_india_time(sched):
    from app.schemas.opportunity import ScheduleRequest
    sched.configure(ScheduleRequest(mode="cron", cron="0 7,13 * * *", timezone="Asia/Kolkata"))
    trigger = sched._scheduler.jobs["scheduled-scrape"][1]
    first = trigger.get_next_fire_time(None, datetime(2026, 10, 13, 0, tzinfo=timezone.utc))
    second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
    third = trigger.get_next_fire_time(second, second + timedelta(seconds=1))
    assert first.astimezone(timezone.utc) == datetime(2026, 10, 13, 1, 30, tzinfo=timezone.utc)
    assert second.astimezone(timezone.utc) == datetime(2026, 10, 13, 7, 30, tzinfo=timezone.utc)
    assert third.astimezone(timezone.utc) == datetime(2026, 10, 14, 1, 30, tzinfo=timezone.utc)
