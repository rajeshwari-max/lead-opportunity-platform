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


def test_an_immediate_repeat_falls_back_instead_of_ending_the_walk():
    """Catching the repeat was only half the job.

    The guard was fixed and ADB still returned twelve rows, because catching
    the repeat made the walk `return`. `_goto_page` reports success whenever
    the URL *loads* — it loads fine, it just re-serves page 1 — so on every
    healthy run the walk stopped at page 1 and the click path underneath it was
    unreachable. A 489-record source yielded twelve and reported success.
    """
    import inspect

    from app.scrapers.adb import AdbTendersScraper

    src = inspect.getsource(AdbTendersScraper._walk)
    assert "_click_next" in src, "the walk must be able to reach the click path"
    code = "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "PagingMode" in code


# ------------------------------------------------------- the fallback rule

def test_a_url_that_pages_is_used_for_the_whole_walk():
    from app.scrapers.adb import PagingMode

    mode = PagingMode()
    assert mode.should_try_url()
    assert mode.url_result(attempted=True, changed=True) == "continue"
    assert mode.should_try_url()


def test_a_url_that_never_paged_falls_back_to_clicking():
    """The bug, as a rule: unchanged rows on the FIRST attempt mean the
    parameter is accepted and ignored, not that a 489-record list ended."""
    from app.scrapers.adb import PagingMode

    mode = PagingMode()
    assert mode.url_result(attempted=True, changed=False) == "click"


def test_a_url_that_paged_and_then_stopped_really_is_the_end():
    """The same observation means the opposite thing once the parameter has
    been shown to work, and conflating the two is how a broken walk reports
    success — or a finished one reports a defect."""
    from app.scrapers.adb import PagingMode

    mode = PagingMode()
    mode.url_result(attempted=True, changed=True)
    assert mode.url_result(attempted=True, changed=False) == "end"


def test_the_fallback_latches():
    """Once clicking, never navigate again. A navigation resets the widget to
    page 1, so retrying the URL each iteration would re-yield page 1 forever
    while the page counter climbed — a worse failure than the original, because
    it looks like progress."""
    from app.scrapers.adb import PagingMode

    mode = PagingMode()
    mode.url_result(attempted=True, changed=False)
    assert not mode.should_try_url()


def test_a_failed_navigation_does_not_condemn_the_url_mechanism():
    """A blocked or timed-out load is not evidence about the parameter. It
    still falls through to clicking — there is nothing else to try — but it
    must not be recorded as 'the list ended'."""
    from app.scrapers.adb import PagingMode

    mode = PagingMode()
    mode.url_result(attempted=True, changed=True)          # it works
    assert mode.url_result(attempted=False, changed=False) == "click"


# ------------------------------------------------- the wait actually waits

# --------------------------------------------- the URL we actually request

# Supplied by the platform owner on 2026-09-02, copied from their own browser.
# Asserted verbatim, because "which URL does this scraper request" was answered
# for two other sources by reading config rather than by checking, and both
# times the answer was wrong.
OWNER_URL = (
    "https://www.adb.org/projects/tenders"
    "?searchstax[query]=*"
    "&searchstax[page]=1"
    "&searchstax[order]=ds_date_closing%20desc"
    "&searchstax[facets][0]=or:ss_fct_group:consulting"
    "&searchstax[facets][1]=or:sm_fct_status:Active"
)


def test_the_scraper_requests_exactly_the_url_the_owner_gave():
    from app.scrapers.adb import search_url

    assert search_url(1) == OWNER_URL


def test_the_page_number_is_the_only_thing_that_moves():
    from app.scrapers.adb import search_url

    assert search_url(7) == OWNER_URL.replace("[page]=1", "[page]=7")


def test_the_facets_are_indexed_separately():
    """Each facet needs its own [n] parameter. Joining them into one would be
    accepted and ignored — the failure shape this source keeps producing."""
    from app.scrapers.adb import search_url

    url = search_url(1)
    assert "searchstax[facets][0]=" in url and "searchstax[facets][1]=" in url


def test_the_status_facet_is_not_optional():
    """Widening the group is a supported change. Dropping Active is not: it
    turns a 41-page walk into a 4,251-page one that gets truncated at 60."""
    from app.scrapers.adb import DEFAULT_FACETS

    assert any("sm_fct_status:Active" in f for f in DEFAULT_FACETS)


def test_the_facets_can_be_widened_without_editing_code(monkeypatch):
    # Patch the settings object THIS MODULE holds, not the one importable from
    # app.core.config. Other tests in the suite reload that module, which swaps
    # the object — so patching the importable one lands on a different instance
    # and the test passes alone and fails in the suite. It did exactly that.
    from app.scrapers import adb

    monkeypatch.setattr(adb.settings, "adb_tender_facets",
                        "or:sm_fct_status:Active", raising=False)
    url = adb.search_url(1)
    assert "sm_fct_status:Active" in url
    assert "consulting" not in url, "the group restriction must be droppable"


# --------------------------------------------- proving the facet applied

def applied(total: int, baseline: int = 0) -> bool:
    from app.scrapers.adb import AdbTendersScraper

    return AdbTendersScraper._facet_applied(total, baseline)


