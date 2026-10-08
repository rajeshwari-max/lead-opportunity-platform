from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date

import httpx
import pytest

from app.database.models import Category
from app.scrapers.web_discovery import (
    WebDiscoveryScraper,
    _category_hint,
    _normalise_url,
    _safe_public_url,
    build_queries,
)


def test_queries_cover_india_then_south_asia_then_global(monkeypatch):
    monkeypatch.setattr("app.scrapers.web_discovery.settings.web_discovery_max_queries", 18)
    monkeypatch.setattr("app.scrapers.web_discovery.settings.web_discovery_extra_queries", "")

    queries = build_queries(date(2026, 10, 4))

    assert len(queries) == 18
    assert all("India" in query for query in queries[:6])
    assert all("South Asia" in query for query in queries[6:12])
    assert all("global OR international" in query for query in queries[12:])
    assert all("2026 OR 2027" in query for query in queries)
    joined = " ".join(queries)
    for required in ("health", "agriculture", "climate", "evaluation",
                     "worker wellbeing", "social protection",
                     "innovative finance", "social business", "resilience"):
        assert required in joined


def test_operator_queries_get_first_claim_on_the_budget(monkeypatch):
    monkeypatch.setattr("app.scrapers.web_discovery.settings.web_discovery_max_queries", 2)
    monkeypatch.setattr(
        "app.scrapers.web_discovery.settings.web_discovery_extra_queries",
        "open grant digital health India|RFP migrant workers Nepal",
    )
    assert build_queries(date(2026, 10, 4)) == [
        "open grant digital health India",
        "RFP migrant workers Nepal",
    ]


def test_result_url_drops_tracking_but_keeps_functional_query_parameters():
    url = _normalise_url(
        "https://Example.org/call?id=42&utm_source=mail&gclid=secret#apply"
    )
    assert url == "https://example.org/call?id=42"


@pytest.mark.parametrize("url", [
    "http://localhost/admin",
    "http://127.0.0.1/private",
    "http://169.254.169.254/latest/meta-data/",
    "https://internal.local/call",
    "https://www.facebook.com/posts/123",
    "file:///etc/passwd",
])
def test_private_social_and_non_http_destinations_are_never_fetched(url):
    assert not _safe_public_url(url)


def test_category_hint_keeps_category_separate_from_hierarchy():
    assert _category_hint("Request for Proposals: health evaluation")[0] is Category.RFP
    assert _category_hint("Invitation to bid for solar equipment")[0] is Category.TENDER
    assert _category_hint("Open grant for smallholder livelihoods")[0] is Category.GRANT
    assert _category_hint("Call for proposals on resilience")[0] is Category.PROPOSAL


def test_deadline_reader_selects_a_future_deadline_and_rejects_past_only():
    scraper = WebDiscoveryScraper()
    raw, parsed, rolling = scraper._deadline(
        "Applications opened in 2024. Closing date: 31 December 2099."
    )
    assert raw == "31 December 2099"
    assert parsed == date(2099, 12, 31)
    assert not rolling

    raw, parsed, rolling = scraper._deadline("Deadline: 12 January 2020")
    assert raw == ""
    assert parsed is None
    assert not rolling

    raw, parsed, rolling = scraper._deadline(
        "This is an ongoing programme. Applications are accepted on a rolling basis."
    )
    assert raw == "Rolling"
    assert parsed is None
    assert rolling

    raw, parsed, rolling = scraper._deadline(
        "This is an ongoing programme. Applications are currently closed."
    )
    assert raw == ""
    assert parsed is None
    assert not rolling


def test_candidate_redirect_cannot_reach_ec2_metadata():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
            request=request,
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="unsafe redirect"):
                await WebDiscoveryScraper._fetch_public_page(
                    client, "https://example.org/redirect")

    asyncio.run(exercise())
    assert requested == ["https://example.org/redirect"]


def test_candidate_page_becomes_a_normal_raw_opportunity(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.web_discovery.settings.web_discovery_require_deadline", True)
    html = """
    <html><head>
      <meta property="og:site_name" content="Example Foundation">
      <meta name="description" content="Funding for public health systems in India.">
    </head><body>
      <h1>Open Grant for Community Health Systems in India</h1>
      <p>Applications are open. Closing date: 31 December 2099.</p>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=html, headers={"content-type": "text/html"}, request=request)

    async def exercise():
        scraper = WebDiscoveryScraper()
        counters = Counter()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = await scraper._candidate(client, {
                "title": "Open Grant for Community Health Systems in India",
                "url": "https://example.org/funding/health-2099?utm_source=search",
                "description": "Applications are invited for a public health grant.",
            }, counters)
        return item

    item = asyncio.run(exercise())

    assert item is not None
    assert item.source_website == "Whole Web Discovery"
    assert item.deadline_raw == "31 December 2099"
    assert item.category_hint is Category.GRANT
    assert item.country == "India"
    assert item.region == "South Asia"
    assert item.organization == "Example Foundation"
    assert item.opportunity_url == "https://example.org/funding/health-2099"


def test_undated_search_hit_is_not_admitted(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.web_discovery.settings.web_discovery_require_deadline", True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<h1>Grant programme overview</h1><p>Read about our work.</p>",
            headers={"content-type": "text/html"},
            request=request,
        )

    async def exercise():
        scraper = WebDiscoveryScraper()
        counters = Counter()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = await scraper._candidate(client, {
                "title": "Grant programme overview",
                "url": "https://example.org/grants/programme",
                "description": "Information about our historic grant programme.",
            }, counters)
        return item, counters

    item, counters = asyncio.run(exercise())

    assert item is None
    assert counters["no_readable_active_deadline"] == 1
