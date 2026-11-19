from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import OperationalError


class _Session:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, _statement):
        if self.error:
            raise self.error

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _operational(message: str) -> OperationalError:
    return OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError(message))


def test_immediate_session_retries_a_transient_writer_lock(monkeypatch):
    from app.database import db as dbmod

    locked = _Session(_operational("database is locked"))
    available = _Session()
    sessions = iter((locked, available))
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(dbmod.time, "sleep", lambda _seconds: None)

    with dbmod.immediate_session_scope(wait_seconds=5, retry_seconds=0.01) as db:
        assert db is available

    assert locked.rolled_back and locked.closed
    assert available.committed and available.closed


def test_immediate_session_does_not_hide_non_lock_database_errors(monkeypatch):
    from app.database import db as dbmod

    broken = _Session(_operational("disk I/O error"))
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: broken)

    with pytest.raises(OperationalError, match="disk I/O error"):
        with dbmod.immediate_session_scope(wait_seconds=5):
            pass

    assert broken.rolled_back and broken.closed
