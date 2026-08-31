"""Pydantic schemas: scraper output (RawOpportunity) and API contracts."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator, Field

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
    # The SOURCE states there is no closing date: rolling, open-ended, until
    # filled. It is NOT "we could not find a date" — those are two different
    # things and conflating them is what put closed calls on the dashboard as
    # permanent "Ongoing" rows for months. A row flagged here never expires on a
    # date, so only set it when the page actually says so; leave it False when
    # the deadline is simply unknown, and the pipeline will keep the row live
    # until its source stops listing it (LOP_ONGOING_MAX_AGE_DAYS).
    assume_active: bool = False
    dayfirst: bool = True                  # date convention: True=31/07 (IN/EU), False=07/31 (US)
    # What the SOURCE calls this record, in the source's own vocabulary —
    # "contract_award", "tender", "eoi", "project", "grant". Left empty unless
    # the source exposes such a field; it is never inferred from the title,
    # because inferring it is exactly the mistake it exists to prevent. A World
    # Bank contract award and an open tender read identically as prose.
    # Judged against the source's contract in services/source_manifest.py.
    record_type: str = ""
    # The source's own status word — "Open", "Cancelled", "Closed",
    # "Request for Expressions of Interest". Only a value the source's contract
    # lists as closed may discard a row; an unrecognised value means UNKNOWN,
    # never closed, so a vocabulary nobody has configured cannot silently
    # delete records.
    source_status: str = ""


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
    work_type: str = ""   # Research | Implementation | "" (unclear)
    study_type: str = ""  # Baseline | Endline | Data Collection | …
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
    approved: bool = False
    approved_at: datetime | None = None
    approved_by: str = ""

    # Always-clickable destination, computed rather than stored so a change to
    # the fallback rules applies to every existing row without a migration.
    #   link_kind == "direct"  -> the opportunity's own page
    #   link_kind == "listing" -> the funder's index/section page; the call is
    #                             on it somewhere, but the reader still has to
    #                             find the row
    #   link_kind == "search"  -> a search that will find it
    # The kind is exposed so the UI can label it honestly; presenting a section
    # page or a search result as though it were the call itself is what made
    # links feel like they "open the wrong opportunity".
    link: str = ""
    link_kind: str = "direct"

    @model_validator(mode="after")
    def _resolve_link(self) -> "OpportunityOut":
        from app.services.links import resolve_link

        self.link, self.link_kind = resolve_link(
            self.opportunity_url, self.website, self.source_website, self.title,
            str(getattr(self.category, "value", self.category) or ""),
        )
        return self


class ApprovalRequest(BaseModel):
    """Body of POST /opportunities/{id}/approve."""

    approved: bool = True
    by: str = ""            # who acted; blank means the dashboard user


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
    # Show the closed/expired archive instead of live opportunities. Off by
    # default so the dashboard stays focused on what can still be bid on.
    archived: bool = False
    # Only opportunities first scraped today — what the "New Today" card counts.
    new_today: bool = False
    # Restrict to the approved set (what feeds the retrieval layer downstream).
    approved: bool = False
    # Routing axes: Research vs Implementation, and which kind of study.
    work_type: str = ""
    study_type: str = ""
    # Hide listings whose title is written in a non-Latin script. A display
    # preference, not a deletion — those rows are real opportunities.
    english_only: bool = True
    # Hide rows the classifier couldn't place in any vertical. Default on: an
    # unclassified row is one nobody owns, so it is noise in the working view.
    # A toggle rather than a deletion — many are real, just thin on text.
    has_vertical: bool = True
    # Administrative opt-in for rolling and undated rows. Ordinary dashboard,
    # export and email requests leave this false and require a real future (or
    # today) deadline.
    include_undated: bool = False
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
    # Empty = everywhere. Unrecognised names are reported back rather than
    # dropped: a country nobody recognises matches nothing, which looks exactly
    # like a working filter that happens to find nothing.
    countries: str = ""
    regions: str = ""
    geo_include_unknown: bool = True
    auto_send: bool = True
    active: bool = True


class TeamMemberOut(TeamMemberIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class SendSelectionIn(BaseModel):
    """Hand-picked opportunities plus the team members to send them to."""
    opportunity_ids: list[int]
    member_ids: list[int]


class ReviewDecisionIn(BaseModel):
    """A person's ruling on a row whose closing date could not be determined.

    Three decisions, because those are the three things a reviewer can actually
    know after looking at the listing:

        dated    they read a closing date -> store it (a past date closes it)
        rolling  the call is open-ended   -> becomes live, any stored date cleared
        closed   it has already closed    -> archived, never deleted
    """

    decision: Literal["dated", "rolling", "closed"]
    # Required for "dated" and ignored otherwise. Validated in the route rather
    # than here so the error names the field a person actually filled in.
    deadline: date | None = None


class BulkVerticalsIn(BaseModel):
    """Assign (or clear, or revert) verticals on a batch of rows by hand."""

    opportunity_ids: list[int]
    # Empty list is a real answer — "none of our six" — and is stored as a
    # human label rather than as an unlabelled row, so the backfill leaves it
    # alone. Ignored when `revert` is true.
    verticals: list[str] = Field(default_factory=list)
    # Hand the row back to the classifier. Exists because "I labelled the wrong
    # batch" has to be recoverable: the backfill skips human rows, so without
    # this a mis-click would be permanent.
    revert: bool = False


class SendResult(BaseModel):
    member: str
    sent: int
    detail: str | None = None
    resent: bool = False
