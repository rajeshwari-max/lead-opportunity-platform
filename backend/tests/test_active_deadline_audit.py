from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status
from scripts.active_deadline_audit import archive_passed, backup_database, collect


TODAY = date(2026, 8, 31)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add(db, uid, deadline=None, state=None, raw=None):
    row = Opportunity(unique_id=uid, title=uid, source_website="Source",
                      status=Status.ACTIVE, deadline=deadline,
                      deadline_state=state, deadline_raw=raw)
    db.add(row)
    db.flush()
    return row


def test_report_has_all_six_requested_buckets(db):
    report = collect(db, TODAY)
    assert set(report["buckets"]) == {
        "active_deadline_null", "active_empty_or_invalid_deadline_text",
        "active_rolling_open_ended", "active_unknown_unassessed",
        "active_deadline_before_today", "active_deadline_today",
    }


def test_buckets_overlap_intentionally(db):
    add(db, "rolling", state="rolling")
    report = collect(db, TODAY)
    assert report["buckets"]["active_deadline_null"]["count"] == 1
    assert report["buckets"]["active_rolling_open_ended"]["count"] == 1


def test_invalid_deadline_text_is_counted(db):
    add(db, "bad", state="unknown", raw="end of next quarter")
    assert collect(db, TODAY)["buckets"]["active_empty_or_invalid_deadline_text"]["count"] == 1


def test_apply_expires_only_passed_dated_rows_and_deletes_nothing(db):
    past = add(db, "past", TODAY - timedelta(days=1), "dated")
    today = add(db, "today", TODAY, "dated")
    rolling = add(db, "rolling", None, "rolling")
    count_before = len(list(db.execute(select(Opportunity)).scalars()))
    assert archive_passed(db, TODAY) == 1
    db.flush()
    assert past.status == Status.EXPIRED
    assert today.status == Status.ACTIVE
    assert rolling.status == Status.ACTIVE
    assert len(list(db.execute(select(Opportunity)).scalars())) == count_before


def test_backup_refuses_a_missing_database(monkeypatch):
    missing = Path(__file__).resolve().parent / "__database_that_does_not_exist__.db"
    monkeypatch.setattr("scripts.active_deadline_audit.sqlite_database_path",
                        lambda: missing)
    with pytest.raises(FileNotFoundError):
        backup_database(missing.with_name("__unused_backup__.db"))
