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


# =====================================================================
# The remaining eight priority sources.
#
# Everything below this line runs against SYNTHETIC fixtures — built from the
# field names and selectors each parser's own code documents, not captured from
# the site. See fixtures/README.md: a synthetic fixture catches a regression in
# our parser and cannot catch drift at the source, and saying so is the
# difference between a suite that is honest and one that is merely green.
# =====================================================================

# ------------------------------------------------------ provenance is declared

def test_every_fixture_declares_where_it_came_from():
    """A fixture nobody can trace is a fixture nobody can trust. The README's
    table is the register, and this stops a file being added without an entry —
    which is how a synthetic fixture quietly starts being read as a real one."""
    declared = load("README.md")
    for path in sorted(FIXTURES.glob("*")):
        if path.name == "README.md":
            continue
        assert f"`{path.name}`" in declared, (
            f"{path.name} is not in the provenance table in fixtures/README.md")


def test_the_provenance_table_does_not_list_files_that_are_gone():
    """The register drifting the other way is just as bad: a row claiming
    coverage that no longer exists."""
    import re as _re

    declared = set(_re.findall(r"`([\w.]+\.(?:json|html))`", load("README.md")))
    on_disk = {p.name for p in FIXTURES.glob("*") if p.name != "README.md"}
    assert declared - on_disk == set(), f"listed but missing: {declared - on_disk}"


def test_every_synthetic_fixture_says_so_inside_the_file():
    """So that somebody reading the fixture alone, without the README, cannot
    mistake it for a capture."""
    # fundsforngos_posts.json is exempt and only that one: the WordPress API
    # returns a bare JSON ARRAY, and there is nowhere in an array to put a
    # comment key without changing the shape the parser is being tested
    # against. Its provenance is in the README table like every other file.
    for name in ("developmentaid_tender_records.json", "unpp_open_projects.json",
                 "unpp_empty.json",
                 "ngobox_listing_card.html", "bond_opportunity_card.html",
                 "cleanairfund_grants_listing.html",
                 "undp_procurement_listing.html", "devex_paywall.html"):
        assert "SYNTHETIC" in load(name), name
    assert json.loads(load("fundsforngos_posts.json")).__class__ is list, (
        "if this became an object, it can carry a _comment like the rest")


# ----------------------------------------------------------- DevelopmentAid

def _devaid_items(slug="tenders"):
    from app.scrapers.developmentaid import DevelopmentAidScraper

    data = json.loads(load("developmentaid_tender_records.json"))
    s = DevelopmentAidScraper()
    return [s._raw_from_item(r, slug) for r in data["items"]]


def test_developmentaid_reads_every_record_in_the_fixture():
    items = _devaid_items()
    assert all(i is not None for i in items), "a record was silently dropped"
    assert len(items) == 4


def test_developmentaid_never_builds_a_url_out_of_the_donor_id_list():
    """The regression this pins: `_pick(item, 'id')` matched `donorIds` and
    produced /tenders/view/118345,118364 — a link 111 rows shared and none of
    which opened the opportunity."""
    for item in _devaid_items():
        assert "," not in item.opportunity_url, item.opportunity_url


def test_developmentaid_accepts_a_capitalised_id_key():
    """A payload keyed `Id` passed the record-set check and then failed the
    id lookup, leaving every row with an empty URL — which collapses the run to
    one dedupe entry and makes every row unsaveable. The two lookups have to
    agree, and the second fixture record is keyed `Id`."""
    item = _devaid_items()[1]
    assert item.opportunity_url.endswith("/118401"), item.opportunity_url


def test_developmentaid_treats_the_9999_sentinel_as_no_deadline():
    """9999-12-31 is their "no closing date". Read literally it produced a
    countdown of 2.9 million days."""
    from app.services.deadline_audit import is_sentinel
    from app.services.deadline_parser import DeadlineParser

    item = _devaid_items()[1]
    parsed = DeadlineParser().parse(item.deadline_raw, dayfirst=item.dayfirst)
    assert parsed is None or is_sentinel(parsed), item.deadline_raw


