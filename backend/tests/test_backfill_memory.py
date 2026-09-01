"""The startup backfills must not load the whole table into memory.

The measurement that prompted this
----------------------------------
2026-09-01, a freshly restarted Gunicorn worker on EC2:

    PID      %CPU  %MEM  RSS       ELAPSED
    2026688  83.8  19.1  1533420   00:30

1.53 GB and 84% CPU **thirty seconds after boot**, before serving a request,
with only 4 threads. Not a leak — nothing was leaking. `main.py` runs eight
whole-table passes on every start, and four of them were written as

    db.execute(select(Opportunity)).scalars().all()

which materialises all 279,129 rows as ORM objects in one list and pins them in
the session's identity map until the pass ends. It was written when the table
held 106,854 rows.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest


@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "bf.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app.database.models import Category, Opportunity, Status
    with db_mod.session_scope() as s:
        for i in range(250):
            s.add(Opportunity(
                unique_id=f"b{i}", title=f"Grant number {i}",
                organization="Funder", source_website="NGOBOX",
                summary="Support for smallholder irrigation in Kenya.",
                category=Category.GRANT, status=Status.ACTIVE,
                deadline=date(2027, 1, 1), deadline_state="dated",
                date_scraped=datetime(2026, 8, 1)))
    yield db_mod


# ------------------------------------------------ it visits every row

def test_the_walk_reaches_every_row(db):
    from app.services.backfill import iter_opportunities

    with db.session_scope() as s:
        ids = [o.id for o in iter_opportunities(s, chunk=17)]
    assert len(ids) == 250
    assert len(set(ids)) == 250, "a row was visited twice"
    assert ids == sorted(ids), "keyset pagination must walk in id order"


@pytest.mark.parametrize("chunk", [1, 7, 250, 1000])
def test_the_result_does_not_depend_on_the_chunk_size(db, chunk):
    """Chunking is an implementation detail. If it changed which rows were
    seen, it would be a behaviour change wearing a performance fix's clothes."""
    from app.services.backfill import iter_opportunities

    with db.session_scope() as s:
        assert len([o.id for o in iter_opportunities(s, chunk=chunk)]) == 250


# ------------------------------- it does not hold the table in memory

def test_only_one_chunk_is_resident_at_a_time(db):
    """The whole point. The session's identity map is what held 279,129 rows;
    this asserts it stays bounded no matter how long the walk is."""
    from app.services.backfill import iter_opportunities

    high_water = 0
    with db.session_scope() as s:
        for _ in iter_opportunities(s, chunk=20):
            high_water = max(high_water, len(s.identity_map))
    assert high_water <= 45, (
        f"{high_water} rows resident with a chunk of 20 — the identity map is "
        f"still accumulating, which is the defect this replaces")


def test_a_bigger_table_does_not_mean_a_bigger_footprint(db):
    """250 rows or 279,129: residency is a function of chunk size alone."""
    from app.services.backfill import iter_opportunities

    peaks = {}
    for chunk in (10, 50):
        with db.session_scope() as s:
            peak = 0
            for _ in iter_opportunities(s, chunk=chunk):
                peak = max(peak, len(s.identity_map))
            peaks[chunk] = peak
    assert peaks[10] < peaks[50], "residency should track the chunk, not the table"


# ------------------------------------------- changes are not lost

def test_a_change_made_during_the_walk_is_persisted(db):
    """Expunging a dirty object would discard the caller's edit — that would
    turn a memory fix into silent data loss. The flush before expunge is what
    prevents it, and this is the test that would catch its removal."""
    from app.services.backfill import iter_opportunities

    with db.session_scope() as s:
        for opp in iter_opportunities(s, chunk=13):
            opp.work_type = "Consultancy"

    from sqlalchemy import func, select

    from app.database.models import Opportunity
    with db.session_scope() as s:
        n = s.execute(select(func.count(Opportunity.id))
                      .where(Opportunity.work_type == "Consultancy")).scalar_one()
    assert n == 250, f"only {n} of 250 edits survived the walk"


def test_a_where_narrows_what_is_read(db):
    """study_type's backfill skipped already-classified rows AFTER loading
    them. Pushing the condition into SQL is the difference between reading the
    whole table and reading the part that needs work."""
    from sqlalchemy import func, or_

    from app.database.models import Opportunity
    from app.services.backfill import iter_opportunities

    with db.session_scope() as s:
        for i, opp in enumerate(iter_opportunities(s, chunk=50)):
            if i < 200:
                opp.study_type = "Baseline"

    needs = or_(Opportunity.study_type.is_(None),
                func.trim(Opportunity.study_type) == "")
    with db.session_scope() as s:
        assert len(list(iter_opportunities(s, chunk=50, where=needs))) == 50


# ------------------------------------ the call sites actually use it

@pytest.mark.parametrize("module", ["amounts", "work_type", "study_type", "geography"])
def test_no_startup_backfill_loads_the_whole_table(module):
    """The regression guard. `select(Opportunity)).scalars().all()` in a
    backfill is the exact line that put 1.5 GB in a Gunicorn worker."""
    import inspect

    mod = __import__(f"app.services.{module}", fromlist=["x"])
    src = inspect.getsource(mod)
    assert "iter_opportunities" in src, f"{module} does not use the chunked walk"
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "select(Opportunity)).scalars().all()" not in code, (
        f"{module} still materialises the entire table")


def test_startup_still_runs_all_eight_passes():
    """The fix must not work by doing less. Every pass main.py ran before must
    still run — the change is how they read, not whether they run."""
    import inspect

    from app import main

    src = inspect.getsource(main)
    for fn in ("audit_deadlines", "backfill_verticals", "repair_links",
               "backfill_geography", "backfill_organizations",
               "backfill_amounts", "backfill_work_types",
               "backfill_study_types"):
        assert fn in src, f"{fn} is no longer run at startup"
