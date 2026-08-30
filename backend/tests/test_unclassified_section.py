"""The Unclassified section, and the filter flags that made it possible.

The brief's worked example, tested literally: an administrator searches
"solar irrigation", selects the matches, assigns two verticals, and those rows
leave the section and enter the normal Active table.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest


@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "unc.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    with db_mod.session_scope() as s:
        _seed(s)
        yield s


ROWS = [
    ("Solar Irrigation Pump Scheme for Smallholders", "India", "NGOBOX",
     "Drip irrigation and solar pumps for farmers."),
    ("Solar Rooftop Installation Tender", "Kenya", "World Bank",
     "Supply and install rooftop solar."),
    ("Irrigation Canal Rehabilitation", "Nepal", "ADB Tenders",
     "Civil works for canal repair."),
    ("Office Furniture Supply", "Ghana", "World Bank", "Chairs and desks."),
    ("Security Guard Services", "India", "NGOBOX", "Manned guarding."),
]


def _seed(session):
    from app.database.models import Category, Opportunity, Status

    for i, (title, country, source, summary) in enumerate(ROWS):
        session.add(Opportunity(
            unique_id=f"u{i}", title=title, organization=f"Org {i}",
            source_website=source, summary=summary, country=country,
            category=Category.TENDER, status=Status.ACTIVE,
            deadline=date(2027, 1, 1), deadline_state="dated",
            verticals="", date_scraped=datetime(2026, 8, 1)))
    session.flush()


def Q(**kw):
    from app.services.vertical_assignment import UnclassifiedQuery
    return UnclassifiedQuery(**kw)


# --------------------------------------------------- the brief's own example

def test_the_worked_example_end_to_end(db):
    """Search, select, assign two verticals, and the rows leave the section."""
    from app.services import vertical_assignment as va

    found = va.search_unclassified(db, Q(search="solar irrigation"))
    assert found["total"] == 1
    assert "Solar Irrigation" in found["items"][0]["title"]

    ids = va.matching_ids(db, Q(search="solar"))
    assert len(ids) == 2

    va.assign(db, ids, ["Livelihood", "Climate/Sustainability(ESG)"],
              reviewer="admin@example.org")
    db.flush()

    assert va.count_unclassified(db) == 3
    from sqlalchemy import select

    from app.database.models import Opportunity
    row = db.execute(select(Opportunity).where(Opportunity.id == ids[0])).scalar_one()
    assert "Livelihood" in row.verticals
    assert "Climate/Sustainability(ESG)" in row.verticals


def test_searching_assigns_nothing_by_itself(db):
    """"Searching by keyword must not silently assign labels." """
    from app.services import vertical_assignment as va

    before = va.count_unclassified(db)
    va.search_unclassified(db, Q(search="solar irrigation"))
    va.matching_ids(db, Q(search="solar"))
    db.flush()
    assert va.count_unclassified(db) == before


# ------------------------------------------------------------ search rules

def test_every_term_must_appear_somewhere(db):
    """AND across terms, OR across fields. OR across terms would return every
    solar row and every irrigation row, which is not what the phrase means."""
    from app.services import vertical_assignment as va

    assert va.search_unclassified(db, Q(search="solar"))["total"] == 2
    assert va.search_unclassified(db, Q(search="irrigation"))["total"] == 2
    assert va.search_unclassified(db, Q(search="solar irrigation"))["total"] == 1


def test_search_reaches_the_summary_not_only_the_title(db):
    from app.services import vertical_assignment as va

    got = va.search_unclassified(db, Q(search="farmers"))
    assert got["total"] == 1


def test_search_matches_are_case_insensitive(db):
    from app.services import vertical_assignment as va

    assert va.search_unclassified(db, Q(search="SOLAR"))["total"] == 2


# ---------------------------------------------------------------- filters

@pytest.mark.parametrize("kwargs,expected", [
    ({"sources": ("NGOBOX",)}, 2),
    ({"countries": ("India",)}, 2),
    ({"categories": ("Tender",)}, 5),
    ({"deadline_after": date(2027, 6, 1)}, 0),
    ({"deadline_before": date(2027, 6, 1)}, 5),
])
def test_each_filter_dimension_narrows_server_side(db, kwargs, expected):
    from app.services import vertical_assignment as va

    assert va.search_unclassified(db, Q(**kwargs))["total"] == expected


def test_filters_combine(db):
    from app.services import vertical_assignment as va

    assert va.search_unclassified(
        db, Q(search="solar", sources=("NGOBOX",)))["total"] == 1


# --------------------------------------------------------------- paging

def test_paging_reports_the_full_total_not_the_page(db):
    """Select-all needs to know what the filter matches, not what fits on
    screen — a bulk action scoped to the visible page silently does a fraction
    of what was asked."""
    from app.services import vertical_assignment as va

    page = va.search_unclassified(db, Q(page_size=2))
    assert len(page["items"]) == 2
    assert page["total"] == 5
    assert page["pages"] == 3


def test_the_second_page_holds_different_rows(db):
    from app.services import vertical_assignment as va

    first = {i["id"] for i in va.search_unclassified(db, Q(page_size=2))["items"]}
    second = {i["id"] for i in
              va.search_unclassified(db, Q(page_size=2, page=2))["items"]}
    assert first and second and not (first & second)


def test_select_all_spans_every_page(db):
    from app.services import vertical_assignment as va

    assert len(va.matching_ids(db, Q(page_size=2))) == 5


def test_select_all_is_capped_at_what_a_bulk_write_accepts(db):
    """The UI must not be able to offer a selection the write path refuses."""
    from app.services import vertical_assignment as va

    ids = va.matching_ids(db, Q(), cap=2)
    assert len(ids) <= 3          # cap + 1, so the caller can detect the overflow


# ------------------------------------------------- suggestions and evidence

def test_each_row_carries_the_model_s_suggestion_and_its_evidence(db):
    """A bare confidence number gives a reviewer nothing to agree with."""
    from app.services import vertical_assignment as va

    item = va.search_unclassified(db, Q(search="solar irrigation"))["items"][0]
    assert item["suggestions"], "no suggestion offered"
    top = item["suggestions"][0]
    assert 0 < top["score"] <= 1
    assert top["evidence"], "no evidence for the suggestion"
    assert item["classification_status"] in {"classified", "uncertain",
                                             "unclassified"}


def test_a_row_with_no_signal_says_so_rather_than_guessing(db):
    from app.services import vertical_assignment as va

    item = va.search_unclassified(db, Q(search="furniture"))["items"][0]
    assert item["suggestions"] == []


# --------------------------------------------- the flags that were `if True`

def test_the_main_table_still_hides_unclassified_rows_by_default(db):
    from app.schemas.opportunity import OpportunityFilters
    from app.services.filter_service import FilterService

    got = FilterService(db).query(OpportunityFilters())
    assert got.total == 0, "unclassified rows must not reach the Active table"


def test_the_flag_can_now_actually_be_turned_off(db):
    """It was `if True:` — documented as an option and ignored. A caller
    passing has_vertical=false got the same rows as one passing true, and the
    Unclassified section could not have existed."""
    from app.schemas.opportunity import OpportunityFilters
    from app.services.filter_service import FilterService

    got = FilterService(db).query(OpportunityFilters(has_vertical=False))
    assert got.total == 5


def test_the_english_flag_can_be_turned_off_too(db):
    from app.schemas.opportunity import OpportunityFilters
    from app.services.filter_service import FilterService

    both_off = FilterService(db).query(
        OpportunityFilters(has_vertical=False, english_only=False))
    assert both_off.total == 5


def test_defaults_are_unchanged_so_no_existing_view_moved(db):
    from app.schemas.opportunity import OpportunityFilters

    f = OpportunityFilters()
    assert f.english_only is True and f.has_vertical is True


# ------------------------------------------- classification fields recorded

def test_a_human_assignment_records_the_classification_trail(db):
    from app.services import vertical_assignment as va

    ids = va.matching_ids(db, Q(search="furniture"))
    va.assign(db, ids, ["Livelihood"], reviewer="admin@example.org")
    db.flush()

    from sqlalchemy import select

    from app.database.models import Opportunity
    row = db.execute(select(Opportunity).where(Opportunity.id == ids[0])).scalar_one()
    assert row.classification_status == "classified"
    assert row.classification_source == "human"
    assert row.classified_at is not None
    # A person is not a model version, and recording one would make a human
    # decision look reproducible by re-running something.
    assert row.classification_version is None


def test_marking_none_of_the_six_is_recorded_as_a_decision(db):
    from app.services import vertical_assignment as va

    ids = va.matching_ids(db, Q(search="guard"))
    va.assign(db, ids, [], reviewer="admin@example.org")
    db.flush()

    from sqlalchemy import select

    from app.database.models import Opportunity
    row = db.execute(select(Opportunity).where(Opportunity.id == ids[0])).scalar_one()
    assert row.classification_status == "unclassified"
    assert row.classification_source == "human"
    assert va.count_unclassified(db) == 4, "it must leave the queue"


# --------------------------------------------------- expired rows drop out

def test_an_expired_row_leaves_the_working_unclassified_section(db):
    """"Expired unclassified records should leave the working Unclassified
    section and move to the archive automatically." """
    from sqlalchemy import update

    from app.database.models import Opportunity, Status
    from app.services import vertical_assignment as va

    before = va.count_unclassified(db)
    db.execute(update(Opportunity).where(Opportunity.id == 1)
               .values(status=Status.EXPIRED))
    db.flush()
    assert va.count_unclassified(db) == before - 1
