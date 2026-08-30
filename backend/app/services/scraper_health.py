"""Is each source actually working? Answered from evidence, not from status.

The problem this replaces
-------------------------
The 2026-08-29 baseline of the live database:

    916 runs: 792 completed, 106 running, 18 stopped, 0 failed — ever
    16 sources have never produced a row, across 127 runs, all "completed"
    47 of 75 producing sources last saved something 21+ days ago

A source that fetches nothing and saves nothing recorded exactly the same word
as one that worked. "Completed" meant "the function returned", which is not a
health signal, and with no failures ever recorded there was nothing to alert
on.

`ScrapeRun` now carries the evidence — outcome, error_code, http status, final
URL, duration, structure signature. This turns that into the two questions
someone actually asks: *which sources are broken*, and *how long has each been
broken*.

Staleness is deliberately measured from the last row SAVED, not the last run
attempted. A source that runs nightly and has saved nothing since July is
broken, and a "last run: today" reading would hide exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import Opportunity, ScrapeRun
from app.services.scrape_outcome import Outcome

# How many consecutive unhealthy runs before a source is called failing.
# One bad run is a bad night — a site was down, a page moved back. Three is a
# pattern, and the number is configurable because "how much flakiness is
# normal" is a property of the sources, not of this code.
DEFAULT_FAILURE_STREAK = 3

# Past this, a producing source is stale regardless of what its runs say.
DEFAULT_STALE_DAYS = 21

# Outcomes that mean the run did not do its job. CONFIRMED_EMPTY is absent on
# purpose: a source that proved it has nothing to list is working correctly,
# and treating it as broken would train people to ignore the alert.
UNHEALTHY = {
    Outcome.NO_FETCH, Outcome.PARSE_ZERO, Outcome.STRUCTURE_CHANGED,
    Outcome.AUTH_REQUIRED, Outcome.SESSION_EXPIRED, Outcome.BLOCKED,
    Outcome.TIMED_OUT, Outcome.CRASHED, Outcome.STALE_RUN_RECOVERED,
}

# Not unhealthy, and each for its own reason:
#   SUCCESS_WITH_RESULTS / SUCCESS_NO_NEW   the run worked
#   CONFIRMED_EMPTY   the source PROVED it has nothing to list; calling that
#                     broken would train people to ignore the alert
#   CANCELLED         somebody pressed stop. That is an operator action, not a
#                     defect, and counting it would make every manual stop look
#                     like a failing source the next morning.


@dataclass
class SourceHealth:
    source_key: str
    display_name: str
    # The brief's coverage-matrix columns. Read off the scraper class and the
    # manifest rather than maintained by hand, so a source that switches to an
    # API or gains a login is described correctly without anyone remembering.
    listing_url: str
    implementation: str          # generic | bespoke
    requires_login: bool
    fetch_mode: str              # http | browser
    pagination: str
    expected_types: str
    scope_confirmed: bool
    last_run_at: datetime | None
    last_outcome: str
    last_error_code: str
    last_error_message: str
    last_http_status: int | None
    runs_30d: int
    unhealthy_streak: int
    total_rows: int
    last_saved_at: datetime | None
    days_since_saved: int | None
    state: str            # ok | never_produced | stale | failing | unknown
    note: str

    def as_dict(self) -> dict:
        return {
            "source_key": self.source_key,
            "display_name": self.display_name,
            "listing_url": self.listing_url,
            "implementation": self.implementation,
            "requires_login": self.requires_login,
            "fetch_mode": self.fetch_mode,
            "pagination": self.pagination,
            "expected_types": self.expected_types,
            "scope_confirmed": self.scope_confirmed,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_outcome": self.last_outcome,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "last_http_status": self.last_http_status,
            "runs_30d": self.runs_30d,
            "unhealthy_streak": self.unhealthy_streak,
            "total_rows": self.total_rows,
            "last_saved_at": self.last_saved_at.isoformat() if self.last_saved_at else None,
            "days_since_saved": self.days_since_saved,
            "state": self.state,
            "note": self.note,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _streak(rows) -> int:
    """Consecutive unhealthy runs, newest first, stopping at the first good one."""
    n = 0
    for outcome in rows:
        try:
            parsed = Outcome(outcome) if outcome else None
        except ValueError:
            parsed = None
        if parsed is not None and parsed in UNHEALTHY:
            n += 1
        elif outcome:
            break        # a recorded healthy outcome ends the streak
        else:
            # A run from before outcomes were recorded says nothing either way.
            # Counting it as healthy would silently reset a real streak;
            # counting it as unhealthy would invent failures that were never
            # observed. Stopping is the only honest option.
            break
    return n


def source_health(
    db: Session,
    failure_streak: int = DEFAULT_FAILURE_STREAK,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> list[SourceHealth]:
    from app.scrapers.registry import SCRAPER_REGISTRY

    now = _now()
    since = now - timedelta(days=30)

    # Rows saved, keyed by the display name they were stored under. Renames
    # fragmented this history once already — 91 distinct source names for 85
    # sources — so both keys are looked up.
    saved = {
        (r[0] or ""): (int(r[1]), r[2])
        for r in db.execute(
            select(Opportunity.source_website, func.count(Opportunity.id),
                   func.max(Opportunity.date_scraped))
            .group_by(Opportunity.source_website)
        ).all()
    }

    from app.services.source_manifest import contract_for

    out: list[SourceHealth] = []
    for key, cls in sorted(SCRAPER_REGISTRY.items()):
        display = getattr(cls, "display_name", key)
        contract = contract_for(key, display)
        # Generic sources are subclasses of GenericListingScraper built at
        # import time from sources.json, so their __module__ is "abc" — the
        # module name says nothing. The ancestry does: 71 of the 85 inherit
        # from the configured class, 14 define their own parser.
        try:
            from app.scrapers.generic_listing import GenericListingScraper
            implementation = ("generic"
                              if issubclass(cls, GenericListingScraper)
                              else "bespoke")
        except Exception:                      # pragma: no cover - import guard
            implementation = "bespoke"
        listing = next((getattr(cls, a, "") for a in
                        ("start_url", "listing_url", "base_url")
                        if isinstance(getattr(cls, a, ""), str)
                        and getattr(cls, a, "").startswith("http")),
                       contract.listing_url or "")
        fetch_mode = "browser" if getattr(cls, "requires_js", False) else "http"
        # Which pagination contract this source is really on. Compared against
        # the class that DEFINES next_page rather than against an arbitrary
        # ancestor: every scraper inherits the method, so "does it have one"
        # answers nothing.
        from app.scrapers.base_scraper import BaseScraper as _Base
        if getattr(cls, "page_url", ""):
            pagination = "url template"
        elif cls.next_page is not _Base.next_page:
            owner = cls.next_page.__qualname__.split(".")[0]
            pagination = ("auto-detected" if owner == "GenericListingScraper"
                          else "custom next_page")
        else:
            pagination = "none"
        runs = db.execute(
            select(ScrapeRun)
            .where((ScrapeRun.source_key == key) | (ScrapeRun.source_website == display))
            .order_by(ScrapeRun.started_at.desc())
            .limit(25)
        ).scalars().all()

        last = runs[0] if runs else None
        runs_30d = sum(
            1 for r in runs
            if r.started_at and r.started_at >= since
        )
        streak = _streak([r.outcome for r in runs])

        total, last_saved = saved.get(display, saved.get(key, (0, None)))
        days = (now - last_saved).days if last_saved else None

        if total == 0:
            state = "never_produced"
            note = (f"{len(runs)} run(s) recorded and no row ever saved. Before "
                    f"outcomes were captured this looked identical to success.")
        elif streak >= failure_streak:
            state = "failing"
            note = (f"{streak} consecutive unhealthy runs "
                    f"({last.outcome if last else 'unknown'}).")
        elif days is not None and days >= stale_days:
            state = "stale"
            note = (f"last saved a row {days} days ago. Measured from the last "
                    f"row SAVED, not the last run attempted.")
        elif last is None or not last.outcome:
            state = "unknown"
            note = "no run has recorded an outcome yet."
        else:
            state = "ok"
            note = ""

        out.append(SourceHealth(
            source_key=key,
            display_name=display,
            listing_url=listing,
            implementation=implementation,
            requires_login=bool(contract.requires_login
                                or getattr(cls, "requires_login", False)),
            fetch_mode=fetch_mode,
            pagination=pagination,
            expected_types=", ".join(t.value for t in contract.expected_types),
            scope_confirmed=not contract.needs_owner_decision,
            last_run_at=last.started_at if last else None,
            last_outcome=(last.outcome or "") if last else "",
            last_error_code=(last.error_code or "") if last else "",
            last_error_message=((last.error_message or "")[:200]) if last else "",
            last_http_status=(last.last_http_status if last else None),
            runs_30d=runs_30d,
            unhealthy_streak=streak,
            total_rows=total,
            last_saved_at=last_saved,
            days_since_saved=days,
            state=state,
            note=note,
        ))

    # Worst first — the list exists to be acted on, and a health page that
    # opens on the sources that are fine is a health page nobody scrolls.
    order = {"failing": 0, "never_produced": 1, "stale": 2, "unknown": 3, "ok": 4}
    out.sort(key=lambda s: (order.get(s.state, 9), -s.unhealthy_streak,
                            -(s.days_since_saved or 0)))
    return out


def needs_recheck(entries, quiet_days: int = 7):
    """Confirmed-empty sources that are due a cheap look.

    A source that proved it has nothing to list is healthy, and it must not be
    switched off: the whole point is that the next opportunity is collected
    automatically. But nothing was scheduling that look — `CONFIRMED_EMPTY` was
    a terminal state in practice, so a source that went empty in June would
    still be reported empty in December whether or not anything had appeared.

    Cheap on purpose: first page or one API call with the open filter applied.
    Enough to clear the state, not a full crawl of a source that is probably
    still empty.
    """
    now = _now()
    due = []
    for e in entries:
        if (e.last_outcome or "") != Outcome.CONFIRMED_EMPTY.value:
            continue
        if e.last_run_at is None or (now - e.last_run_at).days >= quiet_days:
            due.append(e)
    return due


def summary(entries) -> dict:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.state] = counts.get(e.state, 0) + 1
    return {
        "total": len(entries),
        "by_state": counts,
        "needs_attention": sum(
            counts.get(k, 0) for k in ("failing", "never_produced", "stale")),
    }


def alerting_sources(entries, failure_streak: int = DEFAULT_FAILURE_STREAK):
    """Sources worth telling somebody about right now.

    Separate from `needs_attention` because staleness is a slow burn someone
    reviews weekly, while a failing streak is a thing that started happening
    and can still be caught.
    """
    return [e for e in entries
            if e.state == "failing" or
            (e.state == "never_produced" and e.unhealthy_streak >= failure_streak)]