def test_all_rows_reading_active_is_not_evidence_the_facet_applied():
    """The check I wrote first, and the captured page disproves it.

    logs/adb_no_results.html reads "1 - 12 of 51013" — the entire unfiltered
    universe — and all twelve of its rows say Status: Active. The sort is
    ds_date_closing DESC, so open tenders come first by construction and page 1
    of an unfiltered walk is indistinguishable from page 1 of a filtered one.

    A row-level check would have passed on an unfiltered crawl, taken the
    60-page budget and covered 720 of 51,013 records while reporting success.
    """
    from app.scrapers.adb import AdbTendersScraper

    mix = AdbTendersScraper._status_mix(listing())
    assert set(mix) == {"Active"}, \
        "every row in the fixture taken from that page is Active"
    assert not applied(total=51013, baseline=51013), (
        "...and the count says the facet did nothing. The count is the test.")


def test_a_dropped_count_is_the_evidence():
    assert applied(total=489, baseline=51013)


def test_an_unchanged_count_means_the_facet_was_ignored():
    assert not applied(total=51013, baseline=51013)


def test_no_count_read_claims_nothing():
    """Cannot-tell must not read as yes. The fallback click costs one action;
    a wrong yes costs 98.6% of the source."""
    assert not applied(total=0, baseline=51013)


def test_without_a_baseline_it_falls_back_to_plausibility():
    """A backstop, not the test — used only when the plain listing published no
    count to compare against."""
    assert applied(total=489)
    assert not applied(total=51013)


# ------------------------------------- the control, as ADB actually renders it

def bars() -> tuple[str, str]:
    """(page 1 bar, last page bar) from the captured markup."""
    html = (FIXTURES / "adb_pagination_bar.html").read_text(encoding="utf-8")
    first, last = html.split('<hr id="last-page-below">')
    return first, last


def state(html: str) -> str:
    from app.scrapers.adb import AdbTendersScraper

    return AdbTendersScraper._pagination_state(html)


def test_a_live_next_control_is_recognised():
    assert state(bars()[0]) == "next"


def test_a_disabled_next_control_means_the_end_not_a_broken_walk():
    """ADB marks the dead control with a CLASS and inline pointer-events, not
    the disabled attribute and not aria-disabled. Every stock check reads it as
    live, so the walk would click a dead anchor on the last page and then wait
    the full 30 seconds for rows that were never going to change."""
    assert state(bars()[1]) == "end"


def test_a_missing_bar_is_not_reported_as_the_end():
    """"There is no pagination bar" means the page did not render what we think
    it renders, which is a defect. "Next is disabled" means the walk finished.
    Collapsing the two is how a broken run reports success."""
    assert state("<html><body><p>No results found.</p></body></html>") == "missing"


def test_the_control_is_found_by_id_not_by_its_label():
    """The label is "Next >" — a space and an HTML entity away from every
    exact-match guess. Matching "Next" exactly finds nothing at all."""
    from app.scrapers.adb import _NEXT_SELECTORS

    first = bars()[0]
    assert 'id="searchstax-pagination-next"' in first
    assert ">Next &gt;<" in first, "the label really is not plain 'Next'"
    assert _NEXT_SELECTORS[0] == "#searchstax-pagination-next"


def test_there_are_no_numbered_page_buttons_to_click():
    """Previous and Next are the whole bar. A strategy of "click the button
    labelled 3" had nothing to match, and would have failed silently."""
    from bs4 import BeautifulSoup

    bar = BeautifulSoup(bars()[0], "lxml").select_one(".searchstax-pagination-content")
    labels = [a.get_text(" ", strip=True) for a in bar.find_all("a")]
    assert labels == ["< Previous", "Next >"]


def test_the_label_fallback_matches_on_a_prefix():
    from app.scrapers.adb import _FIND_BY_LABEL_JS

    assert "startsWith" in _FIND_BY_LABEL_JS
    assert "classList.contains('disabled')" in _FIND_BY_LABEL_JS, \
        "the fallback must honour this widget's disabled convention too"


def test_the_click_wait_compares_rows_against_rows():
    """The regression that made the click path useless even when reached.

    `before` had been changed to a 16-character sha256 prefix while the wait
    predicate still read `document.body.innerText.slice(0, 4000)`. A 4,000
    character slice is never equal to a 16-character hash, so the predicate was
    true on its first evaluation and the wait returned immediately, having
    waited for nothing.
    """
    from app.scrapers.adb import _ROWS_CHANGED_JS, _ROWS_JS, RESULT_BLOCK

    assert "innerText.slice(0, 4000)" not in _ROWS_CHANGED_JS
    assert RESULT_BLOCK in _ROWS_JS, \
        "the wait must look at the same rows the signature does"
    assert _ROWS_JS.strip() in _ROWS_CHANGED_JS, \
        "one expression, so the wait and the verification cannot disagree"


def test_an_empty_container_does_not_count_as_the_next_page():
    """The widget empties its results while it fetches. An empty container is
    different from the previous rows without being the next page, and treating
    it as arrival would capture a spinner."""
    from app.scrapers.adb import _ROWS_CHANGED_JS

    assert "!!now" in _ROWS_CHANGED_JS