def test_developmentaid_prefers_a_named_donor_over_an_id_list():
    """Name-bearing keys first, so a bare id list can never win the race."""
    org = _devaid_items()[0].organization
    assert org and not org.replace(",", "").replace(" ", "").isdigit(), org


def test_developmentaid_stores_the_abbreviated_donor_name_when_both_exist():
    """Pinned as OBSERVED BEHAVIOUR, not endorsed.

    `_pick` returns the first key whose name contains the needle, and dicts
    iterate in insertion order — so a record carrying both
    `abbreviatedDonorNames: "EU"` and `donorNames: "European Commission"`
    stores "EU". The code names abbreviatedDonorNames deliberately, so this is
    intended rather than accidental.

    It is still worth someone deciding: the Organization column and the
    organisation filter then hold "EU" and "European Commission" as two
    different funders, which fragments both. Changing it is a product call
    about how that column reads, not a bug fix, so this test records what
    happens today and will fail loudly if it changes by accident.
    """
    assert _devaid_items()[0].organization == "EU"


def test_developmentaid_carries_the_source_location_into_country():
    """Without this the Country filter and the By Region chart were empty for
    every one of these rows."""
    assert _devaid_items()[0].country == "Kenya"


# --------------------------------------------------------- UN Partner Portal

def _unpp_items():
    from app.scrapers.unpp import UNPartnerPortalScraper

    data = json.loads(load("unpp_open_projects.json"))
    return UNPartnerPortalScraper()._rows_to_items(data["results"])


def test_unpp_fixture_carries_the_count_coverage_is_measured_against():
    """`unique / count`. Without an official total, coverage is unproven — and
    this field is the whole reason UNPP is one of the few sources whose
    coverage CAN be gated."""
    data = json.loads(load("unpp_open_projects.json"))
    assert isinstance(data["count"], int) and data["count"] > 0
    assert len(data["results"]) == data["count"]


def test_unpp_maps_every_open_record():
    items = _unpp_items()
    assert len(items) == 3
    assert all(i.title for i in items)


def test_unpp_builds_a_detail_url_per_record_not_a_listing_link():
    from app.services.links import link_kind

    for i in _unpp_items():
        assert link_kind(i.opportunity_url) == "deep", i.opportunity_url


def test_unpp_reads_the_agency_and_the_specialisation():
    first = _unpp_items()[0]
    assert "UNICEF" in first.organization
    assert "Nutrition" in first.vertical


def test_a_unpp_record_without_a_deadline_is_not_made_permanently_live():
    """Every row here is an open call, but one with no published closing date
    must not become an immortal row."""
    no_deadline = _unpp_items()[2]
    assert not no_deadline.deadline_raw
    assert no_deadline.assume_active is True, (
        "assume_active carries the source's word; audit_deadlines retires it")


def test_unpp_dates_are_read_as_iso_regardless_of_the_pipeline_default():
    from app.services.deadline_parser import DeadlineParser

    p = DeadlineParser()
    raw = _unpp_items()[0].deadline_raw
    assert p.parse(raw, dayfirst=True) == p.parse(raw, dayfirst=False)


def test_the_empty_unpp_response_is_positive_proof_not_a_parser_shrug():
    data = json.loads(load("unpp_empty.json"))
    assert data["count"] == 0 and data["results"] == []


# ---------------------------------------------------------------- NGOBOX

def _ngobox_items():
    from app.scrapers.ngobox import NGOBoxScraper

    return NGOBoxScraper().parse_listing(
        load("ngobox_listing_card.html"),
        "https://ngobox.org/grant_announcement_listing.php")


def test_ngobox_reads_the_two_real_cards_and_not_the_nav_card():
    """The third card links to /about_us.php. A parser that harvests every
    a.card-title stores 'About Us' as a grant."""
    items = _ngobox_items()
    assert len(items) == 2
    assert not any("About" in i.title for i in items)


