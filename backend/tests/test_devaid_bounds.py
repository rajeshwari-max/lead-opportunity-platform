"""The archive pass is opt-in, and the caps are a scheduled run's ceiling.

Measured on the live database, 2026-08-29:

    DevelopmentAid found 779,856 records and saved 55,013 — a 93% discard
    rate — and 90,551 of the platform's 106,854 opportunities are expired.

The archive walk is where that comes from. A platform for opportunities
someone can still respond to should not spend its scheduled runs on listings
that closed years ago. These tests pin the defaults that make that true, so
the archive cannot come back on by accident.
"""
from __future__ import annotations

import re

from app.core.config import Settings, settings
from app.services.walk_budget import WalkBudget


# ------------------------------------------------- the archive is opt-in

def test_a_scheduled_run_does_not_walk_the_historical_archive():
    """The single most expensive default in the system. If this flips, a
    scheduled run silently becomes a backfill again."""
    assert settings.devaid_include_archive is False
    assert Settings.model_fields["devaid_include_archive"].default is False


def test_the_archive_remains_reachable_as_a_deliberate_backfill():
    """Removing the capability would be a different decision from switching
    the default off. The brief asked for separate, not gone."""
    assert "devaid_include_archive" in Settings.model_fields


# -------------------------------------------------------- the cap defaults

def test_every_cap_has_a_finite_default():
    for name in ("devaid_max_slices", "devaid_max_duration_s",
                 "devaid_max_records"):
        value = getattr(settings, name)
        assert isinstance(value, int) and value > 0, f"{name} is not a bound"


def test_the_search_cap_is_far_below_the_run_that_caused_this():
    """The 2026-08-26 run made 800 searches and lost coverage doing it — the
    budget bisection was re-reading the same 102 rows at every node. 25,000
    was the old ceiling, which is not a ceiling."""
    assert settings.devaid_max_slices <= 1000
    assert settings.devaid_max_slices >= 100, (
        "too tight to cover a real section; coverage would collapse"
    )


def test_one_section_cannot_occupy_an_entire_night():
    """Sections run in sequence. An unbounded one starves every source after
    it, which is how 47 of 75 producing sources came to be 21+ days stale."""
    assert settings.devaid_max_duration_s <= 3600


def test_the_record_cap_is_below_what_the_archive_walk_returned():
    """55,013 rows were saved from a single archive-inclusive crawl. A cap
    above that is not a cap on the behaviour being fixed."""
    assert settings.devaid_max_records < 55_013


# -------------------------------- the scraper actually uses the shared budget

def test_the_walk_is_bounded_by_the_shared_budget_object():
    """Guards against the caps drifting back into hand-rolled arithmetic
    inside the walk, where they are unreachable by any test."""
    src = _walk_source()
    assert "WalkBudget(" in src, "the walk no longer constructs a WalkBudget"
    assert "budget.exceeded(" in src, "the walk no longer consults its budget"


def test_the_walk_wires_all_three_settings_into_the_budget():
    src = _walk_source()
    for name in ("devaid_max_slices", "devaid_max_duration_s",
                 "devaid_max_records"):
        assert f"settings.{name}" in src, f"{name} is configured but unused"


def test_the_partial_coverage_warning_names_the_cap_that_bound():
    """'15.5% covered, completed' is the log line that hid a regression for
    three days. The reason has to reach the message."""
    src = _walk_source()
    assert "PARTIAL COVERAGE" in src
    assert "budget.reason" in src


def test_the_archive_pass_is_gated_inside_the_walk_not_only_in_config():
    src = _walk_source()
    assert "settings.devaid_include_archive" in src


def _walk_source() -> str:
    """The text of _walk_via_api, read without importing the scraper package.

    developmentaid.py pulls in Playwright and the whole scraper registry;
    importing it to read one method would make this test a dependency check.
    """
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1]
            / "app" / "scrapers" / "developmentaid.py")
    text = path.read_text(encoding="utf-8")
    start = text.index("def _walk_via_api")
    # Up to the next method at the same indentation.
    rest = text[start:]
    nxt = re.search(r"\n    def ", rest[10:])
    return rest[:nxt.start() + 10] if nxt else rest


# ------------------------------------------------ the budget behaves in situ

def test_a_realistically_configured_budget_stops_a_runaway_section():
    """The failure mode end to end: a section that keeps returning rows.
    With production settings it stops, and it says why."""
    b = WalkBudget(settings.devaid_max_slices, settings.devaid_max_duration_s,
                   settings.devaid_max_records)
    records = 0
    for _ in range(100_000):
        if b.exceeded(records):
            break
        b.spend()
        records += 500
    else:
        raise AssertionError("the walk never ended")
    assert b.bounded and b.reason
    assert b.searches <= settings.devaid_max_slices
