"""SQLAlchemy ORM models — the canonical Opportunity schema every source feeds."""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, Index, String, Text, UniqueConstraint
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
    category: Mapped[Category] = mapped_column(
        Enum(Category, values_callable=lambda e: [m.value for m in e]),
        default=Category.OTHER,
        index=True,
    )
    deadline: Mapped[date | None] = mapped_column(Date, index=True)
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

    __table_args__ = (
        UniqueConstraint("unique_id", name="uq_opportunity_unique_id"),
        Index("ix_opp_deadline_category", "deadline", "category"),
    )


class ScrapeRun(Base):
    """History of scrape runs (powers 'last scraped' + incremental updates)."""

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