def test_ngobox_reads_the_organisation_from_their_own_class_typo():
    """p.p_balck, not p.p_black. Correcting the spelling breaks the parser."""
    assert _ngobox_items()[0].organization == "Ministry of New and Renewable Energy"


def test_ngobox_deadlines_parse_dayfirst_as_its_manifest_declares():
    from app.services.deadline_parser import DeadlineParser
    from app.services.source_manifest import MANIFESTS

    assert MANIFESTS["ngobox"].deadline_format == "dayfirst"
    p = DeadlineParser()
    second = _ngobox_items()[1]
    assert second.deadline_raw.startswith("09 Jan")
    assert p.parse(second.deadline_raw, dayfirst=True).month == 1


def test_ngobox_finds_the_next_page_without_hardcoding_it():
    from app.scrapers.ngobox import NGOBoxScraper

    nxt = NGOBoxScraper().next_page(
        load("ngobox_listing_card.html"),
        "https://ngobox.org/grant_announcement_listing.php", 1)
    assert nxt is not None and "page=2" in nxt.url


# --------------------------------------------------------------- Bond UK

def _bond_items():
    from app.scrapers.bond import BondScraper

    return BondScraper().parse_listing(
        load("bond_opportunity_card.html"),
        "https://www.bond.org.uk/funding-opportunities/")


def test_bond_reads_both_cards():
    assert len(_bond_items()) == 2


def test_bond_reads_the_dl_meta_block():
    first = _bond_items()[0]
    assert first.deadline_raw.startswith("31 January")
    assert first.location == "Worldwide"
    assert "500" in first.funding_amount


def test_bond_names_the_funder_as_the_organisation():
    assert _bond_items()[0].organization == "Wellcome Trust Climate and Health Award"


def test_a_bond_card_with_no_apply_link_falls_back_to_the_index_anchor():
    """The defect the manifest records, pinned here so it cannot be fixed by
    accident and then silently regress.

    is_usable_link() ACCEPTS start_url#post-NNN, so the row is stored — but
    link_kind() calls it a listing, and the reader lands on the index they came
    from. Same class of defect as DevNetJobsIndia's 86 index-linked rows.
    verify_source.py holds Bond to 90% deep links, which is what measures how
    often this actually happens."""
    from app.services.links import is_usable_link, link_kind

    no_link = _bond_items()[1]
    assert "#post-90212" in no_link.opportunity_url
    assert is_usable_link(no_link.opportunity_url, "https://www.bond.org.uk")
    assert link_kind(no_link.opportunity_url) == "listing"


def test_bond_keeps_ongoing_as_written_rather_than_inventing_a_date():
    from app.services.deadline_parser import DeadlineParser

    ongoing = _bond_items()[1]
    assert ongoing.deadline_raw.lower() == "ongoing"
    p = DeadlineParser()
    assert p.parse(ongoing.deadline_raw) is None
    assert p.is_ongoing(ongoing.deadline_raw) is True


# ---------------------------------------------------------- FundsForNGOs

def _ffn_items():
    from app.scrapers.fundsforngos import FundsForNGOsScraper

    return FundsForNGOsScraper().parse_listing(
        load("fundsforngos_posts.json"),
        "https://www2.fundsforngos.org/wp-json/wp/v2/posts?per_page=50&page=1")


def test_fundsforngos_reads_every_post():
    assert len(_ffn_items()) == 3


def test_a_money_parenthetical_never_becomes_a_country():
    """"Smallholder Livelihoods Grant 2027 ($10,000)" — the trailing bracket is
    a country about as often as it is the grant size, and the wrong guess puts
    "$10,000" in the Country filter."""
    money = _ffn_items()[1]
    assert money.country == "", money.country


def test_a_real_country_parenthetical_is_kept():
    assert _ffn_items()[0].country == "Nigeria"


def test_the_slug_maps_to_a_vertical():
    assert _ffn_items()[0].vertical == "Health"
    assert _ffn_items()[1].vertical == "Livelihood"


