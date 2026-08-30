"""Parser contracts, so drift fails a test instead of a night's scrape.

The brief: "Fixture-based parser contracts for every priority scraper", from
"sanitized representative HTML/JSON fragments — not credentials, cookies, or
full private pages."

Why this is the missing half of `PARSE_ZERO`
--------------------------------------------
The outcome taxonomy can already say "the page loaded and the parser found
nothing". It cannot say WHY, and without a fixture the only way to find out is
to re-run the scraper against a live site that may have changed again. A
fixture turns "PARSE_ZERO, cause unknown" into a named failure with a diff, and
it fails in CI at the moment the parser stops matching what the source sends —
not three weeks later when someone notices the source went quiet.

Every fixture here is safe to publish. The repository is public.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------ hygiene first

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*")))
def test_no_fixture_contains_anything_secret(path):
    """The rule the brief states, enforced rather than trusted. A fixture is
    committed to a public repository, and a cookie pasted into one is published
    the moment it lands."""
    if path.name == "README.md":
        return
    text = path.read_text(encoding="utf-8").lower()
    for marker in ("set-cookie", "authorization:", "bearer ", "sessionid=",
                   "password", "passwd", "secret_key", "api_key",
                   "csrftoken", "__cfduid", "aspxauth"):
        assert marker not in text, f"{path.name} contains {marker!r}"


# ------------------------------------------------- World Bank: the JSON API

def test_world_bank_fixture_matches_the_shape_the_parser_reads():
    """The contract: these field names and this nesting. When the API renames
    `notice_type` or moves the array, this is the test that says so."""
    data = json.loads(load("worldbank_procnotice.json"))
    assert "procnotices" in data, "the row container was renamed"
    for rec in data["procnotices"]:
        assert "id" in rec
        assert "notice_type" in rec
        assert "submission_deadline_date" in rec or "deadline" in rec


def test_the_award_in_the_fixture_is_rejected_by_its_own_notice_type():
    from app.services.notice_types import record_type_for
    from app.services.source_manifest import contract_for, record_is_in_scope

    data = json.loads(load("worldbank_procnotice.json"))
    award = next(r for r in data["procnotices"]
                 if r.get("notice_type") == "Contract Award")
    keep, why = record_is_in_scope(
        contract_for("world_bank"),
        record_type=record_type_for(award["notice_type"]),
        source_status=award["notice_type"])
    assert not keep and "excluded" in why


def test_the_open_notice_in_the_fixture_is_kept():
    from app.services.notice_types import record_type_for
    from app.services.source_manifest import contract_for, record_is_in_scope

    data = json.loads(load("worldbank_procnotice.json"))
    bid = next(r for r in data["procnotices"]
               if r.get("notice_type") == "Invitation for Bids")
    keep, _ = record_is_in_scope(
        contract_for("world_bank"),
        record_type=record_type_for(bid["notice_type"]),
        source_status=bid["notice_type"])
    assert keep


def test_the_record_with_only_a_project_name_is_treated_as_a_project():
    """The third fixture record has no bid_description and an empty
    notice_type — the exact shape that used to be titled from `project_name`
    and then read as a project on the dashboard."""
    from app.services.source_manifest import RecordType, contract_for, record_is_in_scope

    data = json.loads(load("worldbank_procnotice.json"))
    rec = data["procnotices"][2]
    assert not rec.get("bid_description")
    assert not rec.get("notice_type")
    assert rec.get("project_name")
    keep, _ = record_is_in_scope(contract_for("world_bank"),
                                 record_type=RecordType.PROJECT.value)
    assert not keep


def test_an_empty_api_response_carries_the_proof_confirmed_empty_needs():
    """`total: 0` from the API is positive evidence. A parser returning zero is
    not, and the two must never collapse into the same outcome."""
    data = json.loads(load("worldbank_empty.json"))
    assert data["total"] == 0
    assert data["procnotices"] == []


def test_world_bank_iso_deadlines_parse_to_themselves():
    from app.services.deadline_parser import DeadlineParser

    data = json.loads(load("worldbank_procnotice.json"))
    parser = DeadlineParser()
    raw = data["procnotices"][0]["submission_deadline_date"]
    # The pipeline default is dayfirst=True; an ISO date must ignore it.
    assert parser.parse(raw, dayfirst=True) == parser.parse(raw, dayfirst=False)


# ------------------------------------------------------------- ADB: markup

def test_adb_fixture_still_has_the_label_value_spans_the_parser_needs():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(load("adb_listing_row.html"), "lxml")
    blocks = soup.select("div.searchstax-search-result")
    assert len(blocks) == 2, "the result block class changed"
    for block in blocks:
        spans = block.select("span.searchstax-search-result-common")
        assert spans, "the Label: value spans are gone"
        labels = {s.get_text(" ", strip=True).partition(":")[0].strip().lower()
                  for s in spans}
        assert "status" in labels and "notice type" in labels


def test_adb_award_row_is_rejected_even_though_its_status_says_active():
    """The row the local status filter cannot catch: Status: Active, Notice
    type: Contract Award. Status alone would keep it."""
    from bs4 import BeautifulSoup

    from app.services.notice_types import record_type_for
    from app.services.source_manifest import contract_for, record_is_in_scope

    soup = BeautifulSoup(load("adb_listing_row.html"), "lxml")
    award = soup.select("div.searchstax-search-result")[1]
    fields = {}
    for s in award.select("span.searchstax-search-result-common"):
        label, _, value = s.get_text(" ", strip=True).partition(":")
        fields[label.strip().lower()] = value.strip()
    assert fields["status"] == "Active"
    keep, _ = record_is_in_scope(contract_for("adb_tenders"),
                                 record_type=record_type_for(fields["notice type"]),
                                 source_status=fields["status"])
    assert not keep


# --------------------------------------------------------- DevNetJobsIndia

def test_devnet_fixture_covers_both_the_linked_and_postback_row():
    """The postback-only row is the one that produced 86 records sharing a
    single URL. A fixture without it would pass while the real defect
    persisted."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(load("devnet_listing_row.html"), "lxml")
    rows = soup.select("table[id*=grdJobs] tr")
    assert len(rows) == 2
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    assert any("job_id=" in h for h in hrefs), "the direct-link row is missing"
    assert any("__doPostBack" in h for h in hrefs), "the postback row is missing"


def test_devnet_dates_are_read_dayfirst_as_its_manifest_declares():
    from app.services.deadline_parser import DeadlineParser
    from app.services.source_manifest import MANIFESTS

    assert MANIFESTS["devnet"].deadline_format == "dayfirst"
    parser = DeadlineParser()
    # From the fixture: an unambiguous date and an ambiguous one.
    assert parser.parse("31/07/2027", dayfirst=True).month == 7
    assert parser.parse("09/01/2027", dayfirst=True).day == 9


# ------------------------------------------------ what a drift test buys you

def test_a_structure_signature_changes_when_the_markup_does():
    """`STRUCTURE_CHANGED` needs positive evidence, and this is where it comes
    from: the same page shape hashes the same, a changed one does not."""
    import hashlib

    from bs4 import BeautifulSoup

    def signature(html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        tags = sorted({f"{t.name}.{'.'.join(t.get('class') or [])}"
                       for t in soup.find_all(True)})
        return hashlib.sha256("|".join(tags).encode()).hexdigest()[:16]

    original = load("adb_listing_row.html")
    assert signature(original) == signature(original)
    drifted = original.replace("searchstax-search-result-common", "result-meta")
    assert signature(original) != signature(drifted)
