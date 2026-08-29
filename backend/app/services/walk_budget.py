"""How long a source's walk is allowed to run, and why it stopped.

A scraper that partitions a search space has no natural end. DevelopmentAid's
2026-08-26 run is the shape of the problem: it made 800 searches, found 779,856
records and saved 55,013 of them, because nothing told it when enough was
enough. A bound has to be part of the walk, not something an operator notices
afterwards.

Three caps, not one. A source can be slow without being large, large without
being slow, or unexpectedly enormous because a filter silently stopped applying
— and a single cap only catches one of those.

The second job matters as much as the first: when a run stops early, the log
has to say *which* bound stopped it. "15.5% covered, completed" is the entry
that hid the coverage regression for three days. "15.5% covered, stopped at the
600-search cap" is a fact someone can act on.
"""
from __future__ import annotations

import time


class WalkBudget:
    """Three independent caps on one walk. Whichever binds first ends it.

    `reason` latches the FIRST bound that bound. A recursive walk keeps asking
    after it has already stopped, so without latching the message would name
    whichever cap happened to be true when the recursion unwound rather than
    the one that actually ended the run.

    `clock` is injectable so the time cap can be tested for what it does rather
    than by re-implementing its arithmetic in a test — which would only verify
    the test.
    """

    __slots__ = ("searches_cap", "record_cap", "duration_s", "deadline_at",
                 "searches", "reason", "_clock")

    # Floors, not validation errors. A misconfigured cap should bound a run
    # tighter than someone intended; it should never turn a scheduled run into
    # a crash at 3am.
    MIN_SEARCHES = 1
    MIN_DURATION_S = 60
    MIN_RECORDS = 100

    def __init__(self, searches_cap: int, duration_s: int, record_cap: int,
                 clock=None):
        self._clock = clock or time.monotonic
        self.searches_cap = max(self.MIN_SEARCHES, int(searches_cap))
        self.duration_s = max(self.MIN_DURATION_S, int(duration_s))
        self.record_cap = max(self.MIN_RECORDS, int(record_cap))
        self.deadline_at = self._clock() + self.duration_s
        self.searches = 0
        self.reason = ""

    def spend(self, n: int = 1) -> None:
        """Record searches actually issued against the source."""
        self.searches += n

    def exceeded(self, records: int) -> str:
        """Which cap has bound, if any. Empty string means keep going.

        Records are passed in rather than counted here because the walk owns
        the dedup set; the budget should not need to know how records are
        identified in order to know how many there are.
        """
        if self.searches >= self.searches_cap:
            hit = f"the {self.searches_cap}-search cap"
        elif self._clock() >= self.deadline_at:
            hit = f"the {self.duration_s}s time cap"
        elif records >= self.record_cap:
            hit = f"the {self.record_cap:,}-record cap"
        else:
            return ""
        if not self.reason:
            self.reason = hit
        return hit

    @property
    def bounded(self) -> bool:
        """True once any cap has stopped the walk."""
        return bool(self.reason)
