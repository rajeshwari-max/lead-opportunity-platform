from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status
from app.schemas.opportunity import OpportunityFilters
from app.services.actionable import (
    DeadlineState,
    IST_OFFSET_MINUTES,
    actionable_clause,
    strict_actionable_clause,
)
from app.services.filter_service import FilterService


TODAY = date(2026, 8, 31)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add(db, uid, deadline, state="dated", status=Status.ACTIVE, approved=False):
    row = Opportunity(unique_id=uid, title=uid, source_website="Test",
                      deadline=deadline, deadline_state=state, status=status,
                      approved=approved, verticals="Health")
    db.add(row)
    db.flush()
    return row


def ids(db, clause):
    return {row.unique_id for row in db.execute(
        select(Opportunity).where(clause)).scalars()}


def test_default_application_timezone_is_india():
    assert IST_OFFSET_MINUTES == 330


def test_strict_rule_keeps_today_and_future_only(db):
    add(db, "past", TODAY - timedelta(days=1))
    add(db, "today", TODAY)
    add(db, "future", TODAY + timedelta(days=1))
    add(db, "rolling", None, DeadlineState.ROLLING.value)
    add(db, "unknown", None, DeadlineState.UNKNOWN.value)
    assert ids(db, strict_actionable_clause(TODAY)) == {"today", "future"}


def test_administrative_rule_still_reaches_rolling_rows(db):
    add(db, "rolling", None, DeadlineState.ROLLING.value)
    assert ids(db, actionable_clause(TODAY)) == {"rolling"}


def test_strict_rule_rejects_dated_unknown_state(db):
    add(db, "unknown", TODAY + timedelta(days=1), DeadlineState.UNKNOWN.value)
    assert ids(db, strict_actionable_clause(TODAY)) == set()


def test_filter_service_defaults_to_strict_rule(db, monkeypatch):
    add(db, "future", TODAY + timedelta(days=1))
    add(db, "rolling", None, DeadlineState.ROLLING.value)
    monkeypatch.setattr("app.services.actionable.application_today", lambda: TODAY)
    # Pass the date through the SQL clause by patching the service's imported
    # builder; this keeps the assertion independent of the wall clock.
    monkeypatch.setattr("app.services.filter_service.strict_actionable_clause",
                        lambda: strict_actionable_clause(TODAY))
    result = FilterService(db).query(OpportunityFilters())
    assert [row.unique_id for row in result.items] == ["future"]


def test_admin_include_undated_uses_broader_rule(db, monkeypatch):
    add(db, "rolling", None, DeadlineState.ROLLING.value)
    monkeypatch.setattr("app.services.filter_service.actionable_clause",
                        lambda: actionable_clause(TODAY))
    result = FilterService(db).query(OpportunityFilters(include_undated=True))
    assert [row.unique_id for row in result.items] == ["rolling"]

