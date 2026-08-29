"""SQLAlchemy ORM models — the canonical Opportunity schema every source feeds."""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Date, DateTime, Enum, Float, Index, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Category(str, enum.Enum):
    GRANT = "Grant"
    RFP = "RFP"
    TENDER = "Tender"
    PROPOSAL = "Proposal"
    FELLOWSHIP = "Fellowship"
    AWARD = "Award"
    CHALLENGE = "Challenge"
    OTHER = "Other"


class Status(str, enum.Enum):
    ACTIVE = "Active"
    EXPIRED = "Expired"


class Opportunity(Base):
    """Canonical, source-agnostic opportunity record."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    unique_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    title: Mapped[str] = mapped_column(Text)
    organization: Mapped[str] = mapped_column(String(512), default="", index=True)
    country: Mapped[str] = mapped_column(String(128), default="", index=True)
    region: Mapped[str] = mapped_column(String(128), default="", index=True)
    funding_type: Mapped[str] = mapped_column(String(128), default="")
    vertical: Mapped[str] = mapped_column(String(256), default="", index=True)
    # Canonical multi-label vertical tags, comma-separated (see services/verticals.py),
    # e.g. "Health, Climate/Sustainability". `vertical` above keeps the raw
    # source-provided free text untouched.
    verticals: Mapped[str] = mapped_column(String(256), default="", index=True)
    # Research vs Implementation — decides which team an RFP is routed to.
    # Empty means genuinely unclear from the text; see services/work_type.py.
    work_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    # Which kind of study, when it is one: Baseline / Endline / Data Collection…
    study_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    category: Mapped[Category] = mapped_column(
        Enum(Category, values_callable=lambda e: [m.value for m in e]),
        default=Category.OTHER,
        index=True,
    )
    deadline: Mapped[date | None] = mapped_column(Date, index=True)
    # ------------------------------------------------------------- deadlines
    # `deadline IS NULL` was carrying two incompatible meanings — "the source
    # states there is no closing date" (actionable: apply today) and "we could
    # not read one" (not actionable: nobody knows). Storing both as NULL is why
    # "is this still open?" had no reliable answer for 3,021 Active rows.
    #
    # See services/actionable.py for the states and the predicate that uses them.
    deadline_state: Mapped[str | None] = mapped_column(String(16), index=True)
    # The source's own words, kept verbatim. A normalized date can be wrong —
    # 09/01/2026 is two different days depending on the source's convention —
    # and without the original there is nothing to re-parse against when the
    # convention is corrected.
    deadline_raw: Mapped[str | None] = mapped_column(String(256))
    # How the state was arrived at: parsed | source_rolling | unparseable |
    # legacy_assumed. Distinguishes a value a scraper observed from one a
    # migration assigned, so a backfilled guess is never mistaken for evidence.
    deadline_confidence: Mapped[str | None] = mapped_column(String(24))
    # Which day/month convention was applied (dayfirst | monthfirst | iso).
    # 2026-01-09 vs 2026-09-01 is the day/month-inversion class of bug, and
    # without recording the convention it cannot be audited after the fact.
    deadline_convention: Mapped[str | None] = mapped_column(String(16))
    deadline_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    website: Mapped[str] = mapped_column(String(512), default="")
    opportunity_url: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(512), default="")
    eligibility: Mapped[str] = mapped_column(Text, default="")
    funding_amount: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[Status] = mapped_column(
        Enum(Status, values_callable=lambda e: [m.value for m in e]),
        default=Status.ACTIVE,
        index=True,
    )
    source_website: Mapped[str] = mapped_column(String(128), index=True)
    date_scraped: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # When a scrape last saw this listing on the source. date_scraped answers
    # "when did we first find it" and never moves, so it cannot distinguish a
    # call the funder still lists from one they took down months ago. That
    # distinction is the only way to retire an undated "Ongoing" row: it has no
    # deadline to expire by, so without this it stays in the live view forever.
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )

    # Human sign-off. This is the gate for everything downstream — only
    # approved opportunities are meant to reach the retrieval/agentic layer —
    # so who approved it and when are recorded, not just the flag.
    approved: Mapped[bool] = mapped_column(default=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    approved_by: Mapped[str] = mapped_column(String(320), default="")

    __table_args__ = (
        UniqueConstraint("unique_id", name="uq_opportunity_unique_id"),
        Index("ix_opp_deadline_category", "deadline", "category"),
    )


# --------------------------------------------------------------- deadlines
# See services/actionable.py. `deadline IS NULL` used to mean two incompatible
# things — "the source says there is no closing date" and "we could not read
# one" — and the first is actionable while the second is not.
class DeadlineState(str, enum.Enum):
    DATED = "dated"
    ROLLING = "rolling"
    UNKNOWN = "unknown"


class ScrapeRun(Base):
    """History of scrape runs (powers 'last scraped' + incremental updates).

    The 2026-08-29 baseline showed what the original ten columns could not say.
    916 runs, none ever marked failed, and 16 sources that fetched nothing and
    saved nothing across 127 attempts — all filed as "completed", because that
    value was only ever written after the crawl loop, so it meant "the function
    returned". Nothing recorded a status code, a URL or an error, so "the site
    blocked us" and "our parser is broken" were literally the same row.

    Everything added below exists to make one of those distinctions possible.
    Every column is nullable or defaulted: this migrates an existing 177 MB
    database in place, and no historical run can be retro-fitted with evidence
    nobody captured.
    """

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_website: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    pages_scraped: Mapped[int] = mapped_column(default=0)
    found: Mapped[int] = mapped_column(default=0)
    saved: Mapped[int] = mapped_column(default=0)
    skipped_expired: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")

    # -------------------------------------------------- identity and outcome
    # The registry key, which never changes, as opposed to source_website, which
    # is the display name and has. The baseline found 91 distinct names in this
    # table against 85 registered sources, because renames split each source's
    # history in two: "Kbs Frb"/"King Baudouin Foundation", "Macfound"/
    # "Macarthur Foundation", "Mcknight"/"McKnight Foundation", "Openphilanthropy"/
    # "Open Philanthropy"/"Coefficient Giving". Any "last successful run" or
    # consecutive-failure count keyed on the display name is wrong for those, and
    # would raise false alarms the day someone fixes a capitalisation.
    source_key: Mapped[str | None] = mapped_column(String(64), index=True)

    # The taxonomy value (services/scrape_outcome.Outcome). `status` is kept as
    # it was so existing dashboard code and 916 rows of history stay readable.
    outcome: Mapped[str | None] = mapped_column(String(32), index=True)
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(Text)

    # ------------------------------------------------------------- transport
    first_http_status: Mapped[int | None] = mapped_column()
    last_http_status: Mapped[int | None] = mapped_column()
    final_url: Mapped[str | None] = mapped_column(String(1024))
    fetch_mode: Mapped[str | None] = mapped_column(String(16))   # http | browser
    attempts: Mapped[int] = mapped_column(default=0)
    duration_s: Mapped[float | None] = mapped_column(Float)

    # ---------------------------------------------------------------- counts
    # found/saved existed; without these two, "found 40, saved 0" could mean the
    # source is healthy and repeating itself, or that the gate threw everything
    # away. Different problems, same old row.
    duplicates: Mapped[int] = mapped_column(default=0)
    rejected: Mapped[int] = mapped_column(default=0)

    # ------------------------------------------------- liveness and ownership
    # The lease. A run whose heartbeat has stopped is abandoned, which is how
    # startup tells a live run from one whose process is gone — the 106 stuck
    # rows in the baseline had no way to express that.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(64))   # host:pid

    # ------------------------------------------------------- parser drift
    # A hash of the shape the parser depended on. Comparing it against the last
    # successful run is what separates STRUCTURE_CHANGED (positive evidence of
    # drift) from PARSE_ZERO (we simply found nothing and do not know why).
    structure_signature: Mapped[str | None] = mapped_column(String(64))
    debug_capture: Mapped[str | None] = mapped_column(String(512))  # sanitized fixture path


class ScrapeLease(Base):
    """One row. Whoever holds it is the process allowed to scrape.

    APScheduler's max_instances=1 is enforced inside one scheduler object in one
    process, and everything that can produce a second scraper lives outside that
    boundary: an overlapping Gunicorn reload, a deploy where the new process
    starts before the old exits, someone raising `workers` without knowing the
    scheduler is in-process, or a dashboard Start landing on a different worker
    than the scheduled run.

    Acquisition is a single conditional UPDATE, which SQLite serialises, so two
    processes racing cannot both win. See services/run_lock.py.
    """

    __tablename__ = "scrape_lease"

    id: Mapped[int] = mapped_column(primary_key=True)      # always 1
    worker_id: Mapped[str | None] = mapped_column(String(64))   # host:pid
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The liveness signal. A holder that stops refreshing this is presumed dead
    # once the TTL passes, and the lease becomes takeable again.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    label: Mapped[str] = mapped_column(String(200), default="")


class TeamMember(Base):
    """A colleague/lead who receives matching opportunities by email."""

    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    keywords: Mapped[str] = mapped_column(Text, default="")      # comma-separated, e.g. "climate, environment"
    categories: Mapped[str] = mapped_column(Text, default="")    # comma-separated Category values; empty = all
    verticals: Mapped[str] = mapped_column(Text, default="")     # comma-separated canonical verticals; empty = all
    auto_send: Mapped[bool] = mapped_column(default=True)        # include in post-scrape auto-digest
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class SentLog(Base):
    """Which opportunity was sent to which member — prevents re-sending duplicates."""

    __tablename__ = "sent_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(index=True)
    opportunity_id: Mapped[int] = mapped_column(index=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("member_id", "opportunity_id", name="uq_sent_member_opp"),
    )


class ExpertCount(Base):
    """Latest expert-pool size per vertical (DevelopmentAid experts search)."""

    __tablename__ = "expert_counts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vertical: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    count: Mapped[int] = mapped_column(default=0)
    search_url: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
