"""World Bank was storing projects, and the reason was that nothing was told.

Two independent faults, both of which had to be fixed for the source's own
contract to mean anything:

1. `RawOpportunity` gained `record_type` and `source_status`, `_ingest` calls
   `record_is_in_scope`, the manifests exclude `contract_award` and `project` —
   and **no scraper ever populated either field**. Both World Bank and ADB read
   the notice type, used it locally, and wrote it into the summary TEXT. The
   contract saw a blank and kept everything.

2. World Bank's title chain fell back to `project_name`, so a record with no
   bid description was titled with the project it belongs to and then read on
   the dashboard as a project.
"""
from __future__ import annotations

import pytest

from app.services.notice_types import record_type_for
from app.services.source_manifest import (
    MANIFESTS,
    RecordType,
    contract_for,
    record_is_in_scope,
)


# ------------------------------------------------- the source's own wording

@pytest.mark.parametrize("notice,expected", [
    ("Contract Award Notice", "contract_award"),
    ("Contract Award", "contract_award"),
    ("Cancellation Notice", "contract_award"),
    ("Annulment Notice", "contract_award"),
    ("Invitation for Bids", "itb"),
    ("Invitation to Bid", "itb"),
    ("Request for Expressions of Interest", "eoi"),
    ("Request for Proposals", "rfp"),
    ("Request for Quotation", "rfq"),
    ("General Procurement Notice", "tender"),
    ("Specific Procurement Notice", "tender"),
    ("Consultant Qualification", "consultancy"),
    ("Prequalification", "itb"),
])
def test_real_notice_wording_maps_to_a_record_type(notice, expected):
    assert record_type_for(notice) == expected


def test_a_finished_notice_wins_over_a_generic_word_in_the_same_string():
    """"Contract Award Notice" contains both "award" and "notice". A rule that
    reached "procurement notice" first would file an award as an open tender —
    the exact mistake World Bank's manifest exists to prevent."""
    assert record_type_for("Contract Award Notice") == "contract_award"
    assert record_type_for("Cancellation of Procurement Notice") == "contract_award"


def test_an_unrecognised_wording_maps_to_nothing_rather_than_a_guess():
    """Empty means "this source said something we have no rule for". The
    contract treats that as unknown, not as grounds to discard — a vocabulary
    nobody has configured must not silently delete a source's output."""
    assert record_type_for("Some New Wording Nobody Configured") == ""
    assert record_type_for("") == ""
    assert record_type_for(None) == ""


# --------------------------------------------- what the contract now rejects

def test_world_bank_rejects_a_contract_award_end_to_end():
    """From the source's own string, through the mapping, to the verdict —
    the path that was broken in the middle."""
    keep, why = record_is_in_scope(
        contract_for("world_bank"),
        record_type=record_type_for("Contract Award Notice"),
        source_status="Contract Award Notice")
    assert not keep and "excluded" in why


def test_world_bank_rejects_a_project_record():
    """The user's report: World Bank rows that are projects, not tenders."""
    keep, why = record_is_in_scope(contract_for("world_bank"),
                                   record_type=RecordType.PROJECT.value)
    assert not keep and "excluded" in why


@pytest.mark.parametrize("notice", [
    "Invitation for Bids",
    "Request for Expressions of Interest",
    "General Procurement Notice",
    "Consultant Qualification",
])
def test_world_bank_keeps_every_kind_of_open_notice(notice):
    """The fix must not work by rejecting more. These are what the source is
    for."""
    keep, _ = record_is_in_scope(contract_for("world_bank"),
                                 record_type=record_type_for(notice),
                                 source_status=notice)
    assert keep, notice


# ------------------- an incomplete expected list must not become a deletion

def test_adb_keeps_its_own_invitations_for_bids():
    """ADB's expected_types omits `itb`, and "Invitation for Bids" is its main
    output. Treating that list as an allowlist would have discarded most of
    what ADB produces the moment record_type started being populated."""
    keep, why = record_is_in_scope(contract_for("adb_tenders"),
                                   record_type="itb",
                                   source_status="Invitation for Bids")
    assert keep, why


def test_no_manifest_treats_its_expected_list_as_exhaustive_yet():
    """None of these lists has been checked against a real sample of what the
    source emits. Until one has, `expected_types` describes; `excluded_types`
    enforces."""
    for key, contract in MANIFESTS.items():
        assert not contract.expected_types_exhaustive, (
            f"{key} claims its expected_types list is complete — verify that "
            f"against real records first, because it now deletes anything "
            f"outside it")


def test_an_exhaustive_list_does_reject_when_a_source_opts_in():
    """The mechanism still exists for a source someone HAS audited."""
    from dataclasses import replace

    strict = replace(MANIFESTS["adb"], expected_types_exhaustive=True)
    keep, why = record_is_in_scope(strict, record_type="itb")
    assert not keep and "expected types" in why


def test_excluded_always_beats_expected():
    """An excluded type is rejected whether or not the expected list is
    exhaustive — otherwise the enforcement half would depend on the
    descriptive half being filled in."""
    keep, _ = record_is_in_scope(contract_for("world_bank"),
                                 record_type="contract_award")
    assert not keep


# ------------------------------------ the scrapers actually pass the fields

def test_the_world_bank_scraper_hands_over_the_source_s_own_fields():
    """The defect in one assertion: the value existed, was used locally, was
    written into the summary text, and was never given to the contract."""
    import inspect

    from app.scrapers.worldbank import WorldBankScraper

    src = inspect.getsource(WorldBankScraper)
    assert "record_type=" in src
    assert "source_status=" in src


def test_the_adb_scraper_hands_over_the_source_s_own_fields():
    import inspect

    from app.scrapers.adb import AdbTendersScraper

    src = inspect.getsource(AdbTendersScraper)
    assert "record_type=" in src
    assert "source_status=" in src


def test_a_world_bank_row_titled_only_from_project_name_is_rejected():
    """A project name is context, not evidence that bids are being accepted."""
    import json

    from app.scrapers.worldbank import WorldBankScraper

    scraper = WorldBankScraper()
    raw = json.dumps({"procnotices": [{
        "id": "project-only", "project_name": "Rural roads programme",
    }]})
    assert scraper.parse_listing(raw, "") == []
    assert scraper.rejection_counts()["no evidence of a procurement notice"] == 1
