"""The queue exists so unassessed rows are held, not lost.

These run against a real SQLite database rather than fakes, because the thing
being asserted is that a row MOVES between views — out of the queue and into
the live list or the archive. A mock cannot be wrong about that in the way a
query can.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

TODAY = date(2026, 8, 29)
FUTURE = TODAY + timedelta(days=30)
PAST = TODAY - timedelta(days=30)


@pytest.fixture()
def db(monkeypatch):
    """A throwaway database with the real schema."""
    path = os.path.join(tempfile.mkdtemp(), "review.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")

    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)

    db_mod.init_db()
    with db_mod.session_scope() as session:
        yield session


def _add(session, **kw):
    from app.database.models import Opportunity, Status

    defaults = dict(
        unique_id=f"uid-{kw.get('title', 'x')}",
        title="A Call For Something",
        organization="Some Funder",
        source_website="Some Funder",
        opportunity_url="https://example.org/call",
        summary="",
        status=Status.ACTIVE,
        deadline=None,
        deadline_state="unknown",
        deadline_raw="see website",
        deadline_confidence="unparseable",
        date_scraped=datetime(2026, 6, 1, tzinfo=timezone.utc).replace(tzinfo=None),
    )
    defaults.update(kw)
    row = Opportunity(**defaults)
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------- the listing

def test_an_unassessed_row_appears_in_the_queue(db):
    from app.services import review_queue

    _add(db, unique_id="u1", title="Unreadable Date Call")
    assert review_queue.queue_size(db) == 1
    assert review_queue.fetch(db)[0].title == "Unreadable Date Call"


def test_a_dated_row_is_not_in_the_queue(db):
    from app.services import review_queue

    _add(db, unique_id="u2", deadline=FUTURE, deadline_state="dated",
         deadline_confidence="parsed")
    assert review_queue.queue_size(db) == 0


def test_a_rolling_row_is_not_in_the_queue(db):
    """The source said it is open. Nobody needs to decide that again."""
    from app.services import review_queue

    _add(db, unique_id="u3", deadline_state="rolling",
         deadline_confidence="source_rolling")
    assert review_queue.queue_size(db) == 0


def test_an_expired_row_is_not_in_the_queue(db):
    """Already archived. The queue is for rows awaiting a decision, not for
    every row that lacks a date."""
    from app.database.models import Status
    from app.services import review_queue

    _add(db, unique_id="u4", status=Status.EXPIRED)
    assert review_queue.queue_size(db) == 0


def test_the_queue_carries_the_source_s_own_words(db):
    """The single most useful field on the screen: usually the date is right
    there in a format the parser did not recognise."""
    from app.services import review_queue

    _add(db, unique_id="u5", deadline_raw="Closes end of Q3 FY26")
    assert review_queue.fetch(db)[0].deadline_raw == "Closes end of Q3 FY26"


def test_the_oldest_rows_are_reviewed_first(db):
    """An unassessed row ages into irrelevance. Newest-first would leave the
    stalest rows permanently at the bottom."""
    from app.services import review_queue

    _add(db, unique_id="new", title="Newer",
         date_scraped=datetime(2026, 8, 1))
    _add(db, unique_id="old", title="Older",
         date_scraped=datetime(2026, 2, 1))
    assert [e.title for e in review_queue.fetch(db)] == ["Older", "Newer"]


def test_the_backlog_is_broken_down_by_source(db):
    """900 rows from one source is a parser bug for that source, not a review
    job. The distribution is what tells them apart."""
    from app.services import review_queue

    for i in range(3):
        _add(db, unique_id=f"a{i}", source_website="Noisy Source")
    _add(db, unique_id="b0", source_website="Quiet Source")
    got = review_queue.by_source(db)
    assert got[0] == {"source_website": "Noisy Source", "count": 3}
    assert {"source_website": "Quiet Source", "count": 1} in got


def test_the_queue_can_be_filtered_to_one_source(db):
    from app.services import review_queue

    _add(db, unique_id="c0", source_website="Source A")
    _add(db, unique_id="c1", source_website="Source B")
    got = review_queue.fetch(db, source_website="Source B")
    assert len(got) == 1 and got[0].source_website == "Source B"


# ------------------------------------------------------------- the decisions

def test_setting_a_future_date_makes_the_row_live_and_clears_the_queue(db):
    from app.services import review_queue
    from app.services.actionable import is_actionable

    row = _add(db, unique_id="d1")
    review_queue.set_deadline(db, row.id, FUTURE, reviewer="r@example.org",
                              today=TODAY)
    db.flush()
    db.refresh(row)
    assert row.deadline == FUTURE
    assert row.deadline_state == "dated"
    assert is_actionable(row.status, row.deadline, row.deadline_state, today=TODAY)
    assert review_queue.queue_size(db) == 0


def test_setting_a_past_date_is_accepted_and_closes_the_row(db):
    """A reviewer reading 'applications closed 12 June' is telling us something
    true. Refusing it would leave the row in the queue forever with no way to
    record what they just learned."""
    from app.database.models import Status
    from app.services import review_queue

    row = _add(db, unique_id="d2")
    review_queue.set_deadline(db, row.id, PAST, today=TODAY)
    db.flush()
    db.refresh(row)
    assert row.status == Status.EXPIRED
    assert row.deadline == PAST
    assert review_queue.queue_size(db) == 0


def test_marking_rolling_makes_the_row_live(db):
    from app.services import review_queue
    from app.services.actionable import is_actionable

    row = _add(db, unique_id="d3")
    review_queue.mark_rolling(db, row.id)
    db.flush()
    db.refresh(row)
    assert row.deadline_state == "rolling"
    assert is_actionable(row.status, row.deadline, row.deadline_state, today=TODAY)


def test_marking_rolling_clears_any_stored_date(db):
    """Leaving a date behind would expire the row on a date the reviewer just
    said does not apply — and a stored past date closes a rolling row, by
    design, so rolling cannot mean immortal."""
    from app.services import review_queue

    row = _add(db, unique_id="d4", deadline=PAST)
    review_queue.mark_rolling(db, row.id)
    db.flush()
    db.refresh(row)
    assert row.deadline is None


def test_marking_closed_archives_the_row_and_never_deletes_it(db):
    """The brief: expired or invalid rows are archived or quarantined, not
    deleted, unless deletion is separately approved."""
    from sqlalchemy import select

    from app.database.models import Opportunity, Status
    from app.services import review_queue

    row = _add(db, unique_id="d5")
    review_queue.mark_closed(db, row.id)
    db.flush()
    still_there = db.execute(
        select(Opportunity).where(Opportunity.id == row.id)
    ).scalar_one_or_none()
    assert still_there is not None, "the row was deleted"
    assert still_there.status == Status.EXPIRED
    assert review_queue.queue_size(db) == 0


def test_a_closed_row_keeps_its_unknown_deadline_state(db):
    """Which stays true — nobody ever established a date. EXPIRED status is
    what moves it out of the queue; inventing a date would be a lie."""
    from app.services import review_queue

    row = _add(db, unique_id="d6")
    review_queue.mark_closed(db, row.id)
    db.flush()
    db.refresh(row)
    assert row.deadline_state == "unknown"


def test_deciding_a_row_that_does_not_exist_says_so(db):
    from app.services import review_queue

    with pytest.raises(review_queue.ReviewError):
        review_queue.mark_rolling(db, 999_999)


# -------------------------------------- a decision survives the next scrape

@pytest.mark.parametrize("decide", ["set_deadline", "mark_rolling", "mark_closed"])
def test_every_decision_is_marked_as_human_made(db, decide):
    from app.services import review_queue

    row = _add(db, unique_id=f"h-{decide}")
    if decide == "set_deadline":
        review_queue.set_deadline(db, row.id, FUTURE, today=TODAY)
    else:
        getattr(review_queue, decide)(db, row.id)
    db.flush()
    db.refresh(row)
    assert row.deadline_confidence == review_queue.CONFIDENCE_HUMAN
    assert review_queue.is_human_decided(row)


def test_a_machine_assigned_row_is_not_protected(db):
    """The guard has to distinguish a person's ruling from the parser's, or it
    protects everything and means nothing."""
    from app.services import review_queue

    row = _add(db, unique_id="m1", deadline_confidence="unparseable")
    assert not review_queue.is_human_decided(row)


def test_a_decision_records_when_it_was_made(db):
    from app.services import review_queue

    row = _add(db, unique_id="t1", deadline_checked_at=None)
    review_queue.mark_rolling(db, row.id)
    db.flush()
    db.refresh(row)
    assert row.deadline_checked_at is not None


# ---------------------------------------------- the views stay disjoint

def test_a_queued_row_is_in_neither_the_live_view_nor_the_archive(db):
    """The reason this module exists. Before it, such a row was in no view at
    all — which is indistinguishable from having been lost."""
    from sqlalchemy import func, select

    from app.database.models import Opportunity
    from app.services.actionable import actionable_clause, expired_clause

    _add(db, unique_id="v1")

    def count(clause):
        return db.execute(
            select(func.count()).select_from(Opportunity).where(clause)
        ).scalar_one()

    assert count(actionable_clause(TODAY)) == 0
    assert count(expired_clause(TODAY)) == 0


def test_after_a_decision_the_row_is_in_exactly_one_view(db):
    from sqlalchemy import func, select

    from app.database.models import Opportunity
    from app.services import review_queue
    from app.services.actionable import actionable_clause, expired_clause

    row = _add(db, unique_id="v2")
    review_queue.set_deadline(db, row.id, FUTURE, today=TODAY)
    db.flush()

    def count(clause):
        return db.execute(
            select(func.count()).select_from(Opportunity).where(clause)
        ).scalar_one()

    assert count(actionable_clause(TODAY)) == 1
    assert count(expired_clause(TODAY)) == 0
    assert review_queue.queue_size(db) == 0
