"""Zero must never be reported as an unexplained success.

The 2026-08-29 baseline: 916 runs, 0 ever marked failed, and 16 sources that
fetched nothing across 127 attempts — every one stored as "completed". These
tests pin the distinctions that were impossible to make then, and, more
importantly, pin the classifier's refusal to guess when it does not know.
"""
from __future__ import annotations

import pytest

from app.services.scrape_outcome import (
    ErrorCode,
    Evidence,
    Outcome,
    classify,
    reconcile_stale,
)


# ------------------------------------------------------- the four core outcomes

def test_no_page_fetched_is_no_fetch_not_success():
    """Devex's real shape: 11 runs, 0 found, 0 saved, all 'completed'."""
    outcome, code, msg = classify(Evidence(pages_fetched=0, attempts=3,
                                           last_http_status=403,
                                           final_url="https://www.devex.com/funding"))
    assert outcome is Outcome.BLOCKED
    assert code is ErrorCode.HTTP_403
    assert "403" in msg
    assert not outcome.is_healthy


def test_page_fetched_but_nothing_parsed_is_parse_zero():
    outcome, _, msg = classify(Evidence(pages_fetched=1, extracted=0,
                                        last_http_status=200,
                                        response_bytes=48_210))
    assert outcome is Outcome.PARSE_ZERO
    assert "NOT evidence the source is empty" in msg


def test_parse_zero_is_never_upgraded_to_confirmed_empty_by_silence():
    """The single most important rule in the module.

    A parser that returns nothing looks exactly like a source with nothing.
    Treating them the same is how a broken scraper stops being investigated.
    """
    ev = Evidence(pages_fetched=4, extracted=0, last_http_status=200,
                  response_bytes=120_000, body_sample="lots of unrelated page text")
    outcome, _, _ = classify(ev)
    assert outcome is Outcome.PARSE_ZERO, (
        "absence of results is not evidence of absence of opportunities"
    )


@pytest.mark.parametrize("proof_field, value", [
    ("empty_proof", "API reported total=0 with statuses=[open]"),
    ("all_notices_closed", True),
])
def test_confirmed_empty_requires_positive_proof(proof_field, value):
    ev = Evidence(pages_fetched=1, extracted=0, last_http_status=200)
    setattr(ev, proof_field, value)
    outcome, _, _ = classify(ev)
    assert outcome is Outcome.CONFIRMED_EMPTY


def test_confirmed_empty_from_the_page_saying_so():
    ev = Evidence(pages_fetched=1, extracted=0, last_http_status=200,
                  body_sample="There are currently no open opportunities.")
    assert classify(ev)[0] is Outcome.CONFIRMED_EMPTY


def test_empty_phrase_matching_is_narrow():
    """A loose pattern here silently converts broken parsers into empty sources."""
    for text in ("no cookies were found on this device",
                 "our grants programme has no application fee",
                 "there is no charge for applying"):
        ev = Evidence(pages_fetched=1, extracted=0, body_sample=text)
        assert classify(ev)[0] is Outcome.PARSE_ZERO, f"over-matched on: {text!r}"


def test_structure_change_needs_a_signature_difference():
    same = Evidence(pages_fetched=1, extracted=0,
                    structure_signature="abc123", last_good_signature="abc123")
    assert classify(same)[0] is Outcome.PARSE_ZERO

    drifted = Evidence(pages_fetched=1, extracted=0,
                       structure_signature="def456", last_good_signature="abc123")
    outcome, _, msg = classify(drifted)
    assert outcome is Outcome.STRUCTURE_CHANGED
    assert "abc123" in msg and "def456" in msg


def test_missing_expected_container_is_also_drift():
    ev = Evidence(pages_fetched=1, extracted=0, expected_container_present=False)
    assert classify(ev)[0] is Outcome.STRUCTURE_CHANGED


def test_no_signature_history_cannot_claim_drift():
    """A first run has nothing to compare against and must not invent a verdict."""
    ev = Evidence(pages_fetched=1, extracted=0, structure_signature="abc123")
    assert classify(ev)[0] is Outcome.PARSE_ZERO


# --------------------------------------------------- success vs "nothing new"

def test_rows_saved_is_success_with_results():
    assert classify(Evidence(pages_fetched=3, extracted=40, saved=12))[0] \
        is Outcome.SUCCESS_WITH_RESULTS


def test_all_duplicates_is_success_no_new_not_failure():
    """Most 'stale' sources in the matrix are this: working, and repeating.
    Reporting them as failures would bury the 16 that really are dead."""
    outcome, _, msg = classify(Evidence(pages_fetched=3, extracted=40,
                                        saved=0, duplicates=40))
    assert outcome is Outcome.SUCCESS_NO_NEW
    assert outcome.is_healthy
    assert "already stored" in msg


# ------------------------------------------------------------ how a run ended

def test_cancellation_outranks_everything():
    ev = Evidence(pages_fetched=0, cancelled=True, last_http_status=403)
    assert classify(ev)[0] is Outcome.CANCELLED


def test_timeout_outranks_content_questions():
    assert classify(Evidence(pages_fetched=2, extracted=0, timed_out=True))[0] \
        is Outcome.TIMED_OUT


def test_exception_is_crashed_and_keeps_the_message():
    outcome, _, msg = classify(Evidence(pages_fetched=1, exception="KeyError: 'items'"))
    assert outcome is Outcome.CRASHED
    assert "KeyError" in msg


def test_challenge_page_is_blocked_not_no_fetch():
    ev = Evidence(pages_fetched=0, page_title="Just a moment...",
                  body_sample="enable javascript and cookies to continue")
    outcome, code, _ = classify(ev)
    assert outcome is Outcome.BLOCKED
    assert code is ErrorCode.CHALLENGE


def test_session_expiry_is_distinct_from_auth_required():
    assert classify(Evidence(pages_fetched=0, session_expired=True))[0] \
        is Outcome.SESSION_EXPIRED
    assert classify(Evidence(pages_fetched=0, auth_required=True))[0] \
        is Outcome.AUTH_REQUIRED


# ------------------------------------------------------------ health semantics

def test_confirmed_empty_is_healthy_and_needs_no_action():
    """An empty source is working. It must stay enabled and keep being checked,
    or the day it publishes something nobody collects it."""
    assert Outcome.CONFIRMED_EMPTY.is_healthy
    assert not Outcome.CONFIRMED_EMPTY.is_actionable


def test_every_outcome_has_a_next_action():
    for o in Outcome:
        assert o.next_action, f"{o} has no recommended action"


# -------------------------------------------------- reconciling the 106 stuck

def test_stuck_run_with_finish_time_is_a_crash():
    """30 of the 106. _close_run stamped finished_at then copied prog['status'],
    which is only set after the crawl loop — so these raised inside it."""
    outcome, why = reconcile_stale("running", has_finished_at=True)
    assert outcome is Outcome.CRASHED
    assert "inside the crawl loop" in why


def test_stuck_run_without_finish_time_is_abandoned():
    """The other 76. _close_run never ran at all."""
    outcome, why = reconcile_stale("running", has_finished_at=False)
    assert outcome is Outcome.STALE_RUN_RECOVERED
    assert "disappeared" in why


def test_the_two_stuck_populations_get_different_states():
    """Marking all 106 the same would be the guess this module exists to stop."""
    assert reconcile_stale("running", True)[0] is not reconcile_stale("running", False)[0]
