"""Sources re-read in a real browser on 2026-11-07 after the health report.

Each test drives the REGISTERED scraper (built from sources.json) against a
fixture whose structure was copied from the live page, so a config that stops
matching the site fails here instead of going quiet in production.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.scrapers  # noqa: F401 — registers every scraper
from app.scrapers.registry import SCRAPER_REGISTRY
from app.services.scrape_outcome import Evidence, Outcome, classify

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(name: str, fixture: str, url: str):
    return SCRAPER_REGISTRY[name]().parse_listing(
        (FIXTURES / fixture).read_text(encoding="utf-8"), url)


def test_globaltenders_reads_titles_and_deadlines_from_view_detail_rows():
    items = _parse("globaltenders", "globaltenders_india_consultancy.html",
                   "https://www.globaltenders.com/india-consultancy-tenders")
    assert [i.title for i in items] == [
        "Digitization And Geo Referencing Of Existing Land Use Maps And Development Control Plans",
        "Consultancy Services For Preparation Of Detailed Project Report For Water Quality Monitoring",
    ]
    assert items[0].deadline_raw.startswith("2026-12-11") or items[0].deadline_raw == "11 Dec 2026"
    assert items[0].opportunity_url.endswith("UeSvvehlh33oSYx6")


def test_pfizer_keeps_rfps_hosted_on_its_cdn():
    items = _parse("pfizer_foundation", "pfizer_competitive_grants_row.html",
                   "https://www.pfizer.com/about/programs-policies/grants/competitive-grants")
    titles = [i.title for i in items]
    assert titles[0].startswith("Real World Evidence on the Impact")
    assert items[0].opportunity_url.startswith("https://cdn.pfizer.com/")
    assert items[0].deadline_raw.startswith("2026-11-25")
    # "View Awarded Grants" is not a call
    assert not any("Review%20Summary" in i.opportunity_url for i in items)


def test_gulbenkian_cards_give_title_and_closing_date():
    items = _parse("gulbenkian", "gulbenkian_bolsas_lista.html",
                   "https://gulbenkian.pt/en/bolsas-lista/")
    first = items[0]
    assert first.title == "Short Term Conference and Travel Scholarships"
    assert first.deadline_raw == "04 Dec 2026"


def test_agency_fund_calls_on_the_webflow_cdn_are_kept_and_social_links_are_not():
    items = _parse("the_agency_fund", "agency_fund_past_calls.html",
                   "https://www.agency.fund/past-calls")
    urls = [i.opportunity_url for i in items]
    assert any("Spring%202026%20TAF%20Open%20Call" in u for u in urls)
    assert not any("linkedin" in u for u in urls)


def test_wordpress_uploaded_call_pdfs_are_not_treated_as_navigation():
    from app.scrapers.generic_listing import _NAV_HREF

    assert not _NAV_HREF.search("https://erefdn.org/wp-content/uploads/2024/11/EREF-RFP-2024.pdf")
    assert _NAV_HREF.search("https://example.org/wp-admin/")
    assert _NAV_HREF.search("https://example.org/wp-content/uploads/logo.png")


# --------------------------------------------------------- King Baudouin

def test_kbs_reads_open_calls_from_the_search_api():
    scraper = SCRAPER_REGISTRY["kbs_frb"]()
    items = scraper.parse_listing((FIXTURES / "kbs_open_calls.json").read_text(encoding="utf-8"),
                                  scraper.start_url)
    assert [i.title for i in items][:2] == [
        "Applied research focusing on Health and Environment Interactions - Orcadia Fund",
        "2027 Prince Albert Fund"]
    assert items[0].deadline_raw == "2027-01-07"
    assert items[0].opportunity_url.startswith("https://kbs-frb.be/en/")
    assert scraper.next_page("", scraper.start_url, 1) is None      # 1 page of 8


def test_kbs_pages_until_the_api_says_stop():
    scraper = SCRAPER_REGISTRY["kbs_frb"]()
    data = json.loads((FIXTURES / "kbs_open_calls.json").read_text(encoding="utf-8"))
    data["info"]["page"]["num_pages"] = 2
    scraper.parse_listing(json.dumps(data), scraper.start_url)
    nxt = scraper.next_page("", scraper.start_url, 1)
    assert nxt is not None and "page=2" in nxt.url


def test_kbs_is_the_bespoke_scraper_not_the_generic_one():
    assert SCRAPER_REGISTRY["kbs_frb"].__module__ == "app.scrapers.kbs_frb"


# ---------------------------------------------------------- outcomes

@pytest.mark.parametrize("text", [
    "Apply filters No calls for projects found See previous calls for projects",
    "Aucun appels à projets trouvé Voir les précédents appels à projets",
    "Note: We are currently not accepting applications.",
    "Thank you for your interest. Our 2026 application has closed.",
    "There are no open RFPs at this time.",
])
def test_empty_wordings_are_confirmed_empty(text):
    outcome, _, _ = classify(Evidence(pages_fetched=1, extracted=0, last_http_status=200,
                                      response_bytes=20_000, body_sample=text))
    assert outcome is Outcome.CONFIRMED_EMPTY


def test_a_human_verification_page_is_blocked():
    from app.scrapers.fallback_extract import page_wall

    assert page_wall("<title>Let's confirm you are human</title>Complete the security check") == "challenge"
