"""Review of the sources the 2026-11-16 morning report listed as failed."""
from __future__ import annotations

import pytest

from app.scrapers.funders_misc import BlueActionFundScraper, OpenSocietyScraper
from app.services.scrape_outcome import Evidence, Outcome, classify
from app.services.scraper_manager import ScraperManager
from app.services.source_manifest import MANIFESTS, disabled_sources

PAUSED = [
    "triple_funds", "national_institute_of_health_and_care_re",
    "eu_funding_portal_social", "stavros_niarchos_foundation_snf",
    "doen_foundation", "merck_family_foundation", "cjrf",
    "grantmakers_community_project", "veddis_foundation", "gadfly_project",
    "ifad_moonshots",
]


def test_paused_sources_are_real_registered_sources():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "app" / "scrapers" / "sources.json"
    names = {c["name"] for c in json.loads(path.read_text(encoding="utf-8"))}
    assert set(PAUSED) <= names


@pytest.mark.parametrize("key", PAUSED + ["developmentaid"])
def test_each_one_is_held_out_of_scheduled_runs_with_a_reason(key):
    why = disabled_sources([key])[key]
    assert "2026-11-16" in why and len(why) > 40
    assert not MANIFESTS[key].production_enabled


# ------------------------------------------------ the morning report

def test_report_says_why_a_source_failed():
    reason = ScraperManager._problem_reason({
        "status": "failed", "outcome": "blocked",
        "outcome_message": "A page came back, but it is a bot check."})
    assert reason == "blocked by the site. A page came back, but it is a bot check."


def test_report_names_a_stuck_source_and_keeps_long_messages_short():
    assert ScraperManager._problem_reason(
        {"status": "failed", "stalled": True}).startswith("stuck: no page finished")
    long = ScraperManager._problem_reason(
        {"outcome": "parse_zero", "outcome_message": "x" * 600})
    assert long.startswith("the page loaded but nothing was found on it. ")
    assert len(long) < 290


def test_health_report_carries_the_reason():
    m = ScraperManager()
    m.progress = {"undp": {"display_name": "UNDP", "status": "failed",
                           "outcome": "no_fetch",
                           "outcome_message": "No page was retrieved after 2 attempt(s)."}}
    problem = m.health_report(stall_s=1800)["problems"][0]
    assert problem["source"] == "UNDP"
    assert problem["detail"].startswith("the page did not load. No page was retrieved")


# ------------------------------------------------ parsers

_BLUE = (
    "<h3>Calls for proposals</h3><div><h4>Current call for proposals</h4></div>"
    "<div><p>{current}</p></div>"
    "<div><h4>Prior calls for proposals</h4><p>Blue Carbon 2026 "
    "<a href='/wp-content/uploads/2026/06/Call4Proposals_BlueCarbon2026.pdf'>More info</a>"
    "</p></div>"
)


def test_blue_action_with_no_current_call_is_confirmed_empty_not_headings():
    scraper = BlueActionFundScraper()
    page = _BLUE.format(current="There are no upcoming calls for proposals. "
                                "Subscribe to our <a href='/funding-alerts/'>Funding Alerts</a>.")
    assert scraper.parse_listing(page, scraper.start_url) == []
    assert "no upcoming calls" in scraper.last_probe["empty_proof"]
    outcome, _, _ = classify(Evidence(pages_fetched=1, extracted=0, last_http_status=200,
                                      response_bytes=20_000,
                                      empty_proof=scraper.last_probe["empty_proof"]))
    assert outcome is Outcome.CONFIRMED_EMPTY


def test_blue_action_reads_a_current_call_and_never_a_prior_one():
    scraper = BlueActionFundScraper()
    page = _BLUE.format(
        current="Call for Proposals: Blue Carbon in the Western Indian Ocean. "
                "Deadline for concept notes: 15 January 2027. "
                "<a href='/wp-content/uploads/2026/11/Call4Proposals_WIO.pdf'>More info</a>")
    items = scraper.parse_listing(page, scraper.start_url)
    assert [i.opportunity_url for i in items] == [
        "https://www.blueactionfund.org/wp-content/uploads/2026/11/Call4Proposals_WIO.pdf"]
    assert items[0].title.startswith("Call for Proposals: Blue Carbon")
    assert items[0].deadline_raw == "15 January 2027"
    assert scraper.last_probe["empty_proof"] == ""


