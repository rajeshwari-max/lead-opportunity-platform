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


# ================================================================ pagination
# Measured on 2026-09-30 by reading each listing's own pager and stated total.
# Each entry: (the total the site states, per page, how page N+1 is addressed).
# None for per_page/dialect means the listing has NO pagination at all —
# pinned so that nobody "fixes" it by bolting on a template that invents pages.

MEASURED = {
    #  name                        total  per_page  second page URL (or None)
    "samsstc_rfp":               (31,    30,  "https://www.samsstc.com/rfp-tender/rfp-list?page=2"),
    "leverforchange":            (19,    12,  "https://leverforchange.org/open-calls/page/2/"),
    "isti_institutional_grants": (19,    10,  "https://www.indiascienceandtechnology.gov.in/funding-opportunities/research-grants/institutional?page=1"),
    "isti_international_grants": (11,    10,  "https://www.indiascienceandtechnology.gov.in/funding-opportunities/research-grants/international?page=1"),
    "isti_individual_grants":    (20,    10,  "https://www.indiascienceandtechnology.gov.in/funding-opportunities/research-grants/individual?page=1"),
    "isti_conference_grants":    (7,     10,  "https://www.indiascienceandtechnology.gov.in/funding-opportunities/grants-for-conference-seminars?page=1"),
    "devinfo_rfps":              (30,    None, None),
    "grandchallenges":           (1,     None, None),
    "globaleba_fund":            (1,     None, None),
}


def scraper(name):
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import SCRAPER_REGISTRY

    s = SCRAPER_REGISTRY[name]()
    # State the generic next_page reads: the page we just parsed had rows, and
    # it was not a repeat of the one before.
    s._page_had_items = True
    s._page_signature, s._prev_signature = "page-1-rows", ""
    return s


@pytest.mark.parametrize("name", sorted(n for n, m in MEASURED.items() if m[2]))
def test_the_second_page_is_the_one_the_site_itself_links_to(name):
    """The whole point. Page 1 -> the URL the site's own pager uses for page 2.

    For ISTI that is ?page=1, not ?page=2 — Drupal counts from zero, and
    asking for ?page=2 would skip the second page and read the third.
    """
    s = scraper(name)
    nxt = s.next_page("<html></html>", s.start_url, 1)
    assert nxt is not None, f"{name} stopped after page 1"
    assert nxt.url == MEASURED[name][2]


@pytest.mark.parametrize("name", sorted(n for n, m in MEASURED.items() if not m[2]))
def test_a_listing_with_no_pager_is_not_given_one(name):
    """devinfo lists 30 RFPs on one page and has no older-posts control;
    Grand Challenges and the EbA Fund show one call at a time. A template here
    would make the crawler request pages that do not exist."""
    assert not by_name()[name].get("page_url")
    s = scraper(name)
    assert s.next_page("<html><body><p>no pager</p></body></html>", s.start_url, 1) is None


@pytest.mark.parametrize("name", sorted(n for n, m in MEASURED.items() if m[1]))
def test_enough_pages_are_walked_to_reach_the_stated_total(name):
    """The crawl only ends on an empty or repeated page, so it reaches every
    row as long as the per-page and total figures here are right. This keeps
    the arithmetic visible: a site that grows past the safety cap shows up."""
    from app.core.config import settings

    total, per_page, _ = MEASURED[name]
    pages_needed = -(-total // per_page)            # ceiling division
    assert pages_needed + 1 <= settings.max_pages_safety_cap, \
        f"{name} needs {pages_needed} pages plus the empty one that ends the walk"


def test_samsstc_reads_the_full_list_not_the_landing_page():
    """/rfp-tender is a landing page with a 'View all 31 →' link. The source
    was first registered there; the full list is /rfp-tender/rfp-list."""
    assert by_name()["samsstc_rfp"]["url"].endswith("/rfp-tender/rfp-list")


@pytest.mark.parametrize("name", sorted(MEASURED))
def test_server_rendered_listings_are_fetched_without_a_browser(name):
    """Each of these was read without running JavaScript. The generic default
    renders every source in Chromium when it can; for these that is pure cost,
    and on a host without Playwright it logs a warning on every run."""
    assert by_name()[name].get("requires_js") is False
    assert scraper(name).prefer_js is False


@pytest.mark.parametrize("name", sorted(UNVERIFIED | {"fire_biofin", "ai_opportunity_fund_apac"}))
def test_the_unmeasured_ones_keep_the_browser(name):
    """Nothing is known about how these render, so they keep the default."""
    assert "requires_js" not in by_name()[name]
