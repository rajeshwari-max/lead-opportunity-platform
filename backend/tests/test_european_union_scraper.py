from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.database.models import Category
from app.scrapers.european_union import (
    ACTIVE_STATUS_CODES,
    PROPOSALS_URL,
    PROPOSAL_TYPES,
    TENDERS_URL,
    EuropeanUnionScraper,
    api_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "eu_funding_tenders.json"


def test_eu_uses_both_requested_official_pages_and_a_dedicated_scraper():
    from app.scrapers import SCRAPER_REGISTRY
    from app.scrapers.generic_listing import _load_config

    assert PROPOSALS_URL.endswith("/calls-for-proposals")
    assert TENDERS_URL == (
        "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
        "opportunities/calls-for-tenders?isExactMatch=true&order=DESC&"
        "pageNumber=1&pageSize=50&sortBy=startDate"
    )
    assert SCRAPER_REGISTRY["european_union"] is EuropeanUnionScraper
    configured = {row["name"]: row for row in _load_config()}
    assert configured["european_union"]["url"] == PROPOSALS_URL
    assert configured["european_union"]["tender_url"] == TENDERS_URL


def test_api_contract_requests_all_proposal_types_tenders_active_statuses_and_english():
    scraper = EuropeanUnionScraper()
    must = scraper._query["bool"]["must"]

    assert set(must[0]["terms"]["type"]) == {"0", *PROPOSAL_TYPES}
    assert set(must[1]["terms"]["status"]) == ACTIVE_STATUS_CODES
    assert scraper._languages == ["en"]
    assert scraper._sort == {"field": "startDate", "order": "DESC"}


def test_parser_emits_proposals_and_tenders_with_their_real_deadlines():
    scraper = EuropeanUnionScraper()
    payload = FIXTURE.read_text(encoding="utf-8")
    items = scraper.parse_listing(payload, api_url(page_size=2))

    assert len(items) == 2
    proposal, tender = items

    assert proposal.category_hint is Category.PROPOSAL
    assert proposal.record_type == "call_for_proposals"
    assert proposal.source_status == "31094501"
    assert proposal.deadline_raw == "2027-12-01"
    assert proposal.website == PROPOSALS_URL
    assert proposal.opportunity_url.endswith(
        "/topic-details/HORIZON-CL5-2027-07-D3-11"
    )

    assert tender.category_hint is Category.TENDER
    assert tender.record_type == "tender"
    assert tender.source_status == "31094502"
    assert tender.deadline_raw == "2026-12-12"
    assert tender.website == TENDERS_URL
    assert tender.organization == (
        "European Education and Culture Executive Agency (EACEA)"
    )
    assert tender.funding_amount == "8500000 EUR"
    assert tender.opportunity_url.endswith(
        "/tender-details/c0ea897b-edcd-4e5d-8bf2-7380e4636c6d-CN"
    )
    assert "Promote European higher education" in tender.summary


def test_parser_rejects_closed_non_english_and_duplicate_records():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    duplicate = json.loads(json.dumps(payload["results"][0]))
    french = json.loads(json.dumps(payload["results"][0]))
    french["reference"] = "50129464TOPICSfr"
    french["language"] = "fr"
    payload["results"].extend([duplicate, french])

    items = EuropeanUnionScraper().parse_listing(
        json.dumps(payload), api_url(page_size=5)
    )
    assert len(items) == 2


def test_parser_deduplicates_a_record_repeated_on_the_next_api_page():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["results"] = [payload["results"][0]]
    scraper = EuropeanUnionScraper()

    first = scraper.parse_listing(json.dumps(payload), api_url(page=1, page_size=1))
    repeated = scraper.parse_listing(json.dumps(payload), api_url(page=2, page_size=1))

    assert len(first) == 1
    assert repeated == []


def test_pagination_advances_page_number_and_stops_at_total():
    scraper = EuropeanUnionScraper()
    first_url = api_url(page=1, page_size=2)
    payload = FIXTURE.read_text(encoding="utf-8")
    scraper.parse_listing(payload, first_url)

    request = scraper.next_page(payload, first_url, 1)
    assert request is not None
    assert parse_qs(urlparse(request.url).query)["pageNumber"] == ["2"]

    last = json.loads(payload)
    last["results"] = [last["results"][0]]
    last["pageNumber"] = 2
    last_text = json.dumps(last)
    scraper.parse_listing(last_text, request.url)
    assert scraper.next_page(last_text, request.url, 2) is None
