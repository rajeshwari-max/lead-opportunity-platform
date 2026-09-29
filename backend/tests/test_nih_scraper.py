from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.database.models import Category
from app.scrapers.nih import (
    FUNDING_CATEGORIES,
    LISTING_URL,
    NIHOpportunitiesScraper,
    _detail_url,
    api_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "nih_opportunities.json"


def test_nih_uses_the_new_official_listing_and_a_dedicated_scraper():
    from app.scrapers import SCRAPER_REGISTRY
    from app.scrapers.generic_listing import _load_config

    assert LISTING_URL == "https://grants.nih.gov/funding/explore-nih-opportunities"
    assert SCRAPER_REGISTRY["national_institutes_of_health"] is NIHOpportunitiesScraper
    configured = {row["name"]: row for row in _load_config()}
    assert configured["national_institutes_of_health"]["url"] == LISTING_URL


def test_query_is_active_only_all_categories_and_offset_paginated():
    url = api_url(offset=200, page_size=100, today=date(2026, 9, 29))
    query = parse_qs(urlparse(url).query, keep_blank_values=True)

    assert query["type"] == ["active"]
    assert query["activitycodes"] == ["all"]
    assert query["from"] == ["200"]
    assert query["perpage"] == ["100"]
    assert query["daterange"] == ["01011991-09292026"]
    assert set(FUNDING_CATEGORIES) == {
        "Construction and Modernization",
        "Research and Development",
        "Research and Development, Small Business",
        "Research Training and Career Development",
        "Small Business",
    }


def test_parser_emits_only_active_grants_with_deadline_and_direct_link():
    scraper = NIHOpportunitiesScraper()
    payload = FIXTURE.read_text(encoding="utf-8")
    items = scraper.parse_listing(payload, api_url(page_size=2, today=date(2026, 9, 29)))

    assert len(items) == 1
    item = items[0]
    assert item.title.startswith("HEAL Initiative")
    assert item.organization == "National Institutes of Health (NCCIH)"
    assert item.deadline_raw == "2026-10-31"
    assert item.opportunity_url == "https://simpler.grants.gov/opportunity/361304"
    assert item.website == LISTING_URL
    assert item.category_hint is Category.GRANT
    assert item.record_type == "grant"
    assert item.source_status == "active"
    assert "Activity codes: R61, R33" in item.summary


def test_a_transition_record_without_grants_gov_id_gets_a_unique_nih_search_link():
    assert _detail_url({"docnum": "RFA-OD-27-001"}) == (
        f"{LISTING_URL}?query=RFA-OD-27-001&fields=title"
    )


def test_pagination_advances_by_page_size_and_stops_at_total():
    scraper = NIHOpportunitiesScraper()
    first_url = api_url(offset=0, page_size=2, today=date(2026, 9, 29))
    payload = FIXTURE.read_text(encoding="utf-8")
    scraper.parse_listing(payload, first_url)

    request = scraper.next_page(payload, first_url, 1)
    assert request is not None
    assert parse_qs(urlparse(request.url).query)["from"] == ["2"]

    last = json.loads(payload)
    last["data"]["hits"]["hits"] = [last["data"]["hits"]["hits"][0]]
    last_url = request.url
    last_text = json.dumps(last)
    scraper.parse_listing(last_text, last_url)
    assert scraper.next_page(last_text, last_url, 2) is None
