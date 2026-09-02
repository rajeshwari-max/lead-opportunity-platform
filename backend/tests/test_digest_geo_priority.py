"""What a team member reads FIRST — India, then South Asia, then the rest.

The routing tests next door (test_geo_routing.py) prove the digest sends the
right rows. These prove it sends them in the right ORDER, which is a different
failure and was the one still open: a member whose geography is unset, or set
wide, received a correctly-filtered list sorted by closing date, so a Peruvian
call closing on Tuesday sat above the Indian one closing on Friday. Nothing was
wrong with the contents and the email was still hard to use.

The load-bearing test in this file is `test_nothing_is_dropped_by_ordering`.
Everything else here can be wrong and produce a badly-sorted email; that one
going wrong produces a missing opportunity, which nobody would notice.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime

import pytest

from app.services import geo_priority


# ------------------------------------------------------------------ the tiers

def test_country_outranks_region():
    """"India" is more specific than "South Asia" and belongs above it."""
    p = geo_priority.Priority(countries=("India",), regions=("South Asia",))
    assert p.tier(country="India", region="South Asia") < p.tier(
        country="Bangladesh", region="South Asia")


def test_configured_order_is_the_order():
    p = geo_priority.Priority(countries=("India", "Nepal"), regions=())
    assert p.tier(country="India") < p.tier(country="Nepal")


def test_global_sits_above_the_other_regions():
    """A worldwide call is open to an Indian bidder by definition. A call
    scoped to Latin America is not, however good the keyword match."""
    p = geo_priority.load()
    assert p.tier(region="Global") < p.tier(region="Latin America")


def test_anything_unmatched_sorts_last_but_is_still_ranked():
    p = geo_priority.load()
    assert p.tier(country="Peru", region="Latin America") == geo_priority.UNRANKED


def test_matching_is_case_insensitive():
    p = geo_priority.Priority(countries=("India",), regions=())
    assert p.tier(country="  india ") == 0


# --------------------------------------------------- free text, carefully

def test_location_is_consulted_when_country_is_empty():
    """Several sources fill `location` and leave `country` blank —
    DevelopmentAid stores "Malawi, Zambia" there. Ignoring it would drop those
    rows to the bottom of every digest despite naming the home country."""
    p = geo_priority.Priority(countries=("India",), regions=())
    assert p.tier(location="India, Nepal") == 0


def test_indiana_is_not_india():
    """The whole reason `_mentions` exists instead of a bare `in`."""
    p = geo_priority.Priority(countries=("India",), regions=())
    assert p.tier(location="Indiana, United States") == geo_priority.UNRANKED


def test_structured_country_wins_over_free_text():
    """`location` is loose, so it must never override a column that was
    actually parsed. A row explicitly marked Peru is Peru, whatever prose the
    location field happens to contain."""
    p = geo_priority.Priority(countries=("Peru", "India"), regions=())
    assert p.tier(country="Peru", location="lessons from India") == 0


# --------------------------------------------------------- the digest order

def test_india_comes_before_south_asia_before_global_before_the_rest(db):
    from app.services.matching_service import MatchingService

    _rows(db)
    got = MatchingService(db).matches_for(_member(db, "unset"))
    assert [o.country or o.region for o in got] == [
        "India", "India", "Bangladesh", "Global", "Peru",
    ]


def test_the_nearer_deadline_no_longer_wins_across_tiers(db):
    """The behaviour this replaces. `Lima Water` closes first of everything and
    used to head the email; it is now last, under the two Indian rows that
    close later."""
    from app.services.matching_service import MatchingService

    _rows(db)
    got = MatchingService(db).matches_for(_member(db, "d"))
    assert got[0].title != "Lima Water Resilience"
    assert got[-1].title == "Lima Water Resilience"
    assert got[-1].deadline < got[0].deadline, "and it really does close sooner"


def test_deadline_still_orders_within_a_tier(db):
    """Tiers group; they do not replace the sort inside a group."""
    from app.services.matching_service import MatchingService

    _rows(db)
    got = [o for o in MatchingService(db).matches_for(_member(db, "w"))
           if o.country == "India"]
    assert [o.title for o in got] == ["Bihar Water Supply", "Delhi Health Systems"]


def test_relevance_still_orders_within_a_tier(db):
    """With keywords the inner sort is the relevance rank, not the deadline.
    The tier sort must be stable so that ranking survives it intact."""
    from app.services.matching_service import MatchingService

    _rows(db)
    got = MatchingService(db).matches_for(_member(db, "kw", keywords="health"))
    india = [o.title for o in got if o.country == "India"]
    # Delhi has the keyword in its TITLE and Bihar only in its summary, so
    # relevance puts Delhi first — even though Bihar closes ten days sooner.
    # If the tier sort were not stable it would fall back to insertion order
    # and Bihar would lead.
    assert india == ["Delhi Health Systems", "Bihar Water Supply"]
    assert got[:2] == [o for o in got if o.country == "India"], \
        "and both Indian rows still lead the whole digest"


def test_nothing_is_dropped_by_ordering(db):
    """The one that matters. Everything here already passed the member's own
    geography filter, so re-filtering would silently override a choice they
    made — and a missing opportunity looks identical to one that was never
    scraped."""
    from app.services.matching_service import MatchingService

    _rows(db)
    unset = MatchingService(db).matches_for(_member(db, "count"))
    assert len(unset) == len(ROWS)
    assert {o.title for o in unset} == {r[0] for r in ROWS}


def test_a_limit_now_cuts_the_far_rows_not_the_home_ones(db):
    """A capped digest keeps what the reader can act on. Before, the cap fell
    wherever the deadline sort happened to put the line."""
    from app.services.matching_service import MatchingService

    _rows(db)
    got = MatchingService(db).matches_for(_member(db, "cap"), limit=2)
    assert [o.country for o in got] == ["India", "India"]


def test_the_priority_is_configuration_not_code(db, monkeypatch):
    """A team in Nairobi changes .env, not this module."""
    from app.core.config import settings
    from app.services.matching_service import MatchingService

    _rows(db)
    monkeypatch.setattr(settings, "digest_priority_countries", "Peru", raising=False)
    monkeypatch.setattr(settings, "digest_priority_regions", "Latin America",
                        raising=False)
    got = MatchingService(db).matches_for(_member(db, "nbo"))
    assert got[0].title == "Lima Water Resilience"


# ------------------------------------------------------- the email sections

def test_the_email_gives_india_its_own_section_above_south_asia():
    """India, Bangladesh and Nepal used to share one "South Asia" block. Each
    block is capped, so an Indian call could be pushed under the cap by
    Nepalese ones and never appear at all."""
    from app.services.email_service import _group_for_digest

    groups = _group_for_digest(_plain_rows())
    assert [name for name, _ in groups][:2] == ["India", "South Asia"]
    assert [o.country for o in dict(groups)["India"]] == ["India", "India"]
    assert "India" not in [o.country for o in dict(groups)["South Asia"]]


def test_the_email_sections_follow_the_same_settings_as_the_sort():
    """Two hard-coded orderings would drift, and the first symptom would be an
    email whose sections disagree with the order the matcher chose."""
    from app.services.email_service import _region_order

    p = geo_priority.Priority(countries=(), regions=("Global", "South Asia"))
    order = _region_order(p)
    assert order[:2] == ["Global", "South Asia"]
    assert order.count("Global") == 1, "the tail must not repeat a home region"
    assert "Africa" in order, "and the rest of the world is still listed"


def test_every_opportunity_still_reaches_a_section():
    from app.services.email_service import _group_for_digest

    rows = _plain_rows()
    grouped = sum(len(items) for _, items in _group_for_digest(rows))
    assert grouped == len(rows)


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "geoprio.db")
    monkeypatch.setenv("LOP_DATABASE_URL", f"sqlite:///{path}")
    import importlib

    from app.core import config as config_mod
    importlib.reload(config_mod)
    from app.database import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    with db_mod.session_scope() as s:
        yield s


# Deliberately built so that deadline order and geography order DISAGREE: the
# Peruvian row closes first and the Indian rows close last. A test suite whose
# fixtures happen to agree on both would pass with the ordering removed.
ROWS = [
    # title,                     country,      region,          deadline
    ("Lima Water Resilience",    "Peru",       "Latin America", date(2026, 10, 1)),
    ("Open Health Challenge",    "",           "Global",        date(2026, 10, 10)),
    ("Dhaka Health Systems",     "Bangladesh", "South Asia",    date(2026, 10, 20)),
    ("Bihar Water Supply",       "India",      "South Asia",    date(2026, 11, 1)),
    ("Delhi Health Systems",     "India",      "South Asia",    date(2026, 11, 10)),
]


def _rows(session):
    from app.database.models import Opportunity, Status

    for i, (title, country, region, deadline) in enumerate(ROWS):
        session.add(Opportunity(
            unique_id=f"gp{i}", title=title, organization="F",
            source_website="F", summary="health and water programme",
            # A vertical every row shares, so the keyword test below turns on
            # the title alone — which is the field whose weight it is checking.
            vertical="Health", country=country, region=region,
            status=Status.ACTIVE,
            deadline=deadline, deadline_state="dated",
            date_scraped=datetime(2026, 8, 1)))
    session.flush()


def _plain_rows():
    """Detached objects — the grouping helper never touches the database."""
    from app.database.models import Opportunity

    return [
        Opportunity(title=t, country=c, region=r, deadline=d)
        for t, c, r, d in ROWS
    ] + [Opportunity(title="Kathmandu Schools", country="Nepal",
                     region="South Asia", deadline=date(2026, 10, 5))]


def _member(session, name, **kw):
    from app.database.models import TeamMember

    fields = {"keywords": "", "categories": "", "verticals": "",
              "auto_send": False, "active": True}
    fields.update(kw)
    m = TeamMember(name=name, email=f"{name}@x.org", **fields)
    session.add(m)
    session.flush()
    return m
