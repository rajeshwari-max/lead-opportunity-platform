"""GrantWatch: walk the listing by URL, and stop on the rows, not the pager.

Measured in a real browser on 2026-09-21:

  * setPage('N') is an ordinary navigation to new-grants.php?pageNum=N.
  * The pager only ever renders buttons 1-4, and page 4 has no '›'.
  * pageNum=5 .. 11 each still return 14 grants; pageNum=12+ returns none.
  * 154 unique grants by URL, against at most 56 reachable by clicking '›'.

The old scraper clicked '›' and so could never get past page 4. These tests
pin the replacement: next_page() always asks for the next pageNum, and the
walk ends only when a page comes back with no grants.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.scrapers.grantwatch import _MAX_PAGES, GrantWatchScraper, page_url_for

FIXTURE = Path(__file__).parent / "fixtures" / "grantwatch_card.html"
START = "https://international.grantwatch.com/new-grants.php"


def page_of(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["pageNum"][0])


# ------------------------------------------------------------------ parsing

def test_both_cards_are_read_and_view_grant_links_are_not_rows():
    rows = GrantWatchScraper().parse_listing(FIXTURE.read_text(), START)
    assert [r.opportunity_url.split("/grant/")[1][:6] for r in rows] == ["173825", "232800"]


def test_deadline_and_ongoing_are_read_across_the_line_break():
    rows = GrantWatchScraper().parse_listing(FIXTURE.read_text(), START)
    assert [r.deadline_raw for r in rows] == ["Ongoing", "03/05/27"]
    assert all(r.dayfirst is False for r in rows), "US dates: 03/05/27 is 5 March"


def test_summary_comes_from_the_description_paragraph():
    rows = GrantWatchScraper().parse_listing(FIXTURE.read_text(), START)
    assert rows[0].summary.startswith("Grants of up to $1,000")


# --------------------------------------------------------------- pagination

def test_next_page_is_a_plain_url():
    req = GrantWatchScraper().next_page(FIXTURE.read_text(), START, 1)
    assert req is not None and page_of(req.url) == 2


def test_it_does_not_stop_where_the_pager_stops():
    """The regression. The pager on page 4 offers no '›', and the old walk
    ended there. The listing goes on to page 11."""
    html_with_no_next_button = FIXTURE.read_text().replace("&rsaquo;", "")
    s = GrantWatchScraper()
    url = page_url_for(START, 4)
    req = s.next_page(html_with_no_next_button, url, 4)
    assert req is not None and page_of(req.url) == 5


def test_page_url_for_replaces_pagenum_and_keeps_other_parameters():
    url = page_url_for(START + "?pageNum=3&sort=new", 4)
    q = parse_qs(urlsplit(url).query)
    assert q == {"pageNum": ["4"], "sort": ["new"]}


def test_the_backstop_ends_the_walk():
    assert GrantWatchScraper().next_page("", page_url_for(START, _MAX_PAGES), _MAX_PAGES) is None


def test_the_whole_listing_is_walked_and_an_empty_page_ends_it(monkeypatch):
    """End to end through BaseScraper.crawl with a fake site of 11 pages, the
    shape measured live. Every page is fetched once, page 12 ends it."""
    card = FIXTURE.read_text()
    fetched: list[int] = []

    async def fake_fetch(self, client, req):
        n = page_of(req.url) if "pageNum" in req.url else 1
        fetched.append(n)
        if n > 11:
            return "<html><body><ul class='pagination'></ul></body></html>"
        # distinct grant ids per page, so the repeat-guard does not fire
        return card.replace("173825", f"9{n:02d}001").replace("232800", f"9{n:02d}002")

    monkeypatch.setattr(GrantWatchScraper, "_fetch", fake_fetch)

    async def run():
        s = GrantWatchScraper()
        stop, go = asyncio.Event(), asyncio.Event()
        go.set()

        async def progress(*_a, **_k):
            return None

        rows = []
        async for batch in s.crawl(stop, go, progress):
            rows.extend(batch)
        return rows

    rows = asyncio.run(run())
    assert fetched == list(range(1, 13))
    assert len(rows) == 22
    assert len({r.opportunity_url for r in rows}) == 22


def test_a_page_that_repeats_an_earlier_one_ends_the_walk(monkeypatch):
    """If GrantWatch ever answers an out-of-range pageNum by re-serving page 1
    instead of an empty page, the walk must still stop."""
    card = FIXTURE.read_text()
    fetched: list[int] = []

    async def fake_fetch(self, client, req):
        n = page_of(req.url) if "pageNum" in req.url else 1
        fetched.append(n)
        if n >= 3:
            return card                     # same rows as page 1, forever
        return card.replace("173825", f"8{n}0001").replace("232800", f"8{n}0002")

    monkeypatch.setattr(GrantWatchScraper, "_fetch", fake_fetch)

    async def run():
        s = GrantWatchScraper()
        stop, go = asyncio.Event(), asyncio.Event()
        go.set()

        async def progress(*_a, **_k):
            return None

        return [r async for b in s.crawl(stop, go, progress) for r in b]

    asyncio.run(run())
    assert fetched == [1, 2, 3, 4], "page 3 is new content, page 4 repeats it"


def test_the_walk_is_not_cut_short_by_already_stored_rows():
    """stale_page_streak_override = 0: the deeper pages hold grants the pager
    never exposed, and re-reading them is what fills late deadlines."""
    assert GrantWatchScraper.stale_page_streak_override == 0
