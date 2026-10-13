"""run_lock.status() must cope with SQLite returning DATETIME as text.

It used raw SQL, which on SQLite yields strings, and subtracted that from a
datetime. Whenever a lease was held — e.g. the service restarted mid-scrape —
status() raised TypeError, so startup recovery could not release the lease.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from app.services import run_lock


def _fake_scope(row):
    class _Result:
        def first(self):
            return row

    class _Db:
        def execute(self, *a, **k):
            return _Result()

    @contextmanager
    def scope():
        yield _Db()
    return scope


def test_status_parses_text_timestamps(monkeypatch):
    beat = run_lock._now() - timedelta(minutes=20)
    row = ("ip-10-0-1-189:1234", str(beat - timedelta(hours=1)), str(beat), "scheduled")
    monkeypatch.setattr(run_lock, "session_scope", _fake_scope(row))
    monkeypatch.setattr(run_lock, "_ensure_row", lambda db: None)
    st = run_lock.status(ttl_s=600)
    assert st["held"] is True
    assert st["stale"] is True
    assert 1150 <= st["heartbeat_age_s"] <= 1260


def test_acquire_reports_a_live_holder_instead_of_crashing(tmp_path, monkeypatch):
    """A second scrape while one is running must get LeaseNotAcquired.

    It raised TypeError (text heartbeat minus datetime) — seen in the local
    errors.log on 2026-10-12 as "Task exception was never retrieved".
    """
    from contextlib import contextmanager

    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE scrape_lease (id INTEGER PRIMARY KEY, worker_id VARCHAR, "
            "acquired_at DATETIME, heartbeat_at DATETIME, label VARCHAR NOT NULL DEFAULT '')")

    @contextmanager
    def scope():
        s = Session(engine)
        try:
            yield s
            s.commit()
        finally:
            s.close()

    monkeypatch.setattr(run_lock, "session_scope", scope)
    monkeypatch.setattr(run_lock, "worker_id", lambda: "ec2:111")
    assert run_lock.acquire(label="first") == "ec2:111"

    monkeypatch.setattr(run_lock, "worker_id", lambda: "ec2:222")
    with pytest.raises(run_lock.LeaseNotAcquired) as caught:
        run_lock.acquire(label="second")
    assert "ec2:111" in str(caught.value)
