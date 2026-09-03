"""A row already stored without a deadline, when the scrape can finally read one.

The gap this closes
-------------------
The deadline is deliberately not part of `unique_id` — it is an attribute of an
opportunity, not part of what makes it that opportunity, and putting it in the
key forked a record every time a source corrected a date.

The consequence nobody had followed through: a row whose date we could NOT read
before and CAN read now arrives at ingest as a duplicate. The duplicate branch
bumped `last_seen` and dropped everything else, so the new date was discarded.

UNDP Procurement made that concrete. Fixing extraction took it from 0.4% of
rows carrying a deadline to 100% — and not one of the 1,274 rows already stored
would have gained a date, because every one of them comes back as a duplicate.
The dashboard would have looked identical after the fix, and the 550 notices it
was meant to surface would have stayed invisible.

The asymmetry is the point
--------------------------
Filling an empty deadline is safe. Overwriting a stored one is not: a source
that stops printing a date, or a page that renders it late, would silently
erase a good deadline — and a row whose deadline disappears stops being
expirable, which is the immortality bug this system has fixed twice already.
So this only ever fills a gap, and `test_it_never_overwrites_a_deadline_that_is_already_there`
is the test that matters most in this file.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest

from app.schemas.opportunity import RawOpportunity


def raw(title: str, url: str, deadline_raw: str = "") -> RawOpportunity:
    return RawOpportunity(
        title=title, organization="UNDP", source_website="UNDP Procurement",
        website="https://procurement-notices.undp.org", summary="",
        opportunity_url=url, deadline_raw=deadline_raw,
    )


def ingest(db_mod, batch):
    from app.services.scraper_manager import ScraperManager

    return ScraperManager()._ingest(batch, source_key="undp_procurement")


def stored(db_mod, url_fragment: str):
    from sqlalchemy import select

    from app.database.models import Opportunity
    with db_mod.session_scope() as db:
        return db.execute(
            select(Opportunity).where(
                Opportunity.opportunity_url.like(f"%{url_fragment}%"))
        ).scalars().first()


# --------------------------------------------------------------- filling

def test_a_stored_row_with_no_deadline_gains_one(db_mod):
    """The UNDP case, end to end."""
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=1"
    ingest(db_mod, [raw("Request for Proposals: Borehole Rehabilitation", url)])
    row = stored(db_mod, "notice_id=1")
    assert row is not None and row.deadline is None, "starts undated"

    # The same notice, scraped again after the extraction fix.
    ingest(db_mod, [raw("Request for Proposals: Borehole Rehabilitation", url,
                        deadline_raw="21-Feb-27")])
    row = stored(db_mod, "notice_id=1")
    assert row.deadline == date(2027, 2, 21)


def test_the_supporting_columns_are_written_too(db_mod):
    """A deadline with a NULL state reads as UNKNOWN to the actionable rule, so
    filling the date alone would leave the row exactly as hidden as before."""
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=2"
    ingest(db_mod, [raw("Invitation to Bid: Cold Chain Equipment", url)])
    ingest(db_mod, [raw("Invitation to Bid: Cold Chain Equipment", url,
                        deadline_raw="09-Mar-27")])
    row = stored(db_mod, "notice_id=2")
    assert row.deadline == date(2027, 3, 9)
    assert row.deadline_state, "state must be set, or the row stays invisible"
    assert row.deadline_raw == "09-Mar-27"
    assert row.deadline_checked_at is not None


def test_it_is_still_counted_as_a_duplicate_not_as_a_new_row(db_mod):
    """Repairing a row is not the same as finding one. Counting it as new would
    overstate what the scrape discovered."""
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=3"
    ingest(db_mod, [raw("Request for Proposals: Gender Adviser", url)])
    saved, expired, dupes, spam, rejected = ingest(
        db_mod, [raw("Request for Proposals: Gender Adviser", url,
                     deadline_raw="15-Apr-27")])
    assert saved == 0
    assert dupes == 1


# ------------------------------------------------------- and NOT overwriting

def test_it_never_overwrites_a_deadline_that_is_already_there(db_mod):
    """The load-bearing test.

    A source that stops printing a date, or a page that renders it late, would
    otherwise erase a good deadline — and a row whose deadline disappears stops
    being expirable, which is the immortality bug this system has already fixed
    twice.
    """
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=4"
    ingest(db_mod, [raw("Invitation to Bid: Supply of Laboratory Reagents", url,
                        deadline_raw="21-Feb-27")])
    assert stored(db_mod, "notice_id=4").deadline == date(2027, 2, 21)

    # The same notice comes back with NO date at all.
    ingest(db_mod, [raw("Invitation to Bid: Supply of Laboratory Reagents", url)])
    assert stored(db_mod, "notice_id=4").deadline == date(2027, 2, 21), \
        "a missing date must never erase a stored one"


def test_a_different_date_does_not_replace_the_stored_one_either(db_mod):
    """Correcting an existing date is a different decision with a different
    risk. This change deliberately does not make it."""
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=5"
    ingest(db_mod, [raw("Request for Proposals: Baseline Survey", url,
                        deadline_raw="21-Feb-27")])
    ingest(db_mod, [raw("Request for Proposals: Baseline Survey", url,
                        deadline_raw="30-Jun-27")])
    assert stored(db_mod, "notice_id=5").deadline == date(2027, 2, 21)


def test_last_seen_is_still_bumped_on_every_duplicate(db_mod):
    """The behaviour that was already there, and the reason the duplicate
    branch exists: it is what lets an undated row be retired once the source
    stops returning it."""
    url = "https://procurement-notices.undp.org/view_notice.cfm?notice_id=6"
    ingest(db_mod, [raw("Request for Proposals: Rehabilitation of District Offices", url)])
    before = stored(db_mod, "notice_id=6").last_seen
    ingest(db_mod, [raw("Request for Proposals: Rehabilitation of District Offices", url)])
    assert stored(db_mod, "notice_id=6").last_seen >= before


# ------------------------------------------------------------------ fixture

@pytest.fixture()
def db_mod(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "refresh.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    return db_module
