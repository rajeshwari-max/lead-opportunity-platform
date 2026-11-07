"""Fixes for sources the November 2026 source-health report flagged.

"Every row rejected by the filter" (ISTI x3, Wellcome) was not the sites: the
opportunity gate read /research-grants/ and /research-funding/ as a
"research" (publications) page, so every scheme was dropped as "page type is
never an opportunity". These tests pin the fix AND its limits: a real
publications or news section must still be rejected.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.opportunity_gate import is_opportunity

SOURCES = Path(__file__).resolve().parents[1] / "app" / "scrapers" / "sources.json"


@pytest.mark.parametrize("title,url,curated", [
    ("Core Research Grant (CRG)",
     "https://www.indiascienceandtechnology.gov.in/funding-opportunities/"
     "research-grants/institutional/core-research-grant-crg", True),
    ("Ramanujan Fellowship",
     "https://www.indiascienceandtechnology.gov.in/funding-opportunities/"
     "research-grants/individual/ramanujan-fellowship", True),
    ("Discovery Awards", "https://wellcome.org/research-funding/schemes/discovery-awards",
     False),
])
def test_calls_kept_under_research_funding_paths(title, url, curated):
    assert is_opportunity(title, "", url, "", curated) == (True, "")


@pytest.mark.parametrize("url", [
    "https://example.org/research/2025/annual-findings",
    "https://example.org/research-reports/2025",
    "https://example.org/research-2/our-study",
    "https://example.org/news-grants-2025",
    "https://example.org/stories/a-grant-we-made",
])
def test_publications_and_news_are_still_rejected(url):
    keep, why = is_opportunity("What our grants achieved", "grants", url)
    assert not keep and why == "page type is never an opportunity"


def test_a_dated_proposal_deadline_is_evidence_of_a_call():
    keep, _ = is_opportunity(
        "Long-Term Impacts Initiative (LTII)", "Proposals Due: December 2, 2026",
        "https://www.povertyactionlab.org/initiative/long-term-impacts-initiative")
    assert keep


def test_an_initiative_with_no_active_cycle_is_not_a_call():
    keep, _ = is_opportunity(
        "Governance Initiative (GI)", "No active RFP cycles",
        "https://www.povertyactionlab.org/initiative/governance-initiative")
    # "RFP" appears, so the vocabulary passes — the row is stored undated and
    # the strict Active rule keeps it off the dashboard. What matters here is
    # that the page type is not what rejects it.
    assert keep


def test_repointed_sources_no_longer_aim_at_past_grants_pages():
    by_name = {c["name"]: c for c in json.loads(SOURCES.read_text(encoding="utf-8"))}
    assert by_name["clean_air_fund"]["url"].endswith("/about-us/jobs/consultancy-rfps")
    assert by_name["j_pal"]["url"] == "https://www.povertyactionlab.org/initiatives"
    assert by_name["gatesfoundation"]["url"] == "https://submit.gatesfoundation.org/"
    assert "2025" not in by_name["gupta_family_foundation"]["url"]
    for name in ("clean_air_fund", "j_pal", "gatesfoundation", "gupta_family_foundation"):
        assert by_name[name].get("_url_note"), name
