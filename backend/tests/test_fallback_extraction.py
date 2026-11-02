"""A funder page whose layout changed must not become a silent zero.

Why this exists
---------------
The November 2026 source-health report listed 35 sources that fetched a page
and parsed nothing. Some had redesigned, some had moved their calls into a
feed or onto an application portal on another domain, and some were answering
with a bot check. The configured parser cannot know about any of that, so the
generic scraper now tries independent readers (scrapers/fallback_extract.py)
before giving up on page 1.

What these tests pin:
  * each reader finds funding rows in the shape it is meant for, and ignores
    site furniture, social links and old blog posts;
  * a bot-check page is reported as BLOCKED, not as "parser found nothing";
  * a recovered run says so in plain words, so a person updates the config —
    the code never rewrites itself;
  * the monthly report lists recovered sources and sources whose every row the
    filter rejected, because both are where opportunities get missed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.scrapers import fallback_extract as fx
from app.services.scrape_outcome import ErrorCode, Evidence, Outcome, classify
from app.services.source_health_report import build_report


def _scraper():
    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                       # noqa: F401

    s = SCRAPER_REGISTRY["clean_air_fund"]()
    s.last_probe = {}
    return s


class _Resp:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status


class _Client:
    """Stands in for httpx.AsyncClient: a URL -> body map, 404 otherwise."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    async def get(self, url: str) -> _Resp:
        self.asked.append(url)
        if url in self.pages:
            return _Resp(self.pages[url])
        return _Resp("", 404)


@pytest.fixture
def no_browser(monkeypatch):
    import app.scrapers.base_scraper as base
    monkeypatch.setattr(base, "_playwright_available", lambda: False)


# ------------------------------------------------------------- page walls

def test_a_cloudflare_page_is_a_challenge():
    html = "<html><title>Just a moment...</title><script></script>cf_chl_opt</html>"
    assert fx.page_wall(html) == "challenge"


def test_a_javascript_shell_is_recognised():
    html = ("<html><body><div id='root'></div>" + "<script src='a.js'></script>" * 4
            + "</body></html>")
    assert fx.page_wall(html) == "js_shell"


def test_a_normal_page_has_no_wall():
    body = "<p>" + "Our foundation funds community health programmes. " * 20 + "</p>"
    assert fx.page_wall(f"<html><body>{body}<script></script></body></html>") == ""


# ------------------------------------------------------- structured data

LD_PAGE = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Example Foundation", "url": "https://ex.org/"},
  {"@type": "ItemList", "itemListElement": [
    {"@type": "ListItem", "item": {"@type": "Event",
      "name": "Call for Proposals: Clean Air Grants 2027",
      "url": "/calls/clean-air-2027", "endDate": "2027-01-31",
      "description": "Grants of up to USD 50,000 for air-quality projects."}}
  ]}
]}
</script></head><body><div id="app"></div></body></html>"""


def test_structured_data_reads_items_nested_in_a_graph():
    rows = fx.structured_data(LD_PAGE, "https://ex.org/funding/")
    assert rows == [{
        "title": "Call for Proposals: Clean Air Grants 2027",
        "url": "https://ex.org/calls/clean-air-2027",
        "text": "Grants of up to USD 50,000 for air-quality projects.",
        "deadline": "2027-01-31",
    }]


def test_broken_json_ld_is_skipped_not_fatal():
    html = '<script type="application/ld+json">{not json</script>'
    assert fx.structured_data(html, "https://ex.org/") == []


# ------------------------------------------------------ off-site links

OFFSITE_PAGE = """<html><body>
<header><a href="https://portal.example.net/apply">Apply for a grant</a></header>
<main>
  <div><h3>2027 round</h3>
    <a href="https://gms-portal.iucn.org/calls/eba-2027">Global EbA Fund call for proposals 2027</a>
    <p>Deadline 15 March 2027</p></div>
  <a href="https://www.linkedin.com/share?u=grant">Share this grant on LinkedIn</a>
  <a href="https://ex.org/about">About our grant making</a>
