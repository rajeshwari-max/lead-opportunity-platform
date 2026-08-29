"""Page furniture must not be stored as an opportunity.

Every string in the first test is a REAL title from the 2026-08-29 database,
with the row count it had. They survived because `FURNITURE_TITLES` is an
exact-match English set, and an exact-match set can only catch furniture it has
already met.

The second test is the one that matters more. Every pattern added to catch the
first list is a chance to delete a real opportunity, so the real titles below —
also taken from the live database — must all survive.
"""
from __future__ import annotations

import pytest

from app.services.links import is_furniture


# Real rows, with the count each had in the database on 2026-08-29.
JUNK_IN_THE_DATABASE = [
    ("Overslaan en naar inhoud gaan", 53),          # nl: skip to main content
    ('Search results for: "grants" Clear Search', 20),
    ("(E: 404) Content Not Found", 15),
    ("Increase Font Size", 5),
    ("Browse by Focus Area", 5),
]


@pytest.mark.parametrize("title,count", JUNK_IN_THE_DATABASE,
                         ids=[t for t, _ in JUNK_IN_THE_DATABASE])
def test_real_junk_rows_are_rejected(title, count):
    assert is_furniture(title), (
        f"{count} rows titled {title!r} are in the live database as opportunities"
    )


# Real opportunity titles from the same database. If a pattern added above ever
# starts matching one of these, it is deleting leads.
REAL_OPPORTUNITIES = [
    "RFQ - Procurement and Distribution of Nutritional Basket",
    "RFP-Deployment of Soybean Grain Analyser App",
    "RFP-Engagement of a GIS Agency / Consultant",
    "Ninth Call 2026 Country-led Projects",
    "Building the technology and tools to eliminate child sexual abuse",
    "Call for Proposals: New €2m Building Narrative Power funding programme",
    "Request for Proposals: Innovative Advocacy Seed Grant (India)",
    "Full ADOPT Grant: Round 9",
    "Spring Impact Scale Accelerator",
    "Community and Civil Society Engagement (CCSE) Pilot for Lenacapavir",
    # Adversarial: each contains a word one of the new patterns keys on.
    "Increasing Font Accessibility in Rural Schools",
    "Search and Rescue Equipment Supply Tender",
    "Browse Africa: Digital Literacy Programme",
    "Error Correction Coding Research Fellowship",
    "Page Turners: A Literacy Grant for Community Libraries",
    "Clear Water Initiative — Call for Implementation Partners",
    "No Results Left Behind: Evaluation Capacity Grant",
    "Show More Women in STEM — Innovation Challenge",
]


@pytest.mark.parametrize("title", REAL_OPPORTUNITIES, ids=REAL_OPPORTUNITIES)
def test_real_opportunities_survive(title):
    assert not is_furniture(title), (
        f"{title!r} was rejected as furniture — a pattern is too broad and is "
        f"deleting real leads"
    )


# ------------------------------------------------------- the classes, not the
# ------------------------------------------------------- specific strings

@pytest.mark.parametrize("title", [
    "404 Page Not Found",
    "(E: 500) Internal Server Error",
    "Page not found",
    "Content Not Found",
    "Oops! Something went wrong",
    "Access Denied",
])
def test_error_pages(title):
    assert is_furniture(title)


@pytest.mark.parametrize("title", [
    "Search Results for: water",
    "Showing 1-20 of 340",
    "24 results found",
    "No results found",
    "Filter by Country Clear filters",
])
def test_search_and_result_headers(title):
    assert is_furniture(title)


@pytest.mark.parametrize("title", [
    "Increase Font Size", "Decrease text size", "Reset Font Size",
    "High Contrast", "Dark mode", "Font Size",
])
def test_accessibility_controls(title):
    assert is_furniture(title)


@pytest.mark.parametrize("title", [
    "Browse by Focus Area", "Filter by Region", "Sort by Deadline",
    "Explore by Theme", "Load more", "Show More", "Page 3 of 12",
])
def test_navigation_and_faceting(title):
    assert is_furniture(title)


@pytest.mark.parametrize("title", [
    "Overslaan en naar inhoud gaan",            # nl
    "Aller au contenu principal",               # fr
    "Zum Hauptinhalt springen",                 # de
    "Saltar al contenido principal",            # es
    "Salta al contenuto principale",            # it
])
def test_skip_links_in_the_languages_these_sources_publish_in(title):
    """Scoped honestly: the languages present in the current source list, not a
    claim to handle every language. A blocklist cannot be complete."""
    assert is_furniture(title)


@pytest.mark.parametrize("title", [
    "Accept all cookies", "Manage Cookies", "Cookie Settings",
])
def test_consent_banners(title):
    assert is_furniture(title)


def test_matching_is_anchored_not_substring():
    """The existing contract: a real call that merely CONTAINS a furniture word
    is untouched. Stated as a test so a future pattern cannot quietly break it.
    """
    assert not is_furniture("Apply now for the 2026 Water Fund")
    assert not is_furniture("Home Gardens for Nutrition — Small Grants")
    assert not is_furniture("News Media Literacy Fund: Call for Proposals")
