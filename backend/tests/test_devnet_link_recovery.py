"""Recovering DevNet's detail links, and the failure that is worse than losing one.

The situation
-------------
DevNetJobsIndia's RFP list is an ASP.NET GridView. Some rows carry a real
`jobdescription.aspx?job_id=N` link; others are postback-only —

    <a href="javascript:__doPostBack('ctl00$grdJobs','Select$1')">

— and carry no id at all. There is no GET URL to build from a postback, so the
id has to be recovered: from the row's own href, from a `joblogos/<id>.jpg`
image, or by matching the title against the sidebar's direct links.

The 2026-09-02 run: 29 rows extracted, 13 dropped for want of a usable link
(44.8%, against a 20% bar). Those are not bad rows on the dashboard — they are
calls nobody ever sees.

What was wrong with the third path
----------------------------------
It compared raw lowercased strings with `startswith`. A GridView emits `&nbsp;`
between words, which BeautifulSoup returns as \\xa0 — the same title to a
reader, a different string to `startswith`. That character has already cost
this project a whole source once: ADB writes "Notice&nbsp;Type" and a check
looking for "Notice Type" never fired, throwing away a page of good tenders.

Why the ambiguity guard matters more than the fix
-------------------------------------------------
Prefix-matching on 40 characters can hit two different notices from the same
organisation. Attaching one row to the other's job_id produces a link that
WORKS and opens the wrong call — and nothing downstream can see that. A missing
link shows up in `link_loss`; a wrong link shows up nowhere. So a title that
matches more than one sidebar entry is dropped, not guessed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.scrapers.devnet import normalise_title

FIXTURE = Path(__file__).parent / "fixtures" / "devnet_listing_row.html"


def scraper():
    from app.scrapers.registry import SCRAPER_REGISTRY
    import app.scrapers                                        # noqa: F401

    return SCRAPER_REGISTRY["devnet"]()


# ------------------------------------------------------------ normalisation

def test_a_non_breaking_space_is_a_space():
    """The specific character. ASP.NET emits it; readers cannot see it;
    `startswith` treats it as a different string."""
    assert (normalise_title("Endline\xa0Evaluation")
            == normalise_title("Endline Evaluation"))


def test_runs_of_whitespace_collapse():
    assert (normalise_title("Endline   Evaluation\n in Bihar")
            == normalise_title("Endline Evaluation in Bihar"))


def test_the_sidebar_ellipsis_is_dropped():
    """The sidebar truncates long titles with an ellipsis; the grid does not."""
    assert (normalise_title("Nutrition Baseline Study…")
            == normalise_title("Nutrition Baseline Study"))


def test_it_does_not_make_different_titles_equal():
    assert (normalise_title("Baseline Survey in Odisha")
            != normalise_title("Baseline Survey in Bihar"))


# --------------------------------------------------------------- recovery

def test_a_direct_link_row_keeps_its_own_id():
    rows = scraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"),
        "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    by_title = {r.title: r.opportunity_url for r in rows}
    hit = next(u for t, u in by_title.items() if "Odisha" in t)
    assert hit.endswith("jobdescription.aspx?job_id=900001")


def test_a_postback_row_recovers_its_id_from_the_sidebar():
    """The row this fix exists for: no href id, no logo image, and a sidebar
    label separated by a non-breaking space."""
    rows = scraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"),
        "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    hit = next(r for r in rows if "Endline" in r.title)
    assert hit.opportunity_url.endswith("jobdescription.aspx?job_id=900002")


def test_every_row_in_the_fixture_now_has_a_usable_link():
    rows = scraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"),
        "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    assert rows
    assert all(r.opportunity_url for r in rows)
    assert all("job_id=" in r.opportunity_url for r in rows)


# ----------------------------------------------------- the guard that matters

AMBIGUOUS = """
<table id="grdJobs">
  <tr><td>
    <a href="javascript:__doPostBack('ctl00$grdJobs','Select$1')">
      Request for Proposals for a Baseline Survey in Odisha</a>
    Sanitized Organisation
    Apply By: 31/07/2027
  </td></tr>
</table>
<div id="sidebar">
  <a href="jobdescription.aspx?job_id=111">Request for Proposals for a Baseline Survey in Odisha</a>
  <a href="jobdescription.aspx?job_id=222">Request for Proposals for a Baseline Survey in Bihar</a>
</div>
"""


def test_a_title_matching_two_sidebar_entries_is_dropped_not_guessed():
    """Both sidebar titles share far more than 40 characters with the row.

    Picking either produces a link that WORKS and opens the wrong call. Nothing
    downstream can detect that — a missing link is counted in link_loss, a
    wrong one is invisible. So the row is dropped.
    """
    rows = scraper().parse_listing(
        AMBIGUOUS, "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    for r in rows:
        assert not r.opportunity_url, (
            "an ambiguous match must yield no link, never a plausible one")


def test_it_never_points_a_row_at_the_index_page():
    """The original defect: 86 different RFPs all pointing at
    rfp_assignments.aspx — every one opening the index it was scraped from,
    and every one sharing a URL, which also defeated deduplication."""
    rows = scraper().parse_listing(
        AMBIGUOUS, "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    assert all("rfp_assignments" not in (r.opportunity_url or "") for r in rows)


def test_the_ambiguous_drop_is_logged(caplog):
    """A dropped row has to be visible, or 13 lost calls look like 13 that were
    never published."""
    import logging

    with caplog.at_level(logging.INFO, logger="scraper"):
        scraper().parse_listing(
            AMBIGUOUS, "https://www.devnetjobsindia.org/rfp_assignments.aspx")
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "sidebar" in said or "dropping" in said
