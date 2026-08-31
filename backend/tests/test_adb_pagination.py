"""The ADB pagination guard, and why the old one could not have worked.

The 2026-08-30 verification run recorded ADB Tenders as:

    pages 3 | extracted 36  unique 12  duplicates 24  (66.7%)

Thirty-six rows over three pages of which twelve were distinct: the same twelve
rows, three times. The walk already had a guard meant to catch exactly that —

    before = page.inner_text("body")[:4000]
    ...
    if page.inner_text("body")[:4000] == before: return

— and it passed, three times running, because the first four thousand
characters of that page are header, navigation and facet counts. Chrome that
changes between navigations while the twelve result rows stay identical.

This is the World Bank `os={offset}` failure in a different source: a paging
parameter that is present, accepted, and does nothing. It is the shape that
looks most like success, and the only defence is a guard that compares the
results.

The guard is now a pure function of HTML, which is the second half of the fix.
Nothing could exercise the old one without a browser, so nothing did.
"""
from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def sig(html: str) -> str:
    from app.scrapers.adb import AdbTendersScraper

    return AdbTendersScraper._results_signature(html)


def listing() -> str:
    return (FIXTURES / "adb_listing_row.html").read_text(encoding="utf-8")


# --------------------------------------------------------- it is a function

def test_the_guard_can_be_tested_without_a_browser():
    """The reason the old defect survived: it lived inside a Playwright loop,
    so no test could reach it."""
    assert sig(listing())


def test_the_same_results_hash_the_same():
    assert sig(listing()) == sig(listing())


# ------------------------------------------- what the old guard missed

def test_page_chrome_changing_does_not_change_the_signature():
    """The precise failure. A page indicator, a facet count and a nav label all
    change between page 1 and a page 2 that re-served page 1's rows — and the
    old guard read exactly that region."""
    page1 = listing()
    page2 = (
        '<div class="header">Showing 13 - 24 of 489 · Page 2 of 41</div>'
        + page1
    )
    assert sig(page1) == sig(page2), (
        "the signature must ignore everything that is not a result row")


def test_different_results_do_change_the_signature():
    page1 = listing()
    page2 = page1.replace("searchstax-search-result", "searchstax-search-result") \
                 .replace("Contract Award", "Invitation for Bids")
    assert sig(page1) != sig(page2)


def test_a_changed_link_changes_the_signature_even_if_the_text_is_identical():
    """Two pages of a paginated list can legitimately carry similar titles.
    The link is what says they are different records."""
    page1 = listing()
    page2 = page1.replace("href=\"", "href=\"/other")
    assert sig(page1) != sig(page2)


def test_tracking_parameters_alone_do_not_count_as_a_new_page():
    """A widget re-rendering the same rows with rotated query parameters would
    defeat a link-only comparison, so the query string is dropped and the row's
    text carries the rest of the weight."""
    page1 = listing()
    page2 = page1.replace(".html", ".html?utm_source=paging")
    assert sig(page1) == sig(page2)


# ----------------------------------------------------------- empty results

def test_an_empty_page_is_its_own_case_not_the_same_as_last_time():
    """"No results" and "the same results" are different situations. Hashing
    the empty set to a value would make the second page of an emptied listing
    look like a repeat of the first."""
    assert sig("<html><body><p>No results found.</p></body></html>") == ""
    assert sig("") == ""


def test_the_result_selector_is_named_once_and_shared():
    """The guard, the per-page count and the parser all have to agree about
    what a result is. They did not, which is how the count could read twelve
    while the guard read the whole body."""
    from app.scrapers.adb import RESULT_BLOCK

    assert RESULT_BLOCK == "div.searchstax-search-result"
    assert RESULT_BLOCK in (FIXTURES / "adb_listing_row.html").read_text(
        encoding="utf-8").replace('div class="', "div.").replace('"', "") \
        or "searchstax-search-result" in listing()


# ------------------------------------------------- it is wired into the walk

def test_the_walk_compares_results_and_no_longer_compares_the_body():
    import inspect

    from app.scrapers.adb import AdbTendersScraper

    src = inspect.getsource(AdbTendersScraper._walk)
    assert "_results_signature" in src
    # Code only. The comment explaining the defect quotes it, and should.
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert 'inner_text("body")[:4000]' not in code, (
        "the body comparison is the defect; it must not come back")


def test_an_immediate_repeat_is_reported_as_a_defect_not_as_the_end_of_the_list():
    """Stopping is right either way, but a walk that stops on page 2 of a
    489-record list has not finished — and logging 'end of the list' would
    record a pagination failure as a completed walk."""
    import inspect

    from app.scrapers.adb import AdbTendersScraper

    src = inspect.getsource(AdbTendersScraper._walk)
    assert "pagination defect" in src
    assert "if n == 1:" in src
