"""What the ingest path now writes, and what it now refuses.

Two changes are covered here, and they fail in opposite directions:

  * the scope check can DELETE records that should have been kept, if an
    unconfigured source is treated as having said something;
  * the deadline state can HIDE records that should have been visible, if a
    row is stored in a state the actionable rule reads as unassessed.

So the tests that matter most are the ones asserting each change is inert
where it has no evidence to act on.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.actionable import (
    CONFIDENCE_PARSED,
    CONFIDENCE_SOURCE_ROLLING,
    CONFIDENCE_UNPARSEABLE,
    DeadlineState,
    classify_deadline,
    is_actionable,
)
from app.services.source_manifest import contract_for, record_is_in_scope

TODAY = date(2026, 8, 29)
FUTURE = TODAY + timedelta(days=30)
PAST = TODAY - timedelta(days=30)


# ------------------------------------------------- the scope check is inert
# ------------------------------------------------- where it knows nothing

def test_a_source_that_supplies_no_fields_loses_nothing():
    """Almost every one of the 85 sources supplies neither field. If silence
    read as a reason to discard, a single deploy would empty the platform."""
    c = contract_for("some_unmanifested_source", "Some Funder")
    keep, why = record_is_in_scope(c, record_type="", source_status="")
    assert keep and why == ""


def test_a_manifested_source_that_supplies_no_fields_also_loses_nothing():
    """Having a contract is not the same as the scraper populating it."""
    keep, _ = record_is_in_scope(contract_for("worldbank"), "", "")
    assert keep


@pytest.mark.parametrize("key", ["worldbank", "unpp", "adb", "devnet", "ngobox"])
def test_no_manifested_source_rejects_an_empty_record(key):
    keep, _ = record_is_in_scope(contract_for(key), "", "")
    assert keep, f"{key} would discard rows from its own scraper today"


def test_an_unrecognised_status_word_is_not_a_reason_to_discard():
    """A source renaming 'Open' to 'Currently accepting' must degrade to
    keeping the row, not to deleting the source's entire output."""
    keep, _ = record_is_in_scope(contract_for("worldbank"),
                                 record_type="tender",
                                 source_status="Currently accepting")
    assert keep


# ------------------------------------------- and it does act on real evidence

def test_a_contract_award_is_rejected_on_the_source_s_own_type():
    keep, why = record_is_in_scope(contract_for("worldbank"),
                                   record_type="contract_award")
    assert not keep and why


def test_a_cancelled_notice_is_rejected_on_the_source_s_own_status():
    keep, why = record_is_in_scope(contract_for("worldbank"),
                                   record_type="tender",
                                   source_status="Cancelled")
    assert not keep and "closed" in why


# --------------------------------------- what gets written for each row shape

def test_a_parsed_date_is_stored_as_dated():
    state, confidence = classify_deadline("30 September 2026", FUTURE, False)
    assert state is DeadlineState.DATED
    assert confidence == CONFIDENCE_PARSED


def test_a_source_stating_rolling_is_stored_as_rolling():
    state, confidence = classify_deadline("Applications accepted on a rolling basis",
                                          None, True)
    assert state is DeadlineState.ROLLING
    assert confidence == CONFIDENCE_SOURCE_ROLLING


def test_unreadable_text_is_stored_as_unknown_not_as_rolling():
    """This is the immortality bug. 'We could not read a date' becoming
    'there is no closing date' is what put closed calls on the dashboard as
    permanent Ongoing rows."""
    state, confidence = classify_deadline("see website for details", None, False)
    assert state is DeadlineState.UNKNOWN
    assert confidence == CONFIDENCE_UNPARSEABLE


def test_a_date_beats_a_rolling_marker():
    """'Rolling basis — next review 30 September' has a date someone can act
    on, and the date is the more useful of the two."""
    state, _ = classify_deadline("rolling basis, next review 30 Sept", FUTURE, True)
    assert state is DeadlineState.DATED


# ------------------------- the regression this closes: an undated ROLLING row
# ------------------------- written with a NULL state disappears from the UI

def test_a_new_rolling_row_is_visible_once_its_state_is_written():
    state, _ = classify_deadline("rolling basis", None, True)
    assert is_actionable("Active", None, state.value, today=TODAY), (
        "a row the source says is open is not being shown as open"
    )


def test_the_same_row_written_with_a_null_state_would_have_vanished():
    """Phase 4 added deadline_state and backfilled it, but nothing wrote it on
    INSERT. NULL state plus NULL deadline reads as UNKNOWN — not actionable.
    Every new rolling row scraped after that migration would have been
    invisible. This asserts the failure the fix prevents."""
    assert not is_actionable("Active", None, None, today=TODAY)


def test_a_dated_row_was_never_affected_by_the_null_state_gap():
    """Why it went unnoticed: rows WITH a date infer DATED from the date, so
    the majority of rows behaved correctly the whole time."""
    assert is_actionable("Active", FUTURE, None, today=TODAY)


def test_an_unknown_row_is_held_not_shown_and_not_archived():
    """UNKNOWN is deliberately not actionable — 'we could not read a date' is
    not evidence a call is open. The run log reports the count because these
    rows are in no dashboard view until the review queue exists."""
    state, _ = classify_deadline("", None, False)
    assert state is DeadlineState.UNKNOWN
    assert not is_actionable("Active", None, state.value, today=TODAY)


def test_a_rolling_row_with_a_passed_date_is_still_closed():
    """Rolling must not become a way to live forever."""
    assert not is_actionable("Active", PAST, DeadlineState.ROLLING.value, today=TODAY)


# --------------------------------------- the schema carries the source fields

def test_raw_opportunity_carries_the_source_s_own_type_and_status():
    from app.schemas.opportunity import RawOpportunity

    raw = RawOpportunity(title="t", source_website="s",
                         record_type="contract_award", source_status="Cancelled")
    assert raw.record_type == "contract_award"
    assert raw.source_status == "Cancelled"


def test_those_fields_default_to_empty_so_every_existing_scraper_is_unchanged():
    from app.schemas.opportunity import RawOpportunity

    raw = RawOpportunity(title="t", source_website="s")
    assert raw.record_type == "" and raw.source_status == ""
    keep, _ = record_is_in_scope(contract_for("worldbank"),
                                 raw.record_type, raw.source_status)
    assert keep
