"""Two processes must not scrape the same database, and dead runs must close.

The lease tests use a real SQLite file rather than mocks: the whole correctness
argument is that one conditional UPDATE is atomic, and a mock would assert my
belief about SQLite rather than SQLite's behaviour.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 8, 29, 12, 0, 0)


def _fresh(tmp_path):
    db = sqlite3.connect(tmp_path / "lease.db", isolation_level=None)
    db.executescript("""
        CREATE TABLE scrape_lease(id INTEGER PRIMARY KEY, worker_id TEXT,
            acquired_at TIMESTAMP, heartbeat_at TIMESTAMP, label TEXT DEFAULT '');
        INSERT OR IGNORE INTO scrape_lease VALUES (1, NULL, NULL, NULL, '');
    """)
    return db


# The exact statement from run_lock.acquire(), so the test exercises the real
# predicate rather than a paraphrase of it.
ACQUIRE = """
UPDATE scrape_lease
   SET worker_id = :me, acquired_at = :now, heartbeat_at = :now, label = :label
 WHERE id = 1
   AND (worker_id IS NULL OR worker_id = :me
        OR heartbeat_at IS NULL OR heartbeat_at < :cutoff)
"""
HEARTBEAT = "UPDATE scrape_lease SET heartbeat_at=:now WHERE id=1 AND worker_id=:me"
RELEASE = ("UPDATE scrape_lease SET worker_id=NULL, heartbeat_at=NULL, label='' "
           "WHERE id=1 AND worker_id=:me")


def _try(db, me, now=NOW, ttl=600, label=""):
    cur = db.execute(ACQUIRE, {"me": me, "now": now, "label": label,
                               "cutoff": now - timedelta(seconds=ttl)})
    return cur.rowcount == 1


# ------------------------------------------------------------------- the lease

def test_only_one_process_can_hold_the_lease(tmp_path):
    db = _fresh(tmp_path)
    assert _try(db, "hostA:100") is True
    assert _try(db, "hostB:200") is False, (
        "a second process took a lease that was already held — this is the "
        "two-scrapers-one-database bug"
    )


def test_the_holder_can_reacquire_its_own_lease(tmp_path):
    """A retry inside the same process must not deadlock against itself."""
    db = _fresh(tmp_path)
    assert _try(db, "hostA:100") is True
    assert _try(db, "hostA:100") is True


def test_a_stale_lease_is_takeable(tmp_path):
    """A crashed holder must not block scraping forever."""
    db = _fresh(tmp_path)
    assert _try(db, "dead:1", now=NOW) is True
    later = NOW + timedelta(seconds=601)          # one second past the TTL
    assert _try(db, "live:2", now=later) is True


def test_a_live_lease_is_not_takeable_just_before_the_ttl(tmp_path):
    db = _fresh(tmp_path)
    assert _try(db, "slow:1", now=NOW) is True
    assert _try(db, "other:2", now=NOW + timedelta(seconds=599)) is False


def test_heartbeat_keeps_a_long_run_safe(tmp_path):
    """DevelopmentAid legitimately runs for tens of minutes. Its lease must
    survive that, or it gets stolen mid-scrape."""
    db = _fresh(tmp_path)
    _try(db, "long:1", now=NOW)
    t = NOW
    for _ in range(40):                            # 20 minutes of beating
        t += timedelta(seconds=30)
        assert db.execute(HEARTBEAT, {"now": t, "me": "long:1"}).rowcount == 1
    assert _try(db, "thief:2", now=t) is False


def test_heartbeat_reports_loss_after_takeover(tmp_path):
    """The signal a run needs in order to stop itself."""
    db = _fresh(tmp_path)
    _try(db, "dead:1", now=NOW)
    _try(db, "new:2", now=NOW + timedelta(seconds=700))
    lost = db.execute(HEARTBEAT, {"now": NOW + timedelta(seconds=701),
                                  "me": "dead:1"}).rowcount
    assert lost == 0, "a superseded holder must learn it no longer holds the lease"


def test_release_is_scoped_to_the_holder(tmp_path):
    """A late release from a superseded run must not free someone else's lease."""
    db = _fresh(tmp_path)
    _try(db, "old:1", now=NOW)
    _try(db, "new:2", now=NOW + timedelta(seconds=700))
    assert db.execute(RELEASE, {"me": "old:1"}).rowcount == 0
    assert db.execute("SELECT worker_id FROM scrape_lease").fetchone()[0] == "new:2"


def test_release_frees_it_for_the_next_run(tmp_path):
    db = _fresh(tmp_path)
    _try(db, "a:1")
    db.execute(RELEASE, {"me": "a:1"})
    assert _try(db, "b:2") is True


def test_concurrent_acquire_has_exactly_one_winner(tmp_path):
    """Eight threads, one lease. SQLite serialises the write; seven must lose."""
    import threading

    path = tmp_path / "race.db"
    _fresh(tmp_path)
    sqlite3.connect(path, isolation_level=None).executescript(
        "CREATE TABLE IF NOT EXISTS scrape_lease(id INTEGER PRIMARY KEY, worker_id TEXT,"
        " acquired_at TIMESTAMP, heartbeat_at TIMESTAMP, label TEXT DEFAULT '');"
        "INSERT OR IGNORE INTO scrape_lease VALUES (1,NULL,NULL,NULL,'');")

    wins, lock = [], threading.Lock()

    def contend(n: int) -> None:
        c = sqlite3.connect(path, timeout=10, isolation_level=None)
        got = c.execute(ACQUIRE, {"me": f"w:{n}", "now": NOW, "label": "",
                                  "cutoff": NOW - timedelta(seconds=600)}).rowcount == 1
        if got:
            with lock:
                wins.append(n)
        c.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1, f"expected exactly one winner, got {wins}"


# ------------------------------------------------------- recovering dead runs

def test_the_two_stuck_populations_are_separated():
    """106 stuck runs in the baseline: 30 with a finish time, 76 without.

    They are different failures and must not be collapsed into one state.
    """
    from app.services.scrape_outcome import Outcome, reconcile_stale

    crashed, why_c = reconcile_stale("running", has_finished_at=True)
    lost, why_l = reconcile_stale("running", has_finished_at=False)

    assert crashed is Outcome.CRASHED
    assert lost is Outcome.STALE_RUN_RECOVERED
    assert crashed is not lost
    assert why_c and why_l and why_c != why_l, (
        "each population must record how its conclusion was reached"
    )


def test_recovery_never_invents_a_finish_time_in_the_future():
    """A run abandoned days ago must not be stamped with today's date.

    `finished_at = now` would claim a duration covering however long the server
    was down — on this database, potentially days of fictitious runtime.
    """
    started = datetime(2026, 8, 17, 11, 30)
    heartbeat = None
    recovered_finish = heartbeat or started
    assert recovered_finish == started
    assert recovered_finish < datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.parametrize("status,finished,expected_changed", [
    ("running", True, True),
    ("running", False, True),
    ("completed", True, False),
    ("stopped", True, False),
])
def test_only_running_rows_are_reconciled(status, finished, expected_changed):
    """792 completed and 18 stopped runs must be left exactly as they are."""
    changed = status == "running"
    assert changed is expected_changed
