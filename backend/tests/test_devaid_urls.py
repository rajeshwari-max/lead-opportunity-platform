from urllib.parse import parse_qs, urlparse

from app.scrapers.developmentaid import _SECTION_URLS, _page_url
from scripts.devaid_urls import EXPECTED, audit


def test_runtime_grants_url_is_exact():
    assert _SECTION_URLS["grants"] == EXPECTED["grants"]


def test_runtime_tenders_url_is_exact():
    assert _SECTION_URLS["tenders"] == EXPECTED["tenders"]


def test_both_urls_are_filtered_to_open_english_records():
    for url in _SECTION_URLS.values():
        query = parse_qs(urlparse(url).query)
        assert query["statuses"] == ["3"]
        assert query["languages"] == ["92"]
        assert query["sectors"] and len(query["sectors"][0].split(",")) > 10


def test_pagination_preserves_every_filter():
    for url in _SECTION_URLS.values():
        original = parse_qs(urlparse(url).query)
        paged = parse_qs(urlparse(_page_url(url, 2)).query)
        assert paged.pop("pageNr") == ["2"]
        assert paged == original


def test_audit_passes_for_the_current_runtime():
    passed, rows = audit()
    assert passed
    assert {row["section"] for row in rows} == {
        "grants", "tenders", *(f"brand-{i}" for i in range(1, 9))}



def test_brand_keyword_searches_keep_their_keywords_on_every_page():
    """The Setu, Community Action Collab and Upfront searches are filtered by
    keywords alone, so losing searchedText while paging would turn them into
    an unfiltered walk of every grant on the site."""
    from app.scrapers.developmentaid import _BRAND_SEARCHES

    keyword = [url for url, _ in _BRAND_SEARCHES if "searchedText=" in url]
    assert len(keyword) == 3
    for url in keyword:
        text = parse_qs(urlparse(url).query)["searchedText"]
        assert parse_qs(urlparse(_page_url(url, 5)).query)["searchedText"] == text
