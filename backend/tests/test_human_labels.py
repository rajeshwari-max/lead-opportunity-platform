"""A person's label must survive the next restart.

`backfill_verticals()` re-classifies every row at every startup and overwrites
`verticals` wherever the keyword rules now disagree. That was correct while
nothing could hand-edit a row. The moment a review UI can set a vertical, the
same code silently undoes a person's work at the next restart — and the only
thing they learn is that correcting rows does not stick.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "labels.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")

    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)

    db_mod.init_db()
    yield db_mod


def _add(db_mod, **kw):
    from app.database.models import Opportunity, Status

    defaults = dict(
        unique_id=f"u-{kw.get('title','x')}",
        title="Health Systems Strengthening Consultancy",
        organization="Some Funder",
        source_website="Some Funder",
        summary="Support to district health systems.",
        status=Status.ACTIVE,
        verticals="",
    )
    defaults.update(kw)
    with db_mod.session_scope() as s:
        row = Opportunity(**defaults)
        s.add(row)
        s.flush()
        return row.id


def _get(db_mod, row_id):
    from sqlalchemy import select

    from app.database.models import Opportunity
    with db_mod.session_scope() as s:
        return s.execute(
            select(Opportunity).where(Opportunity.id == row_id)
        ).scalar_one()


# ------------------------------------------------------- the migration is safe

def test_the_new_columns_exist_after_migration(db):
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(db.engine).get_columns("opportunities")}
    assert {"verticals_source", "verticals_labeled_by",
            "verticals_labeled_at"} <= cols


def test_every_pre_existing_row_reads_as_machine_classified(db):
    """Nothing could hand-edit a row before this column existed, so NULL meaning
    'auto' is a fact rather than an assumption."""
    from app.services.verticals import is_human_labeled

    row_id = _add(db, unique_id="pre-existing")
    assert not is_human_labeled(_get(db, row_id))


# ------------------------------------------------------- the backfill protects

def test_the_backfill_still_corrects_a_machine_labelled_row(db):
    """The protection must not work by disabling the backfill."""
    from app.services.verticals import backfill_verticals

    row_id = _add(db, unique_id="auto-row", verticals="")
    backfill_verticals()
    assert "Health" in (_get(db, row_id).verticals or "")


def test_the_backfill_leaves_a_human_labelled_row_alone(db):
    """The whole point. The classifier would say Health; the person said
    Worker Wellbeing, and the person wins."""
    from app.services.verticals import backfill_verticals

    row_id = _add(db, unique_id="human-row",
                  verticals="Worker Wellbeing",
                  verticals_source="human",
                  verticals_labeled_by="someone@example.org",
                  verticals_labeled_at=datetime.now(timezone.utc))
    backfill_verticals()
    assert _get(db, row_id).verticals == "Worker Wellbeing"


def test_a_human_label_survives_repeated_restarts(db):
    """One restart is not the test — a label that decays after three is still
    a label that does not stick."""
    from app.services.verticals import backfill_verticals

    row_id = _add(db, unique_id="persist", verticals="Innovative Finance",
                  verticals_source="human")
    for _ in range(3):
        backfill_verticals()
    assert _get(db, row_id).verticals == "Innovative Finance"


def test_a_human_can_clear_every_vertical_and_it_stays_cleared(db):
    """Deliberately empty is a judgement too: 'this belongs to none of our six'.
    Treating empty as unlabelled would re-tag it on the next restart, which is
    exactly the correction being overwritten."""
    from app.services.verticals import backfill_verticals

    row_id = _add(db, unique_id="deliberately-empty", verticals="",
                  verticals_source="human")
    backfill_verticals()
    assert (_get(db, row_id).verticals or "") == ""


def test_the_backfill_reports_how_many_rows_it_protected(db, caplog):
    """The number is how anyone confirms human labelling is being honoured
    without opening the database."""
    import logging

    from app.services.verticals import backfill_verticals

    _add(db, unique_id="p1", verticals="Health", verticals_source="human")
    _add(db, unique_id="p2", verticals="Health", verticals_source="human")
    with caplog.at_level(logging.INFO, logger="scraper"):
        backfill_verticals()
    # getMessage() renders the %s args; r.message is the raw format string.
    assert any("left 2 human-labelled" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------- the predicate

@pytest.mark.parametrize("value,expected", [
    (None, False),
    ("", False),
    ("auto", False),
    ("human", True),
])
def test_is_human_labeled_reads_the_source_column(value, expected):
    from app.services.verticals import is_human_labeled

    class Row:
        verticals_source = value

    assert is_human_labeled(Row()) is expected


# ------------------------------------- the audit must describe the real rules

def test_explain_verticals_assigns_exactly_what_classify_verticals_assigns():
    """`classify_verticals` stops scoring once a vertical crosses the
    threshold; `explain_verticals` keeps going to collect every matching
    pattern. If that difference ever changed the ASSIGNMENT, the precision
    audit would be reporting the reasons for tags the pipeline never applied —
    and every conclusion drawn from it would be about a classifier that does
    not exist.

    Randomised rather than hand-picked: the divergence would live in the
    interaction between the early break and the score, which is exactly the
    place chosen examples do not probe.
    """
    import random

    from app.services.verticals import classify_verticals, explain_verticals

    words = ["agriculture", "health", "evaluation", "research", "climate",
             "finance", "worker", "grant", "tender", "consultancy", "water",
             "farmers", "training", "nutrition", "solar", "wellbeing",
             "impact", "survey", "community", "policy", "wind", "fund"]
    rng = random.Random(7)
    for _ in range(2000):
        title = " ".join(rng.choices(words, k=rng.randint(2, 8))).title()
        body = " ".join(rng.choices(words, k=rng.randint(0, 40)))
        assert classify_verticals(title, body) == list(
            explain_verticals(title, body).keys()), (title, body)
