"""init_db() on an up-to-date database must not need the write lock.

Scripts call init_db() before they start. On 2026-10-12 the deploy's
intelligence backfill failed with "database is locked" inside init_db(): its
one-time deadline_state UPDATE ran every time (an UPDATE takes the write lock
even when it matches nothing) while the API's startup maintenance was writing.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _init(db: Path, busy_ms: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "LOP_DATABASE_URL": f"sqlite:///{db}",
           "LOP_SQLITE_BUSY_TIMEOUT_MS": str(busy_ms), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-c", "from app.database.db import init_db; init_db()"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)


def test_second_init_db_succeeds_while_another_process_holds_the_write_lock(tmp_path):
    db = tmp_path / "lock.db"
    first = _init(db, 30000)
    assert first.returncode == 0, first.stderr[-2000:]

    holder = sqlite3.connect(db, timeout=1)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")                 # take the write lock and keep it
    holder.execute("UPDATE scrape_lease SET label = 'held by test' WHERE id = 1")
    try:
        second = _init(db, 1000)                     # would fail after 1s if it wrote
    finally:
        holder.rollback()
        holder.close()
    assert second.returncode == 0, second.stderr[-2000:]


def test_busy_timeout_comes_from_the_environment(monkeypatch):
    from app.database import db as dbmod

    monkeypatch.setenv("LOP_SQLITE_BUSY_TIMEOUT_MS", "600000")
    conn = sqlite3.connect(":memory:")
    dbmod._sqlite_pragmas(conn, None)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 600000

    monkeypatch.setenv("LOP_SQLITE_BUSY_TIMEOUT_MS", "not-a-number")
    conn = sqlite3.connect(":memory:")
    dbmod._sqlite_pragmas(conn, None)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
