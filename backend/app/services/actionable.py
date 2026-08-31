"""One definition of "can someone still respond to this", used everywhere.

The problem
-----------
There were three different answers in the codebase, and a row's visibility
depended on which one you happened to hit:

  * `filter_service._base_statement` default branch — ACTIVE and
    (deadline >= today or deadline IS NULL)
  * `filter_service._base_statement` approved branch — ACTIVE only. The
    deadline predicate was deliberately skipped, with the reasoning that a
    curated hand-off should not "silently empty itself" as deadlines pass.
  * `matching_service.matches_for` (the email path) — its own copy of the
    first one.

So the 1,481 ACTIVE rows whose deadline has passed were hidden in the main
table and visible in the Approved view, and any new query written tomorrow
would need a fourth copy of a rule nobody had written down.

The three deadline states
-------------------------
`deadline IS NULL` was carrying two incompatible meanings:

    DATED    the source gave a closing date and we parsed it
    ROLLING  the source SAYS there is no closing date ("rolling basis",
             "applications accepted continuously")
    UNKNOWN  the source gave something we could not parse, or gave nothing

ROLLING is actionable — you can apply today. UNKNOWN is not, because nobody
knows whether it is. Storing both as NULL made them the same row, which is why
"is this still open?" had no reliable answer.

`is_actionable` and `actionable_clause` are the same rule expressed twice, once
in Python and once as SQL. The pair is covered by a test that runs both over
the same fixtures, because two implementations that drift are worse than one
that is merely wrong.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import and_, or_

from app.database.models import Opportunity, Status


class DeadlineState(str, Enum):
    """What we actually know about this row's closing date."""

    DATED = "dated"        # a real date, parsed with reasonable confidence
    ROLLING = "rolling"    # the SOURCE states there is no closing date
    UNKNOWN = "unknown"    # unparseable, absent, or never determined

    @property
    def is_respondable(self) -> bool:
        """Can a person act on this today, deadline aside?

        UNKNOWN is deliberately False. "We could not read a date" is not
        evidence that a call is open, and treating it as such is what puts
        long-closed listings in front of someone about to spend a week on a
        proposal.
        """
        return self in (DeadlineState.DATED, DeadlineState.ROLLING)


# Confidence markers stored alongside the state, so a value assigned by a
# migration is never mistaken for one a scraper actually observed.
CONFIDENCE_PARSED = "parsed"            # a date we read from the source
CONFIDENCE_SOURCE_ROLLING = "source_rolling"   # the source said so, in words
CONFIDENCE_LEGACY = "legacy_assumed"    # backfilled; the original signal is gone
CONFIDENCE_UNPARSEABLE = "unparseable"  # raw text was present, no date in it


IST_OFFSET_MINUTES = 330


def application_today(tz_offset_minutes: int | None = None) -> date:
    """"Today" for the purpose of "has this closed".

    A deadline is a local-time concept for the applicant, and the server runs
    UTC. Without an explicit offset, a call closing today looks closed for the
    last few hours of an IST working day — precisely when someone is rushing to
    submit. The offset is configuration, not a guess at the viewer's location.
    """
    offset = IST_OFFSET_MINUTES if tz_offset_minutes is None else tz_offset_minutes
    return (datetime.now(timezone.utc) + timedelta(minutes=offset)).date()


# --------------------------------------------------------------- the predicate

def is_actionable(
    status,
    deadline: date | None,
    deadline_state: str | None,
    today: date | None = None,
) -> bool:
    """Python half of the rule. Same logic as actionable_clause below.

    Written to tolerate rows that predate the deadline_state column: a NULL
    state falls back to inferring from the date, so a partially migrated
    database degrades to the old behaviour rather than hiding everything.
    """
    today = today or application_today()

    active = status == Status.ACTIVE or str(status).upper().endswith("ACTIVE")
    if not active:
        return False

    state = _coerce_state(deadline_state, deadline)
    if state is DeadlineState.ROLLING:
        # A stored date still wins, even here. A source that says "rolling
        # basis" and also carries a closing date that has gone is closed — and
        # without this check, `rolling` becomes a way for a row to stay live
        # forever, which is the failure mode the whole module is about.
        return deadline is None or deadline >= today
    if state is DeadlineState.DATED:
        return deadline is not None and deadline >= today
    return False        # UNKNOWN


