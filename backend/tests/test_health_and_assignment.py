"""Scraper health from evidence, and hand-labelling that sticks.

Both close gaps the brief named. Health answers "which sources are broken and
for how long" from the columns runs now record, instead of from a status word
that said "completed" for all 127 attempts by the 16 sources that never fetched
a page. Assignment produces the human labels the backfill guard already
protects — a protection that was inert because nothing could create one.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest

from app.services.scrape_outcome import Outcome
from app.services.scraper_health import UNHEALTHY, _streak, summary


# ------------------------------------------------------ what counts as broken

def test_a_run_that_proved_the_source_is_empty_is_not_a_failure():
    """CONFIRMED_EMPTY means the source demonstrated it has nothing to list.
    Calling that broken trains people to ignore the alert."""
    assert Outcome.CONFIRMED_EMPTY not in UNHEALTHY


def test_a_cancelled_run_is_not_a_failure():
    """Somebody pressed stop. Counting it would make every manual stop look
    like a failing source the next morning."""
    assert Outcome.CANCELLED not in UNHEALTHY


@pytest.mark.parametrize("outcome", [
    Outcome.NO_FETCH, Outcome.PARSE_ZERO, Outcome.STRUCTURE_CHANGED,
    Outcome.BLOCKED, Outcome.AUTH_REQUIRED, Outcome.TIMED_OUT, Outcome.CRASHED,
])
def test_outcomes_that_mean_the_run_did_not_do_its_job(outcome):
    assert outcome in UNHEALTHY


def test_success_is_never_unhealthy():
    assert Outcome.SUCCESS_WITH_RESULTS not in UNHEALTHY
    assert Outcome.SUCCESS_NO_NEW not in UNHEALTHY


# ------------------------------------------------------------- the streak

def test_a_streak_counts_only_consecutive_failures():
    assert _streak([Outcome.NO_FETCH.value, Outcome.NO_FETCH.value,
                    Outcome.SUCCESS_WITH_RESULTS.value,
                    Outcome.NO_FETCH.value]) == 2


def test_one_good_run_ends_the_streak():
    assert _streak([Outcome.SUCCESS_NO_NEW.value, Outcome.NO_FETCH.value]) == 0


def test_a_run_from_before_outcomes_existed_stops_the_count():
    """Counting a blank as healthy would silently reset a real streak;
    counting it as unhealthy would invent failures nobody observed."""
    assert _streak([Outcome.NO_FETCH.value, "", Outcome.NO_FETCH.value]) == 1


def test_an_unrecognised_outcome_value_does_not_crash_the_page():
    assert _streak(["something-nobody-defined"]) == 0


def test_summary_counts_what_needs_attention():
    class E:
        def __init__(self, state):
            self.state = state
    got = summary([E("ok"), E("failing"), E("stale"), E("never_produced"), E("ok")])
    assert got["total"] == 5
    assert got["needs_attention"] == 3


# --------------------------------------------------------- bulk assignment

@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "assign.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    with db_mod.session_scope() as s:
        yield s


def _add(session, n=1, **kw):
    from app.database.models import Opportunity, Status

    ids = []
    for i in range(n):
        defaults = dict(
            unique_id=f"a{i}-{kw.get('source_website','')}-{kw.get('unique_id','')}",
            title="Procurement of Office Chairs",
            organization="F", source_website="Some Funder",
            summary="Supply of furniture.", status=Status.ACTIVE,
            verticals="", date_scraped=datetime(2026, 8, 1),
            # Actionable, deliberately. `unclassified_clause` is scoped to
            # rows someone could still bid on: a row whose deadline nobody has
            # established belongs in the DEADLINE review queue first, and
            # offering it for vertical labelling as well would ask two
            # questions about a row that may turn out to be closed.
            deadline=date(2027, 1, 1), deadline_state="dated",
        )
        defaults.update(kw)
        row = Opportunity(**defaults)
        session.add(row)
        session.flush()
        ids.append(row.id)
    return ids


def test_a_row_the_classifier_could_not_place_is_offered_for_labelling(db):
    from app.services import vertical_assignment as va

    _add(db, 3)
    assert va.count_unclassified(db) == 3
    assert len(va.fetch_unclassified(db)) == 3


def test_assigning_a_vertical_marks_it_as_a_human_decision(db):
    from app.services import vertical_assignment as va
    from app.services.verticals import HUMAN

    ids = _add(db, 2)
    va.assign(db, ids, ["Health"], reviewer="r@example.org")
    db.flush()
    from sqlalchemy import select

    from app.database.models import Opportunity
    rows = db.execute(select(Opportunity).where(Opportunity.id.in_(ids))).scalars().all()
    for r in rows:
        assert r.verticals == "Health"
        assert r.verticals_source == HUMAN
        assert r.verticals_labeled_by == "r@example.org"
        assert r.verticals_labeled_at is not None


def test_clearing_every_vertical_is_recorded_as_a_decision_not_as_blank(db):
    """"None of our six" is a judgement. Stored as an ordinary empty value the
    next backfill would re-tag it, undoing the reviewer's work — which is the
    exact failure the human-label guard exists to stop."""
    from app.services import vertical_assignment as va
    from app.services.verticals import HUMAN, backfill_verticals

    ids = _add(db, 1, title="Health Systems Strengthening Consultancy")
    va.assign(db, ids, [], reviewer="r@example.org")
    db.flush()
    db.commit()

    backfill_verticals()

    from sqlalchemy import select

    from app.database.models import Opportunity
    row = db.execute(select(Opportunity).where(Opportunity.id == ids[0])).scalar_one()
    db.refresh(row)
    assert (row.verticals or "") == ""
    assert row.verticals_source == HUMAN


def test_a_labelled_row_leaves_the_unclassified_queue(db):
    from app.services import vertical_assignment as va

    ids = _add(db, 2)
    va.assign(db, ids[:1], ["Health"])
    db.flush()
    assert va.count_unclassified(db) == 1


def test_a_deliberately_empty_label_also_leaves_the_queue(db):
    """Re-offering it would make the queue refill with work already done."""
    from app.services import vertical_assignment as va

    ids = _add(db, 1)
    va.assign(db, ids, [])
    db.flush()
    assert va.count_unclassified(db) == 0


def test_an_unknown_vertical_is_refused_rather_than_dropped(db):
    """A typo stored here would sit in the database forever, matching no
    filter, looking exactly like a correctly labelled row."""
    from app.services import vertical_assignment as va

    ids = _add(db, 1)
    with pytest.raises(va.AssignmentError) as exc:
        va.assign(db, ids, ["Helth"])
    assert "not one of the verticals" in str(exc.value)


def test_a_legacy_vertical_name_is_accepted_and_normalised(db):
    from app.services import vertical_assignment as va

    ids = _add(db, 1)
    got = va.assign(db, ids, ["Climate/Sustainability"])
    assert got["verticals"] == ["Climate/Sustainability(ESG)"]


def test_duplicates_collapse(db):
    from app.services import vertical_assignment as va

    ids = _add(db, 1)
    got = va.assign(db, ids, ["Health", "Health"])
    assert got["verticals"] == ["Health"]


def test_a_batch_larger_than_the_cap_is_refused(db):
    """Not a technical limit — a review limit. A bulk assign that silently
    accepted 10,000 ids would let one mis-click relabel a third of the
    database with no way to tell which rows were touched."""
    from app.services import vertical_assignment as va

    with pytest.raises(va.AssignmentError):
        va.assign(db, list(range(va.MAX_BULK + 1)), ["Health"])


def test_assigning_to_a_row_that_no_longer_exists_says_so(db):
    from app.services import vertical_assignment as va

    with pytest.raises(va.AssignmentError) as exc:
        va.assign(db, [999_999], ["Health"])
    assert "no longer exist" in str(exc.value)


def test_an_empty_selection_is_refused(db):
    from app.services import vertical_assignment as va

    with pytest.raises(va.AssignmentError):
        va.assign(db, [], ["Health"])


def test_a_mislabelled_batch_can_be_handed_back_to_the_classifier(db):
    """Without this a mis-click is permanent: the backfill skips human rows,
    so nothing would ever re-derive them."""
    from app.services import vertical_assignment as va

    ids = _add(db, 2)
    va.assign(db, ids, ["Health"])
    db.flush()
    va.revert_to_auto(db, ids)
    db.flush()

    from sqlalchemy import select

    from app.database.models import Opportunity
    for r in db.execute(select(Opportunity).where(Opportunity.id.in_(ids))).scalars():
        assert r.verticals_source is None


def test_the_backlog_is_broken_down_by_source(db):
    """A backlog concentrated in one source is a keyword gap for that source's
    vocabulary, fixed once in the rules rather than a thousand times by hand."""
    from app.services import vertical_assignment as va

    _add(db, 3, source_website="Noisy")
    _add(db, 1, source_website="Quiet")
    got = va.by_source(db)
    assert got[0]["source_website"] == "Noisy" and got[0]["count"] == 3


def test_newest_first_because_an_unclassified_row_is_a_routing_gap(db):
    """The opposite of the deadline queue, on purpose: labelling the newest
    puts live opportunities in front of the right team this week."""
    from app.services import vertical_assignment as va

    _add(db, 1, title="Older", unique_id="old", date_scraped=datetime(2026, 1, 1))
    _add(db, 1, title="Newer", unique_id="new", date_scraped=datetime(2026, 8, 1))
    assert [u.title for u in va.fetch_unclassified(db)] == ["Newer", "Older"]


# ------------------------------------------------------------- timeouts
#
# The baseline found 106 runs stuck in "running". A source that hangs holds
# its concurrency slot forever, so an entire night's scrape stops after the
# first few sources wedge.

def test_a_hanging_source_does_not_hold_its_slot_forever():
    import asyncio

    async def scenario():
        async def hangs():
            await asyncio.sleep(3600)

        try:
            await asyncio.wait_for(hangs(), 0.05)
        except asyncio.TimeoutError:
            return "bounded"
        return "never returned"

    assert asyncio.run(scenario()) == "bounded"


def test_the_whole_run_is_bounded_even_if_every_source_is_slow():
    """Per-source limits alone do not bound the run: 85 sources times the
    per-source limit is days, and the scheduler skips the next run while this
    one is still going."""
    import asyncio

    async def scenario():
        async def slow():
            await asyncio.sleep(3600)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(slow() for _ in range(5))), 0.05)
        except asyncio.TimeoutError:
            return "bounded"
        return "never returned"

    assert asyncio.run(scenario()) == "bounded"


def test_the_timeouts_are_configured_above_the_largest_legitimate_source():
    """DevelopmentAid's own per-section cap is 30 minutes and it has several
    sections. A per-source limit tuned to the average would kill it every
    night and look like a broken scraper."""
    from app.core.config import settings

    assert settings.source_timeout_s > settings.devaid_max_duration_s
    assert settings.run_timeout_s > settings.source_timeout_s


def test_a_timed_out_run_is_not_recorded_as_completed():
    """The whole point. 'Completed' for a run that was killed is the same lie
    the outcome taxonomy exists to stop."""
    import inspect

    from app.services.scraper_manager import ScraperManager

    src = inspect.getsource(ScraperManager._run)
    assert "asyncio.TimeoutError" in src
    assert '"failed"' in src or "'failed'" in src
