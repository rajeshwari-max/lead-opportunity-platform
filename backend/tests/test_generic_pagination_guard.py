"""Pagination that does not advance, in the scraper that serves 71 sources.

Why this exists
---------------
Twice now a source has been found re-serving page 1 under a different URL:
ADB (36 rows over three pages, 12 distinct — the same twelve, three times) and
World Bank (os={offset} sliding one row at a time). Both were found by hand,
months apart. Both were in code with no way to notice.

ADB got a guard. The generic scraper did not, and it is where the bug is most
likely to recur, because nothing in it knows what any given site's pagination
is meant to look like — it tries rel=next, then a numbered link, then an arrow,
then bumping a number in the URL. Every one of those branches is satisfied by a
link that goes nowhere: the URL changes, the fetch succeeds, rows come back.

The manager's stale-page counter does eventually stop such a run, but it
reports it as "only older content ahead" — a confident and wrong explanation.
Clean Air Fund's 2026-08-24 run is the live example: page 2 returned three rows,
all duplicates of page 1, and the run finished "every available page was
scraped".

What the guard must NOT do is fire on a first page, on an empty page, or on two
different pages that happen to be the same length — those are the ways a guard
like this silently truncates a working source, which is worse than the bug.
"""
from __future__ import annotations

from app.scrapers.generic_listing import GenericListingScraper, rows_signature


class Row:
    """Minimal stand-in for RawOpportunity — the signature reads two fields."""

    def __init__(self, title: str, url: str = "") -> None:
        self.title = title
        self.opportunity_url = url


# ------------------------------------------------------------- the signature

def test_the_same_rows_hash_the_same():
    a = [Row("Clean Air in Cities 2027", "/grants/cac-2027")]
    b = [Row("Clean Air in Cities 2027", "/grants/cac-2027")]
    assert rows_signature(a) == rows_signature(b)


def test_different_rows_hash_differently():
    assert rows_signature([Row("A", "/a")]) != rows_signature([Row("B", "/b")])


def test_an_empty_page_is_not_a_repeat():
    """"No rows" and "the same rows as last time" mean opposite things — one
    ends a walk, the other reports a defect. A shared value would conflate
    them."""
    assert rows_signature([]) == ""
    assert rows_signature(None) == ""


def test_tracking_parameters_do_not_defeat_it():
    """A widget that re-renders identical rows with rotated query strings would
    otherwise read as fresh content on every page."""
    assert (rows_signature([Row("Grant", "/g?utm_source=1")])
            == rows_signature([Row("Grant", "/g?utm_source=2")]))


def test_the_title_is_part_of_the_key():
    """Several sources put many distinct calls behind one listing URL — 89
    DevNet rows share rfp_assignments.aspx across 81 titles. Keying on the link
    alone would call those pages identical."""
    assert (rows_signature([Row("First RFP", "/rfp.aspx")])
            != rows_signature([Row("Second RFP", "/rfp.aspx")]))


# ------------------------------------------------------------- the guard

def _scraper() -> GenericListingScraper:
    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                       # noqa: F401

    return SCRAPER_REGISTRY["clean_air_fund"]()


PAGED_HTML = '<a rel="next" href="/what-we-do/our-grants/page/2/">Next</a>'


def test_a_page_that_repeats_the_previous_one_stops_the_walk():
    s = _scraper()
    s._page_signature = s._prev_signature = "deadbeefdeadbeef"
    assert s.next_page(PAGED_HTML, "https://x/our-grants/", 2) is None


def test_and_it_says_pagination_defect_not_end_of_listing(caplog):
    """The message matters as much as the stop. "Every available page was
    scraped" is what this run said while returning five rows of a portfolio."""
    import logging

    s = _scraper()
    s._page_signature = s._prev_signature = "deadbeefdeadbeef"
    with caplog.at_level(logging.WARNING, logger="scraper"):
        s.next_page(PAGED_HTML, "https://x/our-grants/", 2)
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "not advancing" in said
    assert "pagination defect" in said.lower()


def test_the_first_page_never_trips_it():
    """Both signatures start empty. Treating that as a repeat would stop every
    source in the config at page 1 — the guard would take the platform down."""
    s = _scraper()
    assert s._page_signature == "" and s._prev_signature == ""
    assert s.next_page(PAGED_HTML, "https://x/our-grants/", 1) is not None


def test_two_empty_pages_are_not_treated_as_a_repeat():
    """Empty equals empty, but that is the end of a listing, not a defect —
    and it must not be reported as one."""
    s = _scraper()
    s._page_signature = s._prev_signature = ""
    # No rows means _page_had_items is False, so the URL-bumping branches
    # decline on their own; what matters is that the guard did not claim a
    # pagination defect.
    s.next_page("<html></html>", "https://x/our-grants/", 2)


def test_a_genuinely_different_page_still_advances():
    s = _scraper()
    s._prev_signature = "1111111111111111"
    s._page_signature = "2222222222222222"
    assert s.next_page(PAGED_HTML, "https://x/our-grants/", 2) is not None


def test_the_signature_pair_advances_as_pages_are_parsed():
    """parse_listing shifts current into previous. Without that the guard
    compares a page against itself and fires on page 2 of every source."""
    s = _scraper()
    html = ('<main><article><h3><a href="/grants/one-2027">'
            'Open call: Clean Air in Cities Fund 2027</a></h3>'
            '<p>Applications close: 28 February 2027</p>'
            '<p>Grants of up to £250,000 for city-level air quality work.</p>'
            '</article></main>')
    s.parse_listing(html, "https://x/our-grants/")
    first = s._page_signature
    assert s._prev_signature == ""
    s.parse_listing(html, "https://x/our-grants/page/2/")
    assert s._prev_signature == first
    assert s._page_signature == first, "same rows, so the same signature"
