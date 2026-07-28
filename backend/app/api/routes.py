"""REST API — thin controllers delegating to services (no business logic here)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.schemas.opportunity import (
    OpportunityFilters,
    OpportunityOut,
    PaginatedOpportunities,
    ScheduleRequest,
    ScheduleStatusOut,
    ScrapeRequest,
    SendResult,
    StatsOut,
    TeamMemberIn,
    TeamMemberOut,
)
from app.scrapers.registry import SCRAPER_REGISTRY
from app.database.models import TeamMember
from app.services import dispatch_service, email_service, export_service
from app.services.matching_service import MatchingService
from app.services.filter_service import FilterService
from app.services.scheduler import scheduler
from app.services.scraper_manager import manager

router = APIRouter()


def filters_dep(
    categories: list[str] = Query(default=[]),
    verticals: list[str] = Query(default=[]),
    countries: list[str] = Query(default=[]),
    regions: list[str] = Query(default=[]),
    sources: list[str] = Query(default=[]),
    organizations: list[str] = Query(default=[]),
    deadline_before: date | None = None,
    deadline_after: date | None = None,
    search: str = "",
    page: int = 1,
    page_size: int = 25,
    sort_by: str = "deadline",
    sort_dir: str = "asc",
) -> OpportunityFilters:
    return OpportunityFilters(
        categories=categories, verticals=verticals, countries=countries, regions=regions,
        sources=sources, organizations=organizations, deadline_before=deadline_before,
        deadline_after=deadline_after, search=search, page=page, page_size=page_size,
        sort_by=sort_by, sort_dir=sort_dir,
    )


# ------------------------------------------------------------------ opportunities
@router.get("/opportunities", response_model=PaginatedOpportunities)
def list_opportunities(
    f: OpportunityFilters = Depends(filters_dep), db: Session = Depends(get_db)
) -> PaginatedOpportunities:
    return FilterService(db).query(f)


@router.get("/filters")
def get_filters(db: Session = Depends(get_db)) -> dict[str, list[str]]:
    return FilterService(db).facets()


@router.get("/stats", response_model=StatsOut)
def get_stats(
    f: OpportunityFilters = Depends(filters_dep), db: Session = Depends(get_db)
) -> StatsOut:
    """Dashboard stats. Accepts the same filters as /opportunities so cards,
    charts and deadlines reflect the current vertical/category/search selection."""
    return FilterService(db).stats(f)


@router.get("/verticals")
def get_verticals() -> list[dict[str, str]]:
    """The canonical six-vertical system."""
    from app.services.verticals import VERTICAL_DESCRIPTIONS, VERTICALS

    return [{"name": v, "description": VERTICAL_DESCRIPTIONS[v]} for v in VERTICALS]


# ------------------------------------------------------------------------ export
@router.get("/export/csv")
def export_csv(
    f: OpportunityFilters = Depends(filters_dep), db: Session = Depends(get_db)
) -> Response:
    data = export_service.to_csv(FilterService(db).rows_for_export(f))
    return Response(
        data, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=opportunities.csv"},
    )


@router.get("/export/xlsx")
def export_xlsx(
    f: OpportunityFilters = Depends(filters_dep), db: Session = Depends(get_db)
) -> Response:
    data = export_service.to_xlsx(FilterService(db).rows_for_export(f))
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=opportunities.xlsx"},
    )


# ---------------------------------------------------------------------- scraping
@router.post("/scrape", status_code=202)
async def start_scrape(req: ScrapeRequest) -> dict[str, str]:
    try:
        await manager.start(req.sources or None, req.verticals or None)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "started"}


@router.post("/scrape/pause")
def pause_scrape() -> dict[str, str]:
    manager.pause()
    return {"status": manager.state}


@router.post("/scrape/resume")
def resume_scrape() -> dict[str, str]:
    manager.resume()
    return {"status": manager.state}


@router.post("/stop")
async def stop_scrape() -> dict[str, str]:
    await manager.stop()
    return {"status": "stopping"}


@router.get("/progress")
def get_progress() -> dict:
    return manager.snapshot()


# ----------------------------------------------------------------------- sources
@router.get("/sources")
def get_sources() -> list[dict[str, str]]:
    return [
        {"name": cls.name, "display_name": cls.display_name, "website": cls.website}
        for cls in SCRAPER_REGISTRY.values()
    ]


# --------------------------------------------------------------------- scheduler
@router.get("/schedule", response_model=ScheduleStatusOut)
def get_schedule() -> ScheduleStatusOut:
    """Current schedule + Next Run / Last Run / Status / Last Success."""
    return scheduler.status()


@router.put("/schedule", response_model=ScheduleStatusOut)
def set_schedule(req: ScheduleRequest) -> ScheduleStatusOut:
    try:
        scheduler.configure(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return scheduler.status()


# ------------------------------------------------------------------------ health
@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# -------------------------------------------------------------- team & sending
@router.get("/team", response_model=list[TeamMemberOut])
def list_team(db: Session = Depends(get_db)) -> list[TeamMemberOut]:
    rows = db.query(TeamMember).order_by(TeamMember.name).all()
    return [TeamMemberOut.model_validate(r) for r in rows]


@router.post("/team", response_model=TeamMemberOut, status_code=201)
def add_member(body: TeamMemberIn, db: Session = Depends(get_db)) -> TeamMemberOut:
    if db.query(TeamMember).filter(TeamMember.email == body.email).first():
        raise HTTPException(status_code=409, detail="A member with this email already exists")
    member = TeamMember(**body.model_dump())
    db.add(member)
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)


@router.put("/team/{member_id}", response_model=TeamMemberOut)
def update_member(member_id: int, body: TeamMemberIn, db: Session = Depends(get_db)) -> TeamMemberOut:
    member = db.get(TeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    for field, value in body.model_dump().items():
        setattr(member, field, value)
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)


@router.delete("/team/{member_id}", status_code=204)
def delete_member(member_id: int, db: Session = Depends(get_db)) -> None:
    member = db.get(TeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    db.delete(member)
    db.commit()


@router.get("/team/{member_id}/matches", response_model=list[OpportunityOut])
def preview_matches(member_id: int, db: Session = Depends(get_db)) -> list[OpportunityOut]:
    """What WOULD be sent right now (un-sent matches only)."""
    member = db.get(TeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    matches = MatchingService(db).matches_for(member)
    return [OpportunityOut.model_validate(m) for m in matches]


@router.post("/team/{member_id}/send", response_model=SendResult)
async def send_now(member_id: int) -> SendResult:
    """Email this member their new matching opportunities."""
    import asyncio

    try:
        result = await asyncio.to_thread(dispatch_service.send_to_member, member_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except email_service.EmailNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # SMTP/auth failures surface clearly in the UI
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc
    return SendResult(**result)


@router.get("/email/status")
def email_status() -> dict[str, bool]:
    return {"configured": email_service.is_configured()}


# ----------------------------------------------------------------- expert pool
@router.get("/experts")
def experts() -> list[dict]:
    from app.services import experts_service

    return experts_service.get_counts()


@router.post("/experts/refresh")
async def refresh_experts() -> list[dict]:
    from app.services import experts_service

    try:
        return await experts_service.refresh_counts()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Experts refresh failed: {exc}") from exc


@router.get("/devaid/status")
def devaid_status() -> dict[str, bool]:
    from app.scrapers.devaid_auth import has_profile

    return {"connected": has_profile()}


@router.post("/devaid/connect")
async def devaid_connect() -> dict[str, str]:
    """Open a visible browser window for the user to log into DevelopmentAid.
    Returns once they close the window; the session is saved for scrapers."""
    import asyncio

    from app.scrapers.devaid_auth import connect_interactive_sync

    try:
        await asyncio.to_thread(connect_interactive_sync)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open login window: {exc}") from exc
    return {"status": "saved"}