</main></body></html>"""


def test_offsite_links_finds_the_external_application_portal():
    rows = fx.offsite_links(OFFSITE_PAGE, "https://ex.org/funding", "https://ex.org")
    urls = [r["url"] for r in rows]
    assert urls == ["https://gms-portal.iucn.org/calls/eba-2027"]
    assert "15 March 2027" in rows[0]["text"]


# ------------------------------------------------------------- feeds

def test_feed_urls_finds_advertised_and_wordpress_feeds():
    html = ('<link rel="alternate" type="application/rss+xml" href="/news/feed.xml">'
            '<link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
            '<img src="/wp-content/uploads/x.png">')
    assert fx.feed_urls(html, "https://ex.org/grants/", "https://ex.org") == [
        "https://ex.org/news/feed.xml", "https://ex.org/feed/"]


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Open call: Youth Innovation Grants 2026</title>
  <link>https://ex.org/2026/youth-grants</link>
  <pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate>
  <description>&lt;p&gt;Applications open until 30 November 2026.&lt;/p&gt;</description></item>
<item><title>Call for proposals 2022</title>
  <link>https://ex.org/2022/old-call</link>
  <pubDate>Tue, 01 Mar 2022 10:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_feed_items_drops_posts_older_than_eighteen_months():
    rows = fx.feed_items(RSS, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert [r["url"] for r in rows] == ["https://ex.org/2026/youth-grants"]
    assert "30 November 2026" in rows[0]["text"]


# ------------------------------------------------- the recovery chain

def test_recovery_uses_structured_data_when_the_layout_is_gone(no_browser):
    s = _scraper()
    rows, strategy = asyncio.run(
        s.recover_empty_page(_Client({}), LD_PAGE, "https://ex.org/funding/"))
    assert strategy == "structured_data"
    assert [r.title for r in rows] == ["Call for Proposals: Clean Air Grants 2027"]
    assert rows[0].deadline_raw == "2027-01-31"


def test_recovery_falls_back_to_the_feed(no_browser):
    s = _scraper()
    page = ('<html><body><img src="/wp-content/a.png">'
            '<p>' + "Welcome to our funding pages. " * 30 + '</p></body></html>')
    client = _Client({s.website.rstrip("/") + "/feed/": RSS})
    rows, strategy = asyncio.run(
        s.recover_empty_page(client, page, "https://ex.org/funding/"))
    assert strategy.startswith("feed (")
    assert [r.opportunity_url for r in rows] == ["https://ex.org/2026/youth-grants"]


def test_recovery_tries_plain_http_when_the_browser_got_a_bot_check():
    s = _scraper()
    s.last_probe["fetch_mode"] = "browser"
    real = ('<html><body><ul><li><a href="/calls/air-quality-fund-2027">'
            'Air Quality Fund 2027 - call for proposals</a> Deadline: 31 January 2027'
            '</li></ul></body></html>')
    url = "https://www.cleanairfund.org/what-we-do/our-grants/"
    client = _Client({url: real})
    rows, strategy = asyncio.run(s.recover_empty_page(
        client, "<title>Just a moment...</title>", url))
    assert s.last_probe["page_wall"] == "challenge"
    assert strategy == "plain_http_fetch"
    assert rows and "Air Quality Fund 2027" in rows[0].title


def test_recovery_returns_nothing_rather_than_furniture(no_browser):
    s = _scraper()
    page = ('<html><body><a href="https://twitter.com/ex">Follow our grants news</a>'
            '<a href="https://ex.org/about">About us</a>'
            '<p>' + "Text about the foundation. " * 30 + '</p></body></html>')
    rows, strategy = asyncio.run(
        s.recover_empty_page(_Client({}), page, "https://ex.org/funding/"))
    assert (rows, strategy) == ([], "")
    assert "structured_data" in s.last_probe["fallbacks_tried"]


def test_crawl_yields_recovered_rows_once_and_records_the_strategy(no_browser,
                                                                   monkeypatch):
    s = _scraper()

    async def fake_fetch(client, req):
        return LD_PAGE

    async def no_enrich(items, client, budget):
        return None

    monkeypatch.setattr(s, "_fetch", fake_fetch)
    monkeypatch.setattr(s, "_enrich_batch", no_enrich)
    events: list[str] = []

    async def progress(kind, data):
        events.append(kind)

    async def run():
        stop, pause = asyncio.Event(), asyncio.Event()
        pause.set()
        return [batch async for batch in s.crawl(stop, pause, progress)]

    batches = asyncio.run(run())
    assert len(batches) == 1 and len(batches[0]) == 1
    assert s.last_probe["extraction_strategy"] == "structured_data"
    assert events[-1] == "pages_end"


# --------------------------------------------------- outcome wording

def test_a_bot_check_on_a_fetched_page_is_blocked_not_parse_zero():
    outcome, code, msg = classify(Evidence(pages_fetched=1, extracted=0,
                                           last_http_status=200,
                                           response_bytes=28_900,
                                           page_wall="challenge"))
    assert outcome is Outcome.BLOCKED
    assert code is ErrorCode.CHALLENGE


def test_parse_zero_says_the_alternatives_were_tried_too():
    _, _, msg = classify(Evidence(pages_fetched=1, extracted=0,
                                  last_http_status=200, response_bytes=1_900,
                                  page_wall="js_shell"))
    assert "no alternative reader" in msg
    assert "JavaScript shell" in msg


def test_a_recovered_run_asks_for_a_config_update():
    outcome, _, msg = classify(Evidence(pages_fetched=1, extracted=4, saved=2,
                                        last_http_status=200,
                                        extraction_strategy="feed (https://ex.org/feed/)"))
    assert outcome is Outcome.SUCCESS_WITH_RESULTS
    assert "Recovered by fallback" in msg and "update this source's config" in msg


def test_no_new_rows_names_rejections_separately_from_duplicates():
    _, _, msg = classify(Evidence(pages_fetched=1, extracted=19, saved=0,
                                  duplicates=0, rejected=19,
                                  last_http_status=200))
    assert "19 extracted, none new: 0 already stored, 19 rejected" in msg


# ------------------------------------------------------ monthly report

def _entry(name, outcome, state="ok", msg=""):
    return SimpleNamespace(display_name=name, last_outcome=outcome, state=state,
                           last_http_status=200, last_error_message=msg, note="")


def test_report_lists_recovered_and_fully_rejected_sources():
    body = build_report([
        _entry("Healed", "success_with_results",
               msg="3 new of 3 extracted. Recovered by fallback (feed (x)): …"),
        _entry("All rejected", "success_no_new",
               msg="19 extracted, none new: 0 already stored, 19 rejected by the "
                   "opportunity filter, 0 already closed."),
        _entry("Just duplicates", "success_no_new",
               msg="5 extracted, none new: 5 already stored, 0 rejected by the "
                   "opportunity filter, 0 already closed."),
    ], generated_at=datetime(2026, 11, 1, 9, 30))
    assert "RECOVERED BY FALLBACK — CONFIG UPDATE NEEDED: 1" in body
    assert "EVERY ROW REJECTED BY THE FILTER — CHECK FOR MISSED OPPORTUNITIES: 1" in body
    assert "Healed" in body and "All rejected" in body


def test_the_run_message_says_why_rows_were_rejected():
    _, _, msg = classify(Evidence(pages_fetched=1, extracted=19, saved=0,
                                  rejected=19, last_http_status=200,
                                  reject_reasons="no opportunity signal 12, "
                                                 "no usable link 7"))
    assert "Rejected because: no opportunity signal 12, no usable link 7." in msg


def test_the_manager_tallies_rejection_reasons_per_source():
    from app.services.scraper_manager import ScraperManager

    m = ScraperManager.__new__(ScraperManager)
    for _ in range(3):
        m._note_rejection("isti_individual_grants", "no opportunity signal")
    m._note_rejection("isti_individual_grants", "no usable link")
    m._note_rejection("other", "furniture")
    assert m._rejection_reasons("isti_individual_grants") == \
        "no opportunity signal 3, no usable link 1"
    assert m._rejection_reasons("never_ran") == ""
