"""Re-reading stored deadlines from the source's own text (deploy step 8a)."""
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status
import scripts.active_deadline_audit as backup_mod
import scripts.iso_inversion_audit as mod

TODAY = date(2026, 10, 16)


def test_iso_text_is_read_the_same_whatever_the_convention():
    assert mod.corrected_deadline("2026-09-01", None) == date(2026, 9, 1)
    assert mod.corrected_deadline("2026-09-01", "dayfirst") == date(2026, 9, 1)


def test_recorded_convention_is_respected():
    # A US source: 09/01/2026 is 1 September, not 9 January.
    assert mod.corrected_deadline("09/01/2026", "monthfirst") == date(2026, 9, 1)
    assert mod.corrected_deadline("09/01/2026", "dayfirst") == date(2026, 1, 9)


def test_ambiguous_text_without_a_convention_is_left_alone():
    assert mod.corrected_deadline("09/01/2026", None) is None
    # Unambiguous (day above 12) is fine without one.
    assert mod.corrected_deadline("25/01/2026", None) == date(2026, 1, 25)


def test_sentinels_and_blank_text_are_never_written():
    assert mod.corrected_deadline("9999-12-31", None) is None
    assert mod.corrected_deadline("", "dayfirst") is None
    assert mod.corrected_deadline("rolling", "dayfirst") is None


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)

    @contextmanager
    def scope():
        yield session
        session.commit()

    monkeypatch.setattr(mod, "session_scope", scope)
    monkeypatch.setattr(mod, "application_today", lambda: TODAY)
    backups = []
    monkeypatch.setattr(backup_mod, "backup_database",
                        lambda path: backups.append(path) or [str(path)])
    session.backups = backups
    yield session
    session.close()


def _add(db, uid, raw, stored, status=Status.ACTIVE, convention=None):
    db.add(Opportunity(unique_id=uid, title=uid, source_website="S", status=status,
                       deadline=stored, deadline_raw=raw,
                       deadline_convention=convention))
    db.flush()


def test_apply_fixes_a_wrong_future_date_and_expires_the_row(db, tmp_path):
    # Real deadline 1 Sep 2026 (passed) stored as 9 Jan 2027 -> showed Active.
    _add(db, "inverted", "2026-09-01", date(2027, 1, 9))
    # Right already: untouched.
    _add(db, "fine", "2026-12-01", date(2026, 12, 1))
    # US row stored correctly as 1 Sep; must NOT be flipped to 9 Jan.
    _add(db, "us", "09/01/2026", date(2026, 9, 1), status=Status.EXPIRED,
         convention="monthfirst")
    count = len(db.execute(select(Opportunity)).scalars().all())

    assert mod.audit(apply=True, examples=0, backup=str(tmp_path / "b.db")) == 0

    rows = {o.unique_id: o for o in db.execute(select(Opportunity)).scalars()}
    assert len(rows) == count                       # nothing deleted
    assert rows["inverted"].deadline == date(2026, 9, 1)
    assert rows["inverted"].status == Status.EXPIRED
    assert rows["inverted"].deadline_confidence == "reparsed"
    assert rows["fine"].deadline == date(2026, 12, 1)
    assert rows["us"].deadline == date(2026, 9, 1)
    assert len(db.backups) == 1


def test_dry_run_writes_nothing_and_takes_no_backup(db):
    _add(db, "inverted", "2026-09-01", date(2027, 1, 9))
    mod.audit(apply=False, examples=0)
    row = db.execute(select(Opportunity)).scalar_one()
    assert row.deadline == date(2027, 1, 9) and db.backups == []
