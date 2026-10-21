from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status
from app.schemas.opportunity import OpportunityFilters
from app.services.actionable import (
    DeadlineState,
    IST_OFFSET_MINUTES,
    actionable_clause,
    application_day_utc_bounds,
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


def test_application_day_uses_utc_bounds_for_the_full_india_day():
    start, end = application_day_utc_bounds(date(2026, 10, 16))
    assert start == datetime(2026, 10, 15, 18, 30)
    assert end == datetime(2026, 10, 16, 18, 30)


def test_new_today_counts_early_india_morning_stored_on_previous_utc_date(
        db, monkeypatch):
    start = datetime(2026, 10, 15, 18, 30)
    end = datetime(2026, 10, 16, 18, 30)
    monkeypatch.setattr(
        "app.services.filter_service.new_leads_window_utc",
        lambda: (start, end),
    )
    inside = add(db, "inside-india-day", date(2099, 1, 1))
    inside.date_scraped = datetime(2026, 10, 15, 19, 0)  # 00:30 IST on 16 Oct
    outside = add(db, "previous-india-day", date(2099, 1, 1))
    outside.date_scraped = datetime(2026, 10, 15, 18, 29, 59)
    db.flush()

    service = FilterService(db)
    assert service.stats(OpportunityFilters()).todays_new == 1
    result = service.query(OpportunityFilters(new_today=True))
    assert [row.unique_id for row in result.items] == ["inside-india-day"]


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



def test_new_today_starts_at_last_nights_scrape_not_midnight():
    from datetime import timezone as tz

    from app.services.actionable import new_leads_window_utc

    # 11:40 IST on 21 Oct -> window opened 22:00 IST on 20 Oct (16:30 UTC).
    start, end = new_leads_window_utc(datetime(2026, 10, 21, 6, 10, tzinfo=tz.utc))
    assert start == datetime(2026, 10, 20, 16, 30)
    assert end == datetime(2026, 10, 21, 16, 30)
    # 23:00 IST on 21 Oct -> tonight's run, opened 22:00 IST on 21 Oct.
    start, _ = new_leads_window_utc(datetime(2026, 10, 21, 17, 30, tzinfo=tz.utc))
    assert start == datetime(2026, 10, 21, 16, 30)
