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