def _coerce_state(deadline_state: str | None, deadline: date | None) -> DeadlineState:
    """Read the stored state, inferring one for rows written before it existed."""
    if deadline_state:
        try:
            return DeadlineState(deadline_state)
        except ValueError:
            pass
    return DeadlineState.DATED if deadline is not None else DeadlineState.UNKNOWN


def actionable_clause(today: date | None = None):
    """SQL half of the rule. The single place any query should get it from.

    Returns a SQLAlchemy expression, so it composes with every other filter:

        stmt = select(Opportunity).where(actionable_clause())

    The NULL-state branch is what lets this ship before the backfill finishes.
    """
    today = today or application_today()
    return and_(
        Opportunity.status == Status.ACTIVE,
        or_(
            # Rolling, and no stored date has already passed. The date wins
            # even for a rolling row — see is_actionable.
            and_(
                Opportunity.deadline_state == DeadlineState.ROLLING.value,
                or_(Opportunity.deadline.is_(None), Opportunity.deadline >= today),
            ),
            and_(
                Opportunity.deadline_state == DeadlineState.DATED.value,
                Opportunity.deadline >= today,
            ),
            # Rows written before deadline_state existed: infer from the date,
            # which reproduces the old default-branch behaviour exactly.
            and_(
                Opportunity.deadline_state.is_(None),
                Opportunity.deadline.is_not(None),
                Opportunity.deadline >= today,
            ),
        ),
    )


def strict_actionable_clause(today: date | None = None):
    """The ordinary-user definition of an active opportunity.

    Administrative review still uses :func:`actionable_clause`, which admits a
    source-confirmed rolling opportunity without a date.  The dashboard, export
    and email surfaces are intentionally stricter: a user should only be shown
    a call with a real closing date that has not passed in application time.
    """
    today = today or application_today()
    return and_(
        Opportunity.status == Status.ACTIVE,
        Opportunity.deadline.is_not(None),
        Opportunity.deadline >= today,
        or_(
            Opportunity.deadline_state.is_(None),
            Opportunity.deadline_state != DeadlineState.UNKNOWN.value,
        ),
    )


def expired_clause(today: date | None = None):
    """The complement, for the archive view. Deliberately not `not_(...)` of the
    above: an UNKNOWN row is not actionable, but it is not expired either — it
    is unassessed, and lumping it into the archive would bury rows that need a
    human to look at them."""
    today = today or application_today()
    return or_(
        Opportunity.status == Status.EXPIRED,
        and_(
            Opportunity.deadline.is_not(None),
            Opportunity.deadline < today,
        ),
    )


def unassessed_clause():
    """Active rows whose deadline could not be determined at all.

    These are neither shown as live nor archived — they need a decision. Kept
    as its own predicate so the number is visible instead of being silently
    absorbed by one of the other two.
    """
    return and_(
        Opportunity.status == Status.ACTIVE,
        Opportunity.deadline_state == DeadlineState.UNKNOWN.value,
    )


def classify_deadline(
    raw: str,
    parsed: date | None,
    source_says_rolling: bool,
) -> tuple[DeadlineState, str]:
    """(state, confidence) for a freshly scraped row.

    Order matters: an explicit date wins over a rolling marker, because a page
    saying "rolling basis — next review 30 September" has a date someone can
    act on, and the date is the more useful of the two.
    """
    if parsed is not None:
        return DeadlineState.DATED, CONFIDENCE_PARSED
    if source_says_rolling:
        return DeadlineState.ROLLING, CONFIDENCE_SOURCE_ROLLING
    if (raw or "").strip():
        # There WAS text and we could not read a date out of it. That is a
        # parser gap worth finding, not a rolling deadline.
        return DeadlineState.UNKNOWN, CONFIDENCE_UNPARSEABLE
    return DeadlineState.UNKNOWN, CONFIDENCE_UNPARSEABLE