def test_the_ambiguous_fundsforngos_date_is_the_open_question_not_a_settled_one():
    """09-01-2027 is 9 January or 1 September depending on a convention nobody
    has established for this source — and 48,350 stored deadlines rest on it.
    The manifest says so in as many words, and this asserts the note is still
    there rather than quietly resolved by someone guessing."""
    from app.services.source_manifest import MANIFESTS

    note = MANIFESTS["fundsforngos"].owner_note
    assert "DATE CONVENTION NOT ESTABLISHED" in note
    assert MANIFESTS["fundsforngos"].deadline_format == "", (
        "a convention was set without the audit that establishes it")


def test_an_all_numeric_fundsforngos_deadline_is_not_extracted_at_all():
    """A finding, pinned so it cannot be lost, and NOT fixed here.

    `_DEADLINE` is  deadline\\s*:?\\s*([0-3]?\\d[-/ ]\\w{3,9}[-/ ]\\d{2,4})  —
    the middle group needs at least three word characters, so it matches
    `15-Mar-2027` and `9 January 2027` and matches NOTHING in `09-01-2027` or
    `09/01/2027`. A post that writes its closing date numerically is therefore
    stored with no deadline at all: the row never expires on its own and sits
    in the UNKNOWN deadline state until audit_deadlines() retires it.

    This is deliberately left alone, because widening the pattern to accept
    `09-01-2027` immediately forces the question of whether that is 9 January
    or 1 September — which is exactly the convention the manifest says has
    never been established, across 48,350 stored rows. Settle the convention
    first (scripts/deadline_convention_audit.py --source FundsForNGOs), then
    widen the pattern. Doing it the other way round decides the meaning of
    those dates by accident.
    """
    from app.scrapers.fundsforngos import _DEADLINE

    assert _DEADLINE.search("Deadline: 15-Mar-2027")
    assert _DEADLINE.search("Deadline: 9 January 2027")
    assert _DEADLINE.search("Deadline: 09-01-2027") is None
    assert _DEADLINE.search("Deadline: 09/01/2027") is None
    # And what that means for a row, end to end:
    assert _ffn_items()[1].deadline_raw == ""


def test_a_post_with_no_deadline_carries_no_invented_one():
    assert _ffn_items()[2].deadline_raw == ""


def test_the_deadline_prefix_is_stripped_from_the_summary():
    assert not _ffn_items()[0].summary.lower().startswith("deadline")


# -------------------------------------------- Clean Air Fund (generic parser)

def _generic(key: str, html_name: str, page_url: str):
    """Drive the real configured scraper for a generic source, not a stand-in.

    Building a GenericListingScraper by hand here would test a class the
    pipeline never instantiates — the registered one is generated from
    sources.json at import time and carries that source's own settings.
    """
    import app.scrapers  # noqa: F401 — importing registers every plugin

    from app.scrapers.registry import SCRAPER_REGISTRY
    return SCRAPER_REGISTRY[key]().parse_listing(load(html_name), page_url)


def test_clean_air_fund_finds_the_open_call():
    items = _generic("clean_air_fund", "cleanairfund_grants_listing.html",
                     "https://www.cleanairfund.org/what-we-do/our-grants/")
    assert any("Clean Air in Cities" in i.title for i in items), \
        [i.title for i in items]


def test_clean_air_fund_does_not_store_the_privacy_policy():
    """The generic heuristic harvests links. Site furniture reaching the
    dashboard as a funding opportunity is the failure it is prone to."""
    from app.services.links import is_furniture

    items = _generic("clean_air_fund", "cleanairfund_grants_listing.html",
                     "https://www.cleanairfund.org/what-we-do/our-grants/")
    for i in items:
        assert not is_furniture(i.title, i.opportunity_url), i.title


