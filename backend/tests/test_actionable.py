"""No live view may show a row whose deadline has passed.

The acceptance criterion from the brief, stated as tests: across every live
endpoint, approved working view, export and email query, the count of visible
rows with `deadline < today` must be exactly zero.

The baseline had 1,481 such rows, visible in the Approved view because that
branch deliberately skipped the deadline predicate while the main table applied
it — so a row's visibility depended on which query you happened to hit.

The Python and SQL halves of the rule are run over the same fixtures in
`test_python_and_sql_agree`, because two implementations of one rule that drift
apart are worse than one that is merely wrong.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status
from app.services.actionable import (
    CONFIDENCE_LEGACY,
    DeadlineState,
    actionable_clause,
    classify_deadline,
    expired_clause,
    is_actionable,
    unassessed_clause,
)

TODAY = date(2026, 8, 29)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add(db, **kw) -> Opportunity:
    row = Opportunity(
        unique_id=kw.pop("uid", str(id(kw))),
        title=kw.pop("title", "A call"),
        source_website="Test",
        status=kw.pop("status", Status.ACTIVE),
        **kw,
    )
    db.add(row)
    db.flush()
    return row


# ------------------------------------------------- the three deadline states

def test_dated_and_open_is_actionable():
    assert is_actionable(Status.ACTIVE, TOMORROW, "dated", today=TODAY)


def test_dated_and_passed_is_not():
    assert not is_actionable(Status.ACTIVE, YESTERDAY, "dated", today=TODAY)


def test_closing_today_is_still_actionable():
    """Off-by-one on the boundary would close a call a day early — on the day
    someone is most likely to be submitting."""
    assert is_actionable(Status.ACTIVE, TODAY, "dated", today=TODAY)


def test_rolling_is_actionable_without_a_date():
    assert is_actionable(Status.ACTIVE, None, "rolling", today=TODAY)


def test_unknown_is_not_actionable():
    """The distinction the whole module exists for.

    'We could not read a date' is not evidence a call is open, and treating it
    as such is what puts long-closed listings in front of someone about to
    spend a week on a proposal.
    """
    assert not is_actionable(Status.ACTIVE, None, "unknown", today=TODAY)


def test_expired_status_is_never_actionable():
    assert not is_actionable(Status.EXPIRED, TOMORROW, "dated", today=TODAY)


def test_rows_predating_the_column_fall_back_to_the_old_behaviour():
    """A partially migrated database must degrade, not hide everything."""
    assert is_actionable(Status.ACTIVE, TOMORROW, None, today=TODAY)
    assert not is_actionable(Status.ACTIVE, YESTERDAY, None, today=TODAY)


# --------------------------------------------- the acceptance test, in SQL

def test_no_live_query_returns_a_passed_deadline(db):
    for i, (dl, state) in enumerate([
        (YESTERDAY, "dated"), (YESTERDAY, "rolling"), (YESTERDAY, None),
        (TODAY, "dated"), (TOMORROW, "dated"),
        (None, "rolling"), (None, "unknown"), (None, None),
    ]):
        add(db, uid=f"u{i}", deadline=dl, deadline_state=state)
    db.flush()

    rows = db.execute(select(Opportunity).where(actionable_clause(TODAY))).scalars().all()
    passed = [r for r in rows if r.deadline is not None and r.deadline < TODAY]
    assert passed == [], (
        f"{len(passed)} row(s) with a passed deadline reached a live query"
    )


def test_a_rolling_state_cannot_resurrect_a_passed_date(db):
    """If a source says 'rolling' but also gives a date that has gone, the date
    wins. Otherwise 'rolling' becomes a way to keep dead rows alive forever."""
    add(db, uid="r1", deadline=YESTERDAY, deadline_state="rolling")
    db.flush()
    rows = db.execute(select(Opportunity).where(actionable_clause(TODAY))).scalars().all()
    assert [r for r in rows if r.deadline == YESTERDAY] == []


def test_approved_view_no_longer_bypasses_the_predicate(db):
    """Where the 1,481 rows were visible."""
    add(db, uid="a1", deadline=YESTERDAY, deadline_state="dated", approved=True)
    add(db, uid="a2", deadline=TOMORROW, deadline_state="dated", approved=True)
    db.flush()
    stmt = select(Opportunity).where(Opportunity.approved.is_(True), actionable_clause(TODAY))
    got = db.execute(stmt).scalars().all()
    assert [r.unique_id for r in got] == ["a2"]


def test_expired_rows_are_archived_not_deleted(db):
    add(db, uid="e1", deadline=YESTERDAY, deadline_state="dated")
    db.flush()
    assert db.execute(select(Opportunity).where(expired_clause(TODAY))).scalars().all()
    assert db.execute(select(Opportunity)).scalars().all(), "nothing may be deleted"


def test_unassessed_rows_are_neither_live_nor_archived(db):
    """An UNKNOWN row needs a human. Sweeping it into the archive buries it."""
    add(db, uid="x1", deadline=None, deadline_state="unknown")
    db.flush()
    live = db.execute(select(Opportunity).where(actionable_clause(TODAY))).scalars().all()
    archived = db.execute(select(Opportunity).where(expired_clause(TODAY))).scalars().all()
    unassessed = db.execute(select(Opportunity).where(unassessed_clause())).scalars().all()
    assert live == [] and archived == []
    assert len(unassessed) == 1


def test_python_and_sql_agree(db):
    """The two halves of the rule, over identical fixtures."""
    cases = [
        (Status.ACTIVE, YESTERDAY, "dated"), (Status.ACTIVE, TODAY, "dated"),
        (Status.ACTIVE, TOMORROW, "dated"), (Status.ACTIVE, None, "rolling"),
        (Status.ACTIVE, None, "unknown"), (Status.ACTIVE, None, None),
        (Status.ACTIVE, TOMORROW, None), (Status.ACTIVE, YESTERDAY, None),
        (Status.EXPIRED, TOMORROW, "dated"), (Status.EXPIRED, None, "rolling"),
    ]
    for i, (st, dl, state) in enumerate(cases):
        add(db, uid=f"p{i}", status=st, deadline=dl, deadline_state=state)
    db.flush()

    sql_ids = {r.unique_id for r in
               db.execute(select(Opportunity).where(actionable_clause(TODAY))).scalars()}
    py_ids = {f"p{i}" for i, (st, dl, state) in enumerate(cases)
              if is_actionable(st, dl, state, today=TODAY)}
    assert sql_ids == py_ids, (
        f"the Python and SQL rules disagree: only-SQL={sql_ids - py_ids}, "
        f"only-Python={py_ids - sql_ids}"
    )


# ------------------------------------------------------ classifying new rows

def test_a_parsed_date_beats_a_rolling_marker():
    """'Rolling basis — next review 30 September' has a date someone can act on."""
    state, conf = classify_deadline("rolling basis, next review 30 September 2026",
                                    date(2026, 9, 30), source_says_rolling=True)
    assert state is DeadlineState.DATED and conf == "parsed"


def test_source_saying_rolling_with_no_date_is_rolling():
    state, conf = classify_deadline("Applications accepted on a rolling basis",
                                    None, source_says_rolling=True)
    assert state is DeadlineState.ROLLING and conf == "source_rolling"


def test_text_we_could_not_parse_is_unknown_not_rolling():
    """A parser gap must stay findable instead of being filed as 'no deadline'."""
    state, conf = classify_deadline("Closes end of the current quarter", None,
                                    source_says_rolling=False)
    assert state is DeadlineState.UNKNOWN and conf == "unparseable"


def test_no_deadline_text_at_all_is_also_unknown():
    state, _ = classify_deadline("", None, source_says_rolling=False)
    assert state is DeadlineState.UNKNOWN


def test_legacy_backfill_is_marked_as_an_assumption():
    """The migration cannot recover whether a legacy NULL row was rolling or
    unparseable, so it must not pretend it observed one."""
    assert CONFIDENCE_LEGACY == "legacy_assumed"
    assert CONFIDENCE_LEGACY != "source_rolling"
