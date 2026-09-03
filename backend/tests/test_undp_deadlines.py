"""UNDP Procurement's deadlines, and the percentage that hid the real fault.

The reported defect was:

    [QUALITY] deadlines: 50.0% of the deadline strings parse into a date,
              below 90%. Rows whose date never parses cannot expire.

which reads like a parser that mishandles UNDP's date format. The parser is
fine — `DeadlineParser().parse("21-Feb-27")` has always returned 2027-02-21.

The real numbers from the same run:

    deadline_present   0.4%      <- 2 of 555 rows carried a date string AT ALL
    deadline_parses   50.0%      <- measured over those 2 rows

So 553 rows had no date extracted, and that was invisible behind a percentage
computed on a sample of two. Two separate causes:

  1. `21-Feb-27` — the shape UNDP prints — matched none of the extraction
     patterns. The spaced form wanted `\\s` separators and a four-digit year;
     the all-numeric form wanted digits where the month name is.
  2. The date sits in its own table cell with no "Deadline:" label beside it,
     and the label-driven regex needs one.

The lesson worth keeping is the third one: a quality percentage computed over a
tiny denominator is not a small problem, it is a wrong signal — it named the
parser and pointed away from extraction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.scrapers.generic_listing import _DATE_ONLY, _DEADLINE
from app.services.deadline_parser import DeadlineParser

FIXTURE = Path(__file__).parent / "fixtures" / "undp_procurement_listing.html"


def scraper():
    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                        # noqa: F401

    return SCRAPER_REGISTRY["undp_procurement"]()


# --------------------------------------------- the parser was never at fault

@pytest.mark.parametrize("raw,expected", [
    ("21-Feb-27", "2027-02-21"),
    ("05-Mar-27", "2027-03-05"),
    ("09-01-27", "2027-01-09"),
])
def test_the_parser_already_handled_these(raw, expected):
    assert str(DeadlineParser().parse(raw)) == expected


# ------------------------------------------------------- extraction, fixed

@pytest.mark.parametrize("raw", ["21-Feb-27", "05-Mar-27", "21-Feb-2027",
                                 "21/Feb/27"])
def test_the_hyphenated_month_shape_is_extracted(raw):
    assert _DATE_ONLY.search(raw), f"{raw!r} must be recognised as a date"


@pytest.mark.parametrize("raw", ["21 Feb 2027", "February 21, 2027",
                                 "2027-02-21", "09-01-27"])
def test_the_shapes_that_already_worked_still_do(raw):
    """Added as a new alternative rather than by loosening the old ones, so no
    source that works today can lose recall."""
    assert _DATE_ONLY.search(raw)
    assert _DEADLINE.search(f"Deadline: {raw}")


@pytest.mark.parametrize("noise", ["2-year-2027", "REF-ABC-2027", "12-345-67"])
def test_it_does_not_invent_dates_out_of_reference_codes(noise):
    """The month is matched by NAME, not `\\w{3,9}`. Without that, "REF-ABC-2027"
    is a date — and it parses, to today's date with the year replaced, which is
    a deadline nobody can see is wrong."""
    assert not _DATE_ONLY.search(noise)


def test_the_two_date_patterns_cannot_drift_apart():
    """They were two hand-copied copies of one alternation, and the comment
    above them said they must match the same shapes. That is how the ADB guard
    ended up comparing a text slice against a hash."""
    from app.scrapers.generic_listing import _DATE_SHAPES

    assert _DATE_SHAPES in _DEADLINE.pattern
    assert _DATE_SHAPES in _DATE_ONLY.pattern


# ------------------------------------------------ the date in its own cell

def test_a_date_alone_in_a_cell_is_read_as_the_deadline():
    rows = scraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"),
        "https://procurement-notices.undp.org/?lang=en")
    assert rows, "the fixture must still parse"
    assert all((r.deadline_raw or "").strip() for r in rows), \
        "every notice on this board carries a closing date"
    parsed = [DeadlineParser().parse(r.deadline_raw) for r in rows]
    assert all(p is not None for p in parsed)
    assert {str(p) for p in parsed} == {"2027-02-21", "2027-01-09", "2027-03-05"}


def test_two_date_cells_are_declined_rather_than_guessed():
    """A board printing a posting date AND a closing date gives no way to tell
    them apart by shape. A wrong deadline is worse than none — it silently
    expires a live call, or keeps a dead one on the dashboard."""
    html = ('<table><tr>'
            '<td><a href="/view_notice.cfm?notice_id=1">'
            'Request for Proposals: Borehole Rehabilitation</a></td>'
            '<td>01-Jan-27</td><td>21-Feb-27</td></tr></table>')
    rows = scraper().parse_listing(html, "https://procurement-notices.undp.org/")
    assert rows
    assert not (rows[0].deadline_raw or "").strip(), \
        "ambiguous rows must yield no deadline, not a guessed one"


def test_a_cell_of_prose_containing_a_date_is_not_a_date_cell():
    """`fullmatch` is what makes "its own cell" true."""
    html = ('<table><tr>'
            '<td><a href="/view_notice.cfm?notice_id=2">'
            'Invitation to Bid: Supply of Laboratory Reagents</a></td>'
            '<td>Published on 01-Jan-27 by the country office</td>'
            '</tr></table>')
    rows = scraper().parse_listing(html, "https://procurement-notices.undp.org/")
    assert rows
    assert not (rows[0].deadline_raw or "").strip()


# ------------------------------------------- a curated board keeps its rows

def test_a_consultancy_notice_is_not_discarded_for_saying_nothing_about_money():
    """UNDP is declared a curated notice board — every row is a published
    notice. With the funding-signal test on, "Individual Consultant: Gender
    Mainstreaming Adviser" is thrown away for containing no funding vocabulary,
    which on this board is the wrong question to ask."""
    rows = scraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"),
        "https://procurement-notices.undp.org/?lang=en")
    titles = [r.title for r in rows]
    assert any("Individual Consultant" in t for t in titles), titles
    assert len(rows) == 3


def test_navigation_is_still_rejected_on_the_curated_board():
    """Turning the funding-signal test off must not open the door to chrome —
    is_furniture, the same-page test and the nav-href test still apply."""
    html = ('<table><tr><td><a href="/?lang=en">Skip to main content</a></td>'
            '<td><a href="/about.cfm">About this site and how it works</a></td>'
            '</tr></table>')
    rows = scraper().parse_listing(html, "https://procurement-notices.undp.org/?lang=en")
    assert rows == [] or all("Skip to main" not in r.title for r in rows)