def test_clean_air_funds_awarded_grants_are_excluded_by_its_manifest():
    """The reason this source needed a manifest at all: /our-grants/ is largely
    a portfolio of money already given, and nobody can apply to those."""
    from app.services.source_manifest import RecordType, contract_for, record_is_in_scope

    contract = contract_for("clean_air_fund")
    keep, why = record_is_in_scope(contract,
                                   record_type=RecordType.CONTRACT_AWARD.value)
    assert not keep and "excluded" in why
    keep_open, _ = record_is_in_scope(contract, record_type=RecordType.GRANT.value)
    assert keep_open


def test_clean_air_fund_is_not_treated_as_a_curated_notice_board():
    """Unlike NGOBOX or UNDP, this page is not one. Marking it curated would
    exempt its rows from the funding-vocabulary test and let the awarded
    portfolio through."""
    from app.services.source_manifest import contract_for

    assert contract_for("clean_air_fund").curated is False


# ------------------------------------------- UNDP Procurement (generic parser)

def test_undp_reads_the_notice_rows():
    items = _generic("undp_procurement", "undp_procurement_listing.html",
                     "https://procurement-notices.undp.org/?lang=en")
    titles = " ".join(i.title for i in items)
    assert "Solar Mini-Grid" in titles, [i.title for i in items]


def test_undp_notices_link_to_the_notice_not_the_board():
    from app.services.links import link_kind

    items = _generic("undp_procurement", "undp_procurement_listing.html",
                     "https://procurement-notices.undp.org/?lang=en")
    deep = [i for i in items if link_kind(i.opportunity_url) == "deep"]
    assert deep, "every row links back to the index"


def test_the_undp_fixture_carries_the_total_its_coverage_needs():
    """"Showing 1 - 3 of 1,284 notices". Coverage without that number is
    unproven, and this is where verify_source.py's --official-total comes from."""
    import re as _re

    m = _re.search(r"of\s+([\d,]+)\s+notices", load("undp_procurement_listing.html"))
    assert m and int(m.group(1).replace(",", "")) == 1284


def test_undp_is_curated_because_every_row_is_a_published_notice():
    from app.services.source_manifest import contract_for

    c = contract_for("undp_procurement")
    assert c.expected_types and c.scope_status.value == "confirmed"


# ------------------------------------------------------------------ Devex

def test_the_devex_wall_is_classified_as_auth_required_not_an_empty_source():
    """11 runs, 0 pages, 0 rows — and every one recorded 'completed'. That is
    the single most misleading state in the whole platform: a source that
    cannot be reached at all, reported as a source with nothing to offer."""
    from app.services.scrape_outcome import ErrorCode, Evidence, Outcome, classify

    outcome, code, _msg = classify(Evidence(
        pages_fetched=0, extracted=0,
        auth_required=True,
        page_title="Sign in to continue | Devex",
        body_sample=load("devex_paywall.html").lower()[:4000],
        fetch_mode="http",
    ))
    assert outcome is Outcome.AUTH_REQUIRED
    assert code is ErrorCode.LOGIN_WALL


def test_the_devex_wall_is_never_confirmed_empty():
    """CONFIRMED_EMPTY is healthy and stops anyone investigating. A login wall
    must never reach it — emptiness needs positive proof from the source, and a
    paywall proves the opposite."""
    from app.services.scrape_outcome import Evidence, Outcome, classify

    outcome, _, _ = classify(Evidence(
        pages_fetched=0, extracted=0, auth_required=True,
        page_title="Sign in to continue | Devex",
        body_sample=load("devex_paywall.html").lower()[:4000]))
    assert outcome is not Outcome.CONFIRMED_EMPTY


def test_the_devex_fixture_holds_no_subscriber_content():
    """It is the anonymous view by construction. Asserted because a later
    'let me just grab a real page' would put paid content into a public repo."""
    text = load("devex_paywall.html").lower()
    assert "devex pro members" in text
    for marker in ("funding opportunity", "deadline:", "grant amount"):
        assert marker not in text, f"the wall fixture contains listing data: {marker}"