def test_open_society_with_only_passed_deadlines_is_confirmed_empty():
    scraper = OpenSocietyScraper()
    page = "<a href='/grants/soros-justice-fellowships'>Soros Justice Fellowships DEADLINE: PASSED</a>"
    assert scraper.parse_listing(page, scraper.start_url) == []
    assert scraper.last_probe["all_notices_closed"] is True
    outcome, _, _ = classify(Evidence(pages_fetched=1, extracted=0, last_http_status=200,
                                      response_bytes=20_000, all_notices_closed=True))
    assert outcome is Outcome.CONFIRMED_EMPTY
    open_page = page.replace("PASSED", "January 15, 2027")
    assert len(scraper.parse_listing(open_page, scraper.start_url)) == 1
    assert scraper.last_probe["all_notices_closed"] is False


# ------------------------------------------------ fixes from the EC2 reasons

def test_grand_challenges_reads_the_card_and_its_closing_date():
    from pathlib import Path

    import app.scrapers  # noqa: F401
    from app.scrapers.registry import SCRAPER_REGISTRY

    html = (Path(__file__).parent / "fixtures"
            / "grandchallenges_grant_opportunities.html").read_text(encoding="utf-8")
    items = SCRAPER_REGISTRY["grandchallenges"]().parse_listing(
        html, "https://www.grandchallenges.org/grant-opportunities")
    assert [(i.title, i.deadline_raw, i.opportunity_url) for i in items] == [(
        "Keystone Symposia: Funding Opportunities for 2027 Conferences",
        "Jan 20, 2027", "https://hubs.la/Q04tCtdl0")]


def test_reading_detail_pages_reports_progress_so_a_big_page_is_not_called_stuck():
    """UNDP (526 rows on one page) and Bond (about 500) were ended at exactly
    30 minutes every night: nothing was reported until the whole page was done."""
    import asyncio

    from app.schemas.opportunity import RawOpportunity
    from app.scrapers.base_scraper import BaseScraper

    class Board(BaseScraper):
        name = "board"; display_name = "Board"; website = "https://x.org"
        start_url = "https://x.org/"
        enrich_details = True

        def parse_listing(self, html, page_url):
            return []

        async def parse_detail(self, item, client):
            return item

    events = []

    async def progress(event, payload):
        events.append((event, payload["detail"]))

    rows = [RawOpportunity(title=f"Notice {i}", opportunity_url=f"https://x.org/{i}",
                           source_website="Board") for i in range(45)]
    budget = [1500]
    asyncio.run(Board()._enrich_batch(rows, None, budget, progress))
    assert events == [("heartbeat", "20/45 detail pages read"),
                      ("heartbeat", "40/45 detail pages read"),
                      ("heartbeat", "45/45 detail pages read")]
    assert budget == [1455]


def test_no_http_answer_falls_back_to_the_browser_once(monkeypatch):
    """ISTI and Blue Action Fund: no HTTP answer from EC2, fine in a browser."""
    import asyncio

    import httpx

    from app.core.config import settings
    from app.scrapers import base_scraper as mod
    from app.scrapers.base_scraper import BaseScraper, PageRequest

    class Plain(BaseScraper):
        name = "plain"; display_name = "Plain"; website = "https://x.org"
        start_url = "https://x.org/"

        def parse_listing(self, html, page_url):
            return []

    class Refusing:
        async def get(self, url):
            raise httpx.ConnectError("connection reset")

    rendered = []

    async def fake_render(self, req):
        rendered.append(req.url)
        return "<html>the listing</html>"

    async def no_sleep(_):
        return None

    monkeypatch.setattr(settings, "rate_limit_delay", 0)
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(mod, "_playwright_available", lambda: True)
    monkeypatch.setattr(Plain, "_fetch_rendered", fake_render)
    scraper = Plain()
    html = asyncio.run(scraper._fetch(Refusing(), PageRequest("https://x.org/")))
    assert html == "<html>the listing</html>" and rendered == ["https://x.org/"]
    assert scraper.last_probe["fetch_mode"] == "browser"


def test_an_error_status_is_the_sites_answer_and_is_not_retried_in_a_browser(monkeypatch):
    import asyncio

    import httpx

    from app.core.config import settings
    from app.scrapers import base_scraper as mod
    from app.scrapers.base_scraper import BaseScraper, PageRequest

    class Plain(BaseScraper):
        name = "plain2"; display_name = "Plain"; website = "https://x.org"
        start_url = "https://x.org/"

        def parse_listing(self, html, page_url):
            return []

    class Forbidding:
        async def get(self, url):
            return httpx.Response(403, request=httpx.Request("GET", url))

    async def never(self, req):
        raise AssertionError("browser used after a 403")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(settings, "rate_limit_delay", 0)
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(mod, "_playwright_available", lambda: True)
    monkeypatch.setattr(Plain, "_fetch_rendered", never)
    assert asyncio.run(Plain()._fetch(Forbidding(), PageRequest("https://x.org/"))) is None
