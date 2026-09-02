"""The twelve sources added on 2026-09-02, and what is NOT yet proven of them.

Registering a source proves one thing: the platform will now visit that URL.
It proves nothing about whether the URL lists open calls, and this project has
been caught by that distinction before — nine sources in the coverage audit
point at pages listing money already given, and World Bank carried a pagination
template that had been doing nothing for months because a URL that LOADS says
nothing about whether it lists what you want.

Eight of the twelve URLs supplied are HOMEPAGES. A homepage rarely carries the
repeated block of opportunity links the generic parser looks for, so most of
these are expected to need repointing at a funding/opportunities page. That is
what scripts/find_listing_url.py measures, and it has to run somewhere with
egress to these sites — neither this container nor the laptop has it.

So these tests assert registration and hygiene only. They deliberately do NOT
assert that the sources yield rows, because nothing here can know that yet, and
a test that claimed it would be the same false comfort as a coverage percentage
computed as unique/unique.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

CONFIG = Path(__file__).parents[1] / "app" / "scrapers" / "sources.json"

# name -> the domain it must stay on. The URL may be repointed by
# find_listing_url.py; the domain may not, because that would silently turn one
# funder's source into another's.
ADDED = {
    "brainforest_global": "brainforest.global",
    "gef": "thegef.org",
    "cisco_foundation": "cisco.com",
    "green_climate_fund": "greenclimate.fund",
    "rippleworks": "rippleworks.org",
    "drk_foundation": "drkfoundation.org",
    "wfp_innovation": "innovation.wfp.org",
    "ifad_moonshots": "ifad.org",
    "dbs_foundation": "dbs.com",
    "hundredx_impact": "100ximpact.org",
    "agroecology_fund": "agroecology-fund.org",
    "raising_impact": "raisingimpact.org",
}


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
    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                    # noqa: F401

    assert name in SCRAPER_REGISTRY


@pytest.mark.parametrize("name,domain", sorted(ADDED.items()))
def test_the_url_stays_on_the_funder_s_own_domain(name, domain):
    """find_listing_url.py may repoint the URL. It must not repoint it off the
    funder's site — a source named for one funder scraping another's page is
    worse than a source that yields nothing, because the rows look real."""
    entry = by_name()[name]
    for field in ("url", "website"):
        host = (urlparse(entry[field]).hostname or "").lower()
        assert host.endswith(domain), f"{name}.{field} is on {host}, not {domain}"


# --------------------------------------------------------------- hygiene

def test_no_duplicate_source_names():
    names = [s["name"] for s in sources()]
    assert len(names) == len(set(names))


def test_no_two_sources_scrape_the_same_url():
    """The user's instruction was explicit: if a site is already a source, do
    not add it again. This is the check that keeps that true as the list
    grows — the same page under two names double-counts every row it yields."""
    urls = [s["url"].rstrip("/").lower() for s in sources()]
    dupes = {u for u in urls if urls.count(u) > 1}
    assert not dupes, f"the same URL is registered more than once: {dupes}"


def test_none_of_the_twelve_displaced_an_existing_source():
    """71 config sources existed before these were added."""
    assert len(sources()) == 71 + len(ADDED)


@pytest.mark.parametrize("name", sorted(ADDED))
def test_every_entry_has_the_four_fields_the_generic_scraper_needs(name):
    entry = by_name()[name]
    assert set(entry) >= {"name", "display_name", "url", "website"}
    assert entry["display_name"].strip()


# ------------------------------------------------- what is not proven yet

def test_the_homepage_urls_are_recorded_as_needing_discovery():
    """Not a behaviour test — a standing reminder in executable form.

    Eight of the twelve point at a site root. Until find_listing_url.py has run
    against them with egress, "registered" and "scraping open calls" are
    different claims, and only the first is true.
    """
    roots = [n for n, _ in ADDED.items()
             if urlparse(by_name()[n]["url"]).path.strip("/") in ("", "index.html")]
    assert len(roots) >= 7, (
        "if this drops, the URLs were repointed — update the count and the "
        "note above so the file stops overstating what is unverified")
