"""A source's own fields decide scope — not words in a title.

The three cases the brief calls out by name are the three a title-keyword gate
gets wrong, and each is tested against the real shape of that source's data.
"""
from __future__ import annotations

import pytest

from app.services.source_manifest import (
    MANIFESTS,
    RecordType,
    ScopeStatus,
    contract_for,
    disabled_sources,
    record_is_in_scope,
    unconfirmed_sources,
)


# ------------------------------------------ World Bank: awards are the majority

def test_world_bank_rejects_a_contract_award_by_its_type():
    """The feed is mostly awards, and 'award' in a title is not the signal —
    a real notice can be titled 'Award of Contract for Supervision Services'
    while being an open invitation. The record's notice_type decides."""
    c = MANIFESTS["worldbank"]
    keep, why = record_is_in_scope(c, record_type="contract_award")
    assert not keep and "excluded" in why


def test_world_bank_keeps_an_open_procurement_notice():
    c = MANIFESTS["worldbank"]
    keep, _ = record_is_in_scope(c, record_type="tender",
                                 source_status="Request for Expressions of Interest")
    assert keep


def test_world_bank_rejects_a_cancelled_notice_by_status():
    c = MANIFESTS["worldbank"]
    keep, why = record_is_in_scope(c, record_type="tender", source_status="Cancelled")
    assert not keep and "closed" in why


# ------------------------------ UN Partner Portal: the /projects route is a red
# ------------------------------ herring, and the brief says so explicitly

def test_unpp_route_containing_projects_does_not_make_a_record_a_project():
    """`/api/projects/open/` is where CFEIs live. Filtering on URL text would
    delete an entire working source — the brief calls this out by name."""
    c = MANIFESTS["unpp"]
    assert "projects" in c.listing_url
    keep, _ = record_is_in_scope(c, record_type="eoi", source_status="Open")
    assert keep, "a CFEI must not be rejected because its route says 'projects'"


def test_unpp_still_rejects_an_actual_project_record():
    c = MANIFESTS["unpp"]
    keep, why = record_is_in_scope(c, record_type="project")
    assert not keep and "excluded" in why


def test_unpp_rejects_a_closed_cfei():
    c = MANIFESTS["unpp"]
    keep, _ = record_is_in_scope(c, record_type="eoi", source_status="Closed")
    assert not keep


# ------------------------------------------------------- status vocabulary

def test_unknown_status_is_not_treated_as_closed():
    """None means 'this source has no status vocabulary configured', which is
    not the same as 'the source says closed'. Only the second may discard."""
    c = MANIFESTS["adb"]
    assert c.status_is_open("some value nobody configured") is None
    keep, _ = record_is_in_scope(c, record_type="tender", source_status="whatever")
    assert keep, "an unrecognised status must not silently delete a record"


def test_a_source_with_no_status_vocabulary_keeps_everything_in_type():
    c = contract_for("some_foundation", "Some Foundation")
    keep, _ = record_is_in_scope(c, record_type="grant", source_status="live")
    assert keep


def test_empty_status_is_unknown_not_closed():
    assert MANIFESTS["worldbank"].status_is_open("") is None


# ------------------------------------------------------------ honesty of scope

def test_unmanifested_sources_are_needs_review_not_silently_confirmed():
    c = contract_for("hewlett_foundation", "Hewlett Foundation")
    assert c.scope_status is ScopeStatus.NEEDS_REVIEW
    assert c.needs_owner_decision
    assert c.owner_note, "a needs_review source must say what is missing"


def test_needs_review_does_not_disable_a_source_by_itself():
    """Applied literally, 'disable unconfirmed sources' switches off 71 of 85 —
    a judgement about someone else's business. The fields are independent; only
    evidence disables."""
    c = contract_for("hewlett_foundation")
    assert c.needs_owner_decision
    assert c.production_enabled


def test_devex_is_disabled_with_a_stated_reason():
    """11 runs, 0 pages, 0 rows, all recorded 'completed'."""
    c = MANIFESTS["devex"]
    assert not c.production_enabled
    assert "paywall" in c.known_defect.lower()
    assert "0 rows saved" in c.known_defect or "0 pages fetched" in c.known_defect


def test_every_disabled_source_explains_itself():
    for key, c in MANIFESTS.items():
        if not c.production_enabled:
            assert c.known_defect, f"{key} is disabled with no stated reason"


def test_known_defects_are_recorded_without_disabling_a_working_source():
    """DevNetJobsIndia's links are wrong, but it still produces real RFPs.
    Recording the defect and switching the source off are different acts."""
    c = MANIFESTS["devnet"]
    assert c.known_defect and "rfp_assignments.aspx" in c.known_defect
    assert c.production_enabled


# --------------------------------------------------------- deadline conventions

@pytest.mark.parametrize("key,expected", [
    ("devnet", "dayfirst"),      # Indian source: 31/07/2026
    ("ngobox", "dayfirst"),
    ("worldbank", "iso"),
    ("unpp", "iso"),
])
def test_deadline_convention_is_stated_per_source(key, expected):
    """A global default is how 09/01/2026 becomes the wrong day. The convention
    has to be a property of the source."""
    assert MANIFESTS[key].deadline_format == expected


# ------------------------------------------------------------ the review queue

def test_the_review_queue_lists_what_needs_a_decision():
    keys = ["worldbank", "unpp", "hewlett_foundation", "ospreys", "devnet"]
    pending = unconfirmed_sources(keys)
    assert "hewlett_foundation" in pending and "ospreys" in pending
    assert "worldbank" not in pending and "unpp" not in pending


def test_disabled_sources_report_why():
    got = disabled_sources(["devex", "worldbank", "ngobox"])
    assert set(got) == {"devex"}
    assert got["devex"]


def test_unicef_is_absent_and_must_stay_absent_until_someone_says_which_one():
    """Three legitimate candidates collect different things — Supply Division
    tenders, country-office notices, or UNGM. Inventing one would be exactly the
    guess the brief forbids."""
    assert "unicef" not in MANIFESTS
    c = contract_for("unicef")
    assert c.needs_owner_decision
