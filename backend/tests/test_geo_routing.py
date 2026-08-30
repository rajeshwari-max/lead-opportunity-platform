"""Where a member works, as a routing axis — and nobody's mail changes yet.

Measured on 4,000 recent actionable rows: US 7.5%, UK 7.1%, Australia 6.3%,
Austria 5.3%, Canada 4.7% — against India at 1.8%. `TeamMember` had keywords,
categories and verticals and no geography at all, so the digest could not know
where anyone works. That is how an Australian council's micro-grant for
individuals reached a member whose filter reads Health / E4C / Livelihood.

The tests that matter most are the ones proving this is inert until somebody
chooses a geography.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest

from app.services.geo_routing import (
    _countries_in,
    describe,
    geo_clause,
    normalize_countries,
    normalize_regions,
)


# ------------------------------------------------------------- inert by default

def test_no_geography_selected_adds_no_filter_at_all():
    """Returns None rather than an always-true clause: that would be slower in
    the query plan and would read, wrongly, like a filter was applied."""
    assert geo_clause([], [], include_unknown=True) is None
    assert geo_clause([], []) is None


def test_a_member_with_no_geography_is_unchanged(db):
    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "unset")
    assert len(MatchingService(db).matches_for(member)) == 5


# ------------------------------------------------------------- and it filters

def test_a_region_matches_its_countries(db):
    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "sa", regions="South Asia")
    got = {o.country for o in MatchingService(db).matches_for(member)}
    assert "India" in got and "Bangladesh" in got
    assert "Australia" not in got and "United Kingdom" not in got


def test_a_named_country_matches_on_its_own(db):
    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "india", countries="India")
    got = {o.country for o in MatchingService(db).matches_for(member)}
    assert got <= {"India", ""}


def test_region_and_country_are_ored_not_anded():
    """Someone selecting "South Asia" and "Kenya" wants both — the region and
    the one country outside it. Requiring every selection would make each
    addition narrow the list, the opposite of what picking more places means."""
    clause = geo_clause(["Kenya"], ["South Asia"], include_unknown=False)
    sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert " OR " in sql


def test_selecting_a_region_still_finds_rows_whose_region_was_never_derived():
    """Without this, "South Asia" would miss every Indian row that predates the
    geography backfill — its country is set and its region column is blank."""
    assert "India" in _countries_in(["South Asia"])
    assert "Bangladesh" in _countries_in(["South Asia"])
    assert "Australia" not in _countries_in(["South Asia"])


# -------------------------------------------------- the 10% with no country

def test_a_row_with_no_country_is_kept_by_default(db):
    """A geographic filter cannot see those rows either way. That is a data
    gap, not evidence the opportunity is somewhere else — and excluding them by
    default would silently drop one row in ten from every filtered digest."""
    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "sa2", regions="South Asia")
    titles = {o.title for o in MatchingService(db).matches_for(member)}
    assert "Global Innovation Challenge" in titles


def test_a_member_can_ask_for_the_tighter_list(db):
    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "strict", regions="South Asia",
                     geo_include_unknown=False)
    titles = {o.title for o in MatchingService(db).matches_for(member)}
    assert "Global Innovation Challenge" not in titles


def test_unknown_means_both_columns_blank_not_either():
    """A row with a country but no region is not unknown — its geography is
    known, and treating it as unknown would let it into every filtered list."""
    clause = geo_clause([], ["South Asia"], include_unknown=True)
    sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "AND" in sql, "the unknown branch must require BOTH columns blank"


# --------------------------------------------------------- names and typos

def test_country_names_are_canonicalised():
    assert normalize_countries("india, KENYA")[0] == "India, Kenya"


def test_region_names_are_canonicalised():
    assert normalize_regions("south asia")[0] == "South Asia"


def test_duplicates_collapse():
    assert normalize_countries("India, india")[0] == "India"


def test_an_unrecognised_place_is_reported_not_dropped():
    """A country nobody recognises matches nothing, which looks exactly like a
    working filter that happens to find nothing — the failure the vertical
    rename already had once."""
    value, unknown = normalize_countries("India, Narnia")
    assert value == "India"
    assert unknown == ["Narnia"]


def test_a_wholly_unrecognised_geography_fails_open_and_says_so(db, caplog):
    """Deliberate. A member whose only entry is a typo gets everything, with a
    warning, rather than silently getting nothing — an empty inbox looks like
    the system is broken and gives them no clue why, while too much mail is
    visibly wrong and the log names the cause."""
    import logging

    from app.services.matching_service import MatchingService

    _rows(db)
    member = _member(db, "typo", countries="Narnia")
    with caplog.at_level(logging.WARNING, logger="scraper"):
        got = MatchingService(db).matches_for(member)
    assert len(got) == 5
    assert any("Narnia" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------- the summary

def test_describe_says_everywhere_when_nothing_is_set():
    assert describe([], [], True) == "everywhere (no geography set)"


def test_describe_names_the_unknown_rule():
    assert "included" in describe(["India"], [], True)
    assert "excluded" in describe(["India"], [], False)


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "geo.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    with db_mod.session_scope() as s:
        yield s


ROWS = [
    ("Rural Water Supply, Bihar", "India", "South Asia"),
    ("Banyule Environment Grants", "Australia", "Oceania"),
    ("Binn Wind Turbine Fund", "United Kingdom", "Europe"),
    ("Health Systems Study, Dhaka", "Bangladesh", "South Asia"),
    ("Global Innovation Challenge", "", ""),
]


def _rows(session):
    from app.database.models import Opportunity, Status

    for i, (title, country, region) in enumerate(ROWS):
        session.add(Opportunity(
            unique_id=f"geo{i}", title=title, organization="F",
            source_website="F", summary="", country=country, region=region,
            status=Status.ACTIVE, deadline=date(2027, 1, 1),
            deadline_state="dated", date_scraped=datetime(2026, 8, 1)))
    session.flush()


def _member(session, name, **kw):
    from app.database.models import TeamMember

    m = TeamMember(name=name, email=f"{name}@x.org", keywords="", categories="",
                   verticals="", auto_send=False, active=True, **kw)
    session.add(m)
    session.flush()
    return m
