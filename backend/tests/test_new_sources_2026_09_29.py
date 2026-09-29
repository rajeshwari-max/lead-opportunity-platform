"""The fifteen sources added on 2026-09-29, and what is known about each.

The owner supplied 19 distinct URLs. Fifteen were added; four were not, and
the reasons are recorded in NOT_ADDED below rather than lost in a chat log,
because the useful question later is "why isn't CSRBox in here" and the
answer has to survive.

What these tests DO assert: the entries are registered, hygienic, and on the
funder's own domain, and that four specific verdicts have not been quietly
reversed.

What they deliberately do NOT assert: that any of these yields rows. Nine
sources were removed from this project in September precisely because
registering a URL had been mistaken for coverage. Four of the fifteen could
not be read at all by the audit (robots.txt timeouts, 403 bot walls, an
https->http redirect loop) and are marked UNVERIFIED in sources.json; only
scripts/verify_source.py, run somewhere with real egress, can settle them.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

CONFIG = Path(__file__).parents[1] / "app" / "scrapers" / "sources.json"

# name -> the domain it must stay on. verify_source / find_listing_url may
# repoint a URL; neither may move it to another funder's site.
ADDED = {
    "wellcome_contracts": "wellcome.org",
    "grandchallenges": "grandchallenges.org",
    "isti_institutional_grants": "indiascienceandtechnology.gov.in",
    "isti_international_grants": "indiascienceandtechnology.gov.in",
    "isti_individual_grants": "indiascienceandtechnology.gov.in",
    "isti_conference_grants": "indiascienceandtechnology.gov.in",
    "fire_biofin": "biofin.org",
    "eu_funding_portal_social": "eufundingportal.eu",
    "globaleba_fund": "globalebafund.org",
    "developmentwala_rfps": "developmentwala.org",
    "devinfo_rfps": "devinfo.in",
    "investindia_rfp": "investindia.gov.in",
    "leverforchange": "leverforchange.org",
    "samsstc_rfp": "samsstc.com",
    "ai_opportunity_fund_apac": "withgoogle.com",
}

# Supplied by the owner in the same list, and left out on purpose.
NOT_ADDED = {
    "indiantenders.in":
        "Subscription site. The tenders readable without paying are flat and "
        "land sale notices, not development funding, and unlimited access is "
        "sold from Rs 2500 — scraping past that is what the site's terms "
        "restrict.",
    "guidestarindia.org":
        "A directory of NGOs, not a funding board. Nothing on it lists calls.",
    "csrbox.org":
        "Answered 403 to every audit request, and the only RFP page findable "
        "from outside is an index of RFP/EOI news from 2019. No current "
        "listing URL could be established, and pointing a source at a "
        "homepage is the exact defect that got nine sources deleted on "
        "2026-09-15.",
}

# Sources that the audit could NOT read. They are in sources.json with an
# UNVERIFIED note; this list is what makes that promise checkable.
UNVERIFIED = {"wellcome_contracts", "eu_funding_portal_social",
              "developmentwala_rfps", "investindia_rfp"}


def sources() -> list[dict]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def by_name() -> dict[str, dict]:
    return {s["name"]: s for s in sources()}


# ------------------------------------------------------------- registered

@pytest.mark.parametrize("name", sorted(ADDED))
def test_the_source_is_registered(name):
    assert name in by_name()


@pytest.mark.parametrize("name", sorted(ADDED))
def test_the_source_builds_a_scraper(name):
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import SCRAPER_REGISTRY

    assert name in SCRAPER_REGISTRY


@pytest.mark.parametrize("name,domain", sorted(ADDED.items()))
def test_the_url_stays_on_the_funder_s_own_domain(name, domain):
    entry = by_name()[name]
    for field in ("url", "website"):
        host = (urlparse(entry[field]).hostname or "").lower()
        assert host == domain or host.endswith("." + domain), \
            f"{name}.{field} is on {host}, not {domain}"


@pytest.mark.parametrize("name", sorted(ADDED))
def test_every_entry_has_the_fields_the_generic_scraper_needs(name):
    entry = by_name()[name]
    assert set(entry) >= {"name", "display_name", "url", "website"}
    assert entry["display_name"].strip()


# ----------------------------------------------------------- what is known

@pytest.mark.parametrize("name", sorted(UNVERIFIED))
def test_an_unreadable_source_says_so_in_the_file(name):
    """Four of these could not be fetched by the audit at all. The entry has
    to carry that, or the next person reads a registered source as a checked
    one — which is how nine homepages lived here for a month."""
    assert "UNVERIFIED" in by_name()[name].get("_note", ""), name


def test_the_verified_ones_do_not_claim_to_be_unverified():
    for name in set(ADDED) - UNVERIFIED:
        assert "UNVERIFIED" not in by_name()[name].get("_note", ""), name


def test_the_page_that_is_not_a_listing_says_so():
    """aiopportunityfund is a single programme page: the grantees are already
    chosen and there is no call to read. It was added because it was asked
    for, and the entry must not imply more than that."""
    assert "NOT A LISTING" in by_name()["ai_opportunity_fund_apac"]["_note"]


@pytest.mark.parametrize("name", sorted(n for n in ADDED if n.startswith("isti_")))
def test_the_isti_sources_paginate_zero_based(name):
    """Drupal's ?page=0 is the FIRST page. {page} would start the walk at the
    third page and silently skip the second — the World Bank defect, which
    cost that source nine of every ten rows."""
    entry = by_name()[name]
    assert entry["page_url"].endswith("?page={offset}")
    assert entry["page_size"] == 1


# --------------------------------------------------------------- hygiene

def test_no_duplicate_source_names():
    names = [s["name"] for s in sources()]
    assert len(names) == len(set(names))


def test_no_two_sources_scrape_the_same_url():
    urls = [s["url"].rstrip("/").lower() for s in sources()]
    dupes = {u for u in urls if urls.count(u) > 1}
    assert not dupes, f"the same URL is registered more than once: {dupes}"


def test_the_second_wellcome_and_grand_challenges_entries_are_deliberate():
    """Both funders were already present at a DIFFERENT path. The rule the
    owner set is "don't add a site twice"; these are two sections of one site
    that list different things, so the entry has to explain itself."""
    for name, other in (("wellcome_contracts", "wellcome_trust"),
                        ("grandchallenges", "bmgf_grand_challenges")):
        entry, existing = by_name()[name], by_name()[other]
        assert entry["url"] != existing["url"]
        assert other in entry["_note"], \
            f"{name} must name the source it overlaps, so the overlap is on purpose"


def test_none_of_the_new_sources_is_a_bare_homepage_except_the_one_that_is():
    """fire.biofin.org IS its own listing — the database is the front page.
    Everything else must name a path, because a homepage carries no repeated
    block of opportunity links for the parser to find."""
    for name in ADDED:
        path = urlparse(by_name()[name]["url"]).path.strip("/")
        if name == "fire_biofin":
            assert path == ""
            continue
        assert path not in ("", "index.html"), f"{name} points at a site root"


# ------------------------------------------------- and the ones left out

@pytest.mark.parametrize("domain", sorted(NOT_ADDED))
def test_the_rejected_urls_really_are_absent(domain):
    """Keeps a decision from being undone by accident. Re-adding one of these
    means deleting its line from NOT_ADDED and saying why."""
    hosts = {(urlparse(s["url"]).hostname or "").lower() for s in sources()}
    assert not any(h == domain or h.endswith("." + domain) for h in hosts), \
        f"{domain} was excluded on 2026-09-29: {NOT_ADDED[domain]}"
