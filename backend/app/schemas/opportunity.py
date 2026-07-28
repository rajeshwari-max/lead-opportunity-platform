"""Pydantic schemas: scraper output (RawOpportunity) and API contracts."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import Category, Status


class RawOpportunity(BaseModel):
    """What a scraper emits BEFORE normalization/classification/dedup.

    Scrapers only fill what the source exposes; the pipeline enriches the rest.
    """

    title: str
    organization: str = ""
    country: str = ""
    region: str = ""
    funding_type: str = ""
    vertical: str = ""
    deadline_raw: str = ""              # free-text; normalized by DeadlineParser
    website: str = ""
    opportunity_url: str = ""
    summary: str = ""
    location: str = ""
    eligibility: str = ""
    funding_amount: str = ""
    source_website: str
    category_hint: Category | None = None  # optional hint from the source itself
    assume_active: bool = False            # source explicitly marks it open but
                                           # hides the exact deadline (membership sites)
    dayfirst: bool = True                  # date convention: True=31/07 (IN/EU), False=07/31 (US)


class OpportunityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    unique_id: str
    title: str
    organization: str
    country: str
    region: str
    funding_type: str
    vertical: str
    verticals: str = ""  # canonical comma-separated vertical tags
    category: Category
    deadline: date | None
    website: str
    opportunity_url: str
    summary: str
    location: str
    eligibility: str
    funding_amount: str
    status: Status
    source_website: str
    date_scraped: datetime


class PaginatedOpportunities(BaseModel):
    items: list[OpportunityOut]
    total: int
    page: int
    page_size: int
    pages: int


class OpportunityFilters(BaseModel):
    """Query-side filter model (bound from query params in the route)."""

    categories: list[str] = Field(default_factory=list)
    verticals: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    deadline_before: date | None = None
    deadline_after: date | None = None
    search: str = ""
    page: int = 1
    page_size: int = 25
    sort_by: str = "deadline"
    sort_dir: str = "asc"


class ScrapeRequest(BaseModel):
    sources: list[str] = Field(default_factory=list)  # empty = all registered
    verticals: list[str] = Field(default_factory=list)  # vertical-aware scraping; empty = all


class ScheduleRequest(BaseModel):
    mode: str = "manual"        # manual | daily | weekly | monthly | yearly | cron
    cron: str | None = None     # used when mode == "cron"
    hour: int = 2
    minute: int = 0


class ScheduleStatusOut(ScheduleRequest):
    """Full scheduler state shown in the UI (persisted across restarts)."""

    next_run: datetime | None = None
    last_run: datetime | None = None
    last_status: str | None = None          # success | failed | skipped
    last_success: datetime | None = None


class StatsOut(BaseModel):
    total_active: int
    by_category: dict[str, int]
    by_region: dict[str, int]
    by_vertical: dict[str, int]
    todays_new: int
    upcoming_deadlines: list[OpportunityOut]
    last_scraped: datetime | None


class TeamMemberIn(BaseModel):
    name: str
    email: str
    keywords: str = ""      # comma-separated, e.g. "climate, environment"
    categories: str = ""    # comma-separated Category values; empty = all
    verticals: str = ""     # comma-separated canonical verticals; empty = all
    auto_send: bool = True
    active: bool = True


class TeamMemberOut(TeamMemberIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class SendResult(BaseModel):
    member: str
    sent: int
    detail: str | None = None
