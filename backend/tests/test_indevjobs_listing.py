"""IndevJobs /funding: the rows were rendered, and the parser skipped them.

The capture in fixtures/indevjobs_funding_listing.html is the page exactly as
the scraper's own browser rendered it on 2026-09-07 (data/debug/). It shows
two open calls. The old parser returned zero, because detail links moved from
/funding/<slug> to /funding-opportunity/<slug>.
"""
from __future__ import annotations

from pathlib import Path

import app.scrapers  # noqa: F401
from app.scrapers.indevjobs import IndevJobsScraper

HTML = (Path(__file__).parent / "fixtures" / "indevjobs_funding_listing.html").read_text(encoding="utf-8")


def rows():
    s = IndevJobsScraper()
    return s.parse_listing(HTML, s.start_url)


def test_both_rendered_calls_are_read():
    assert [r.title for r in rows()] == [
        "Air Charter Services for long term contracts (Fixed-Wing and Rotary-Wing aircraft)",
        "NOFO Innovative Health Practices to Improved Health Outcomes",
    ]


def test_the_count_matches_what_the_page_says_it_has():
    """The page prints "2 Total Available". A parser that reads fewer than the
    page's own counter is wrong, whatever the reason."""
    import re

    from bs4 import BeautifulSoup
    text = BeautifulSoup(HTML, "lxml").get_text(" ", strip=True)
    stated = int(re.search(r"(\d+)\s+Total Available", text).group(1))
    assert len(rows()) == stated == 2


def test_deadlines_and_organisations():
    got = [(r.deadline_raw, r.organization) for r in rows()]
    assert got == [("Jan 11, 2027", "united nations global marketplace (UNGM)"),
                   ("Feb 20, 2028", "USAID")]


def test_view_details_and_avatar_links_do_not_become_rows():
    assert not any(r.title.lower().startswith(("view details", "u")) and len(r.title) < 15
                   for r in rows())
    assert len({r.opportunity_url for r in rows()}) == len(rows())


def test_links_point_at_the_detail_page():
    assert all("/funding-opportunity/" in r.opportunity_url for r in rows())


def test_the_old_link_shape_still_parses():
    """If the site moves back, nothing is lost."""
    old = HTML.replace("/funding-opportunity/", "/funding/")
    s = IndevJobsScraper()
    assert len(s.parse_listing(old, s.start_url)) == 2


def test_no_blind_page_parameter():
    """The page has no pager. Appending ?page=2 to an SPA that ignores it only
    re-renders the same cards — an accepted-and-ignored parameter."""
    s = IndevJobsScraper()
    assert s.next_page(HTML, s.start_url, 1) is None
