"""A renamed vertical must not leave a member's routing working by luck.

The routing audit found this stored against a real member:

    verticals: Climate/Sustainability, Climate/Sustainability(ESG)

The old name and its replacement, both saved. It routes correctly today only
because the vertical filter is a substring test and the old name is a prefix of
the new one — so the first person to make that matching exact, which is the
correct change, silently empties that member's routing and nobody finds out
until they notice the mail stopped.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.services.vertical_names import (
    LEGACY_NAMES,
    canonical_vertical,
    normalize_vertical_csv,
)
from app.services.verticals import VERTICALS


# --------------------------------------------------------- resolving one name

@pytest.mark.parametrize("name", VERTICALS)
def test_a_current_name_resolves_to_itself(name):
    assert canonical_vertical(name) == name


@pytest.mark.parametrize("old,new", sorted(LEGACY_NAMES.items()))
def test_every_legacy_name_resolves_to_a_real_vertical(old, new):
    assert canonical_vertical(old) == new
    assert new in VERTICALS, f"{old!r} maps to {new!r}, which is not a vertical"


def test_resolution_ignores_case_and_padding():
    assert canonical_vertical("  health  ") == "Health"


def test_an_unknown_value_resolves_to_empty_rather_than_passing_through():
    """Passing a typo through would let it sit in someone's routing forever,
    matching nothing, looking exactly like a working filter that finds
    nothing."""
    assert canonical_vertical("Helth") == ""
    assert canonical_vertical("Agriculture") == ""


def test_blank_resolves_to_blank():
    assert canonical_vertical("") == ""
    assert canonical_vertical("   ") == ""


# ------------------------------------------------------- the real member row

def test_the_duplicate_that_the_audit_found_collapses_to_one_vertical():
    """One vertical written twice is not two things to filter on, and a
    routing list that names it twice looks like broader coverage than it is."""
    got, unknown = normalize_vertical_csv(
        "Climate/Sustainability, Climate/Sustainability(ESG)")
    assert got == "Climate/Sustainability(ESG)"
    assert unknown == []


def test_normalising_does_not_change_what_that_member_matches():
    """The safety property. Both spellings selected the same vertical before;
    the normalised value must select exactly that same one."""
    before = {"Climate/Sustainability(ESG)"}     # what the substring test hit
    after = set(normalize_vertical_csv(
        "Climate/Sustainability, Climate/Sustainability(ESG)")[0].split(", "))
    assert after == before


def test_a_legacy_only_list_still_routes_after_normalising():
    """A member saved before the rename, with no new-name entry at all."""
    got, _ = normalize_vertical_csv("E4C")
    assert got == "E4C(Evidence for Change)"


def test_order_is_preserved_for_a_list_that_needs_no_change():
    value = "Health, Livelihood"
    assert normalize_vertical_csv(value)[0] == value


def test_unknown_values_are_reported_and_not_silently_dropped():
    """Deleting part of someone's routing without telling them is how a filter
    quietly stops matching what they expect."""
    got, unknown = normalize_vertical_csv("Health, Nonsense, Livelihood")
    assert got == "Health, Livelihood"
    assert unknown == ["Nonsense"]


def test_an_empty_list_stays_empty():
    """Empty means 'all verticals'. Turning it into anything else would change
    what a member receives."""
    assert normalize_vertical_csv("") == ("", [])


# ------------------------------------------------ the migration does the same

@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "vn.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def _member(db_mod, verticals):
    from app.database.models import TeamMember
    with db_mod.session_scope() as s:
        m = TeamMember(name="X", email=f"{verticals[:8]}@x.org", keywords="",
                       categories="", verticals=verticals, auto_send=False,
                       active=True)
        s.add(m)
        s.flush()
        return m.id


def _verticals_of(db_mod, member_id):
    from sqlalchemy import select

    from app.database.models import TeamMember
    with db_mod.session_scope() as s:
        return s.execute(
            select(TeamMember).where(TeamMember.id == member_id)
        ).scalar_one().verticals


def test_the_migration_fixes_a_stored_legacy_name(db):
    member_id = _member(db, "Climate/Sustainability, Climate/Sustainability(ESG)")
    with db.engine.begin() as conn:
        db._run_migrations(conn)
    assert _verticals_of(db, member_id) == "Climate/Sustainability(ESG)"


def test_the_migration_leaves_a_correct_list_alone(db):
    member_id = _member(db, "Health, Livelihood")
    with db.engine.begin() as conn:
        db._run_migrations(conn)
    assert _verticals_of(db, member_id) == "Health, Livelihood"


def test_the_migration_does_not_touch_a_member_with_an_unknown_vertical(db):
    """It reports and moves on. Rewriting a routing list that contains
    something unrecognised risks discarding the part nobody understood."""
    member_id = _member(db, "Health, Something Odd")
    with db.engine.begin() as conn:
        db._run_migrations(conn)
    assert _verticals_of(db, member_id) == "Health, Something Odd"


def test_the_migration_is_repeatable(db):
    member_id = _member(db, "E4C")
    for _ in range(3):
        with db.engine.begin() as conn:
            db._run_migrations(conn)
    assert _verticals_of(db, member_id) == "E4C(Evidence for Change)"
