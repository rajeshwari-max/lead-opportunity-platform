"""A walk that cannot end is the bug; these are the three ways it now ends.

The 2026-08-26 DevelopmentAid run made 800 searches to save 55,013 of 779,856
records found. Each test below is one of the shapes that run could have taken,
and asserts the walk stops AND says which bound stopped it.
"""
from __future__ import annotations

import pytest

from app.services.walk_budget import WalkBudget


class FakeClock:
    """Time only moves when a test moves it."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def budget(searches=600, duration=1800, records=20000, clock=None):
    return WalkBudget(searches, duration, records, clock=clock)


# ------------------------------------------------------------ the search cap

def test_a_fresh_budget_does_not_stop_a_walk():
    assert budget().exceeded(0) == ""


def test_the_search_cap_stops_the_walk_at_the_cap_not_after_it():
    b = budget(searches=3)
    for _ in range(3):
        assert b.exceeded(0) == "", "stopped before spending the budget"
        b.spend()
    assert b.exceeded(0), "spent the whole budget and kept going"


def test_the_search_cap_names_itself():
    b = budget(searches=3)
    b.spend(3)
    assert "3-search cap" in b.exceeded(0)


# -------------------------------------------------------------- the time cap

def test_the_time_cap_stops_a_walk_that_is_slow_rather_than_large():
    """A section can be small and still take hours if each probe is slow.
    The search cap never fires; the walk still has to end."""
    clock = FakeClock()
    b = budget(duration=1800, clock=clock)
    b.spend(5)
    assert b.exceeded(10) == ""
    clock.advance(1800)
    assert "1800s time cap" in b.exceeded(10)


def test_the_time_cap_is_measured_from_construction_not_from_first_check():
    clock = FakeClock()
    b = budget(duration=600, clock=clock)
    clock.advance(599)
    assert b.exceeded(0) == ""
    clock.advance(1)
    assert b.bounded is False, "a check that returns empty must not latch"
    assert b.exceeded(0)


# ------------------------------------------------------------ the record cap

def test_the_record_cap_stops_a_section_that_is_unexpectedly_enormous():
    """This is the filter-silently-stopped-applying case: few searches, little
    time, and the whole archive arriving anyway."""
    b = budget(records=20000)
    b.spend(2)
    assert b.exceeded(19_999) == ""
    assert "20,000-record cap" in b.exceeded(20_000)


def test_the_record_count_is_supplied_by_the_walk_not_counted_here():
    """The walk owns the dedup set. The budget knowing how records are
    identified would couple it to a scraper's key scheme."""
    b = budget(records=100)
    assert b.exceeded(0) == ""
    assert b.exceeded(500)


# ------------------------------------------------- which cap gets the blame

def test_the_first_bound_to_bind_is_the_one_reported():
    """A recursive walk asks again on the way out. Whichever cap was true at
    the end is not the one that stopped the run."""
    clock = FakeClock()
    b = budget(searches=3, duration=600, records=100, clock=clock)
    b.spend(3)
    assert "3-search cap" in b.exceeded(0)
    # Now every other cap is also blown.
    clock.advance(10_000)
    assert "3-search cap" in b.exceeded(999_999), "the reported cap changed"
    assert "3-search cap" in b.reason


def test_reason_is_empty_until_something_actually_binds():
    b = budget()
    b.spend(5)
    b.exceeded(10)
    assert b.reason == ""
    assert b.bounded is False


def test_bounded_is_the_signal_a_run_was_partial():
    """The log line that distinguishes 'covered 15.5% and stopped at a cap'
    from 'covered 15.5% and that is all there is' reads this."""
    b = budget(searches=1)
    b.spend()
    b.exceeded(0)
    assert b.bounded


# -------------------------------------------------------- misconfiguration

@pytest.mark.parametrize("kwargs,attr,floor", [
    ({"searches": 0}, "searches_cap", 1),
    ({"searches": -5}, "searches_cap", 1),
    ({"duration": 0}, "duration_s", 60),
    ({"duration": 5}, "duration_s", 60),
    ({"records": 0}, "record_cap", 100),
    ({"records": -1}, "record_cap", 100),
])
def test_a_nonsense_cap_bounds_tighter_but_never_crashes(kwargs, attr, floor):
    """A bad env var should make a run short, not make it fail at 3am."""
    assert getattr(budget(**kwargs), attr) == floor


def test_a_zero_duration_still_gives_the_walk_time_to_do_something():
    """max(60, ...) exists so LOP_DEVAID_MAX_DURATION_S=0 does not produce a
    run that stops before its first probe and reports 0% coverage."""
    clock = FakeClock()
    b = budget(duration=0, clock=clock)
    assert b.exceeded(0) == ""
    clock.advance(59)
    assert b.exceeded(0) == ""
    clock.advance(1)
    assert b.exceeded(0)


def test_caps_are_independent_of_each_other():
    """Spending the search budget must not consume the record allowance."""
    b = budget(searches=2, records=20000)
    b.spend(2)
    assert b.searches_cap == 2 and b.record_cap == 20000
