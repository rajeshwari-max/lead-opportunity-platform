import ast
import inspect
import json

from app.scrapers import worldbank
from app.scrapers.worldbank import (
    CANONICAL_URL,
    WorldBankScraper,
    _paged_endpoint,
    is_first_party_data_request,
    notice_evidence,
    pick_data_endpoint,
)


def test_canonical_page_is_the_runtime_start():
    scraper = WorldBankScraper()
    assert scraper.start_url == "https://projects.worldbank.org/en/projects-operations/opportunities"
    assert scraper.start_url == CANONICAL_URL
    assert scraper.requires_js is True


def test_no_hard_coded_data_endpoint_is_reachable_code():
    tree = ast.parse(inspect.getsource(worldbank))
    strings = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    executable_strings = [s for s in strings if not s.startswith("World Bank current")]
    assert not any("search.worldbank.org/api/v2/procnotices" in s
                   for s in executable_strings)


def test_first_party_host_check_rejects_suffix_attack():
    assert is_first_party_data_request(
        "https://search.worldbank.org/api/procurement?status=current")
    assert not is_first_party_data_request(
        "https://notworldbank.org/api/procurement?status=current")
    assert not is_first_party_data_request(
        "https://worldbank.org.attacker.example/api/procurement?status=current")


def test_filtered_observed_request_wins_over_bare_request():
    bare = "https://search.worldbank.org/api/procurement?format=json"
    filtered = bare + "&status=current&fct=notice_type"
    assert pick_data_endpoint([bare, filtered]) == filtered


def test_no_observed_request_means_no_substitute():
    assert pick_data_endpoint([]) == ""
    assert pick_data_endpoint([CANONICAL_URL]) == ""


def test_offset_pagination_preserves_filters():
    first = ("https://search.worldbank.org/api/procurement?format=json&"
             "status=current&rows=34&os=0")
    second = _paged_endpoint(first, 2, 34)
    assert "status=current" in second
    assert "rows=34" in second
    assert "os=34" in second


def test_page_number_pagination_preserves_filters():
    first = "https://api.worldbank.org/procurement?status=open&pageNumber=1&limit=20"
    assert "pageNumber=3" in _paged_endpoint(first, 3, 20)


def test_record_requires_notice_evidence():
    assert notice_evidence({"project_name": "A development project"}) == ""
    assert notice_evidence({"bid_reference_no": "REF-1"}) == "bid_reference_no"


def _payload(**overrides):
    row = {
        "id": "123",
        "notice_type": "Request for Expressions of Interest",
        "bid_description": "Consultancy opportunity",
        "submission_deadline_date": "2026-09-30T00:00:00Z",
        "submission_date": "2026-08-01T00:00:00Z",
        "project_ctry_name": "India",
        "project_name": "Programme name",
    }
    row.update(overrides)
    return json.dumps({"procnotices": [row], "total": 1})


def test_closing_date_wins_over_publication_date():
    item = WorldBankScraper().parse_listing(_payload(), CANONICAL_URL)[0]
    assert item.deadline_raw == "2026-09-30"


def test_project_only_record_is_rejected_before_ingest():
    raw = json.dumps({"procnotices": [{"id": "1", "project_name": "Project"}]})
    scraper = WorldBankScraper()
    assert scraper.parse_listing(raw, CANONICAL_URL) == []
    assert scraper.rejection_counts()["no evidence of a procurement notice"] == 1


def test_contract_award_is_rejected():
    scraper = WorldBankScraper()
    assert scraper.parse_listing(_payload(notice_type="Contract Award"), CANONICAL_URL) == []
    assert scraper.rejection_counts()["closed/already-decided notice (award/cancelled)"] == 1
