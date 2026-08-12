"""REST API — thin controllers delegating to services (no business logic here)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.db import get_db
from app.schemas.opportunity import (
    ApprovalRequest,
    OpportunityFilters,
    OpportunityOut,
    PaginatedOpportunities,
    ScheduleRequest,
    ScheduleStatusOut,
    ScrapeRequest,
    SendResult,
    SendSelectionIn,
    StatsOut,
    TeamMemberIn,
    TeamMemberOut,
)
from app.scrapers.registry import SCRAPER_REGISTRY
from app.database.models import TeamMember
from app.services import approval_service, dispatch_service, email_service, export_service
from app.services.matching_service import MatchingService
from app.services.filter_service import FilterService
from app.services.scheduler import scheduler
from app.services.scraper_manager import manager

router = APIRouter()


@router.get("/config")
def get_config(request: Request) -> dict:
    """Capability + identity probe. Exempt from the auth gate so the frontend
    can decide whether to show a login form before it has a session."""
    from app.core.auth import COOKIE_NAME, admin_required, auth_required, current_user

    user = current_user(request.cookies.get(COOKIE_NAME))
    return {
        "read_only": settings.read_only,
        "auth_required": auth_required(),
        "admin_required": admin_required(),
        **user,
    }


def _name_from_email(email: str) -> str:
    """A readable display name from the local part: rajeshwari.c -> Rajeshwari C."""
    local = (email or "").split("@")[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    return " ".join(p.capitalize() for p in parts) or email


@router.post("/login")
def login(body: dict, response: Response, db: Session = Depends(get_db)) -> dict:
    """Sign in as a named team member.

    The email identifies who you are; the password authorises you. Supplying the
    admin password instead of the dashboard one signs you in with admin rights,
    so there is one form rather than two.
    """
    from sqlalchemy import func, select

    from app.core.auth import (COOKIE_NAME, SESSION_DAYS, admin_password_matches,
                               auth_required, domain_allowed, make_session_token,
                               password_matches)

    if not auth_required():
        return {"authenticated": True, "name": "Local", "email": "", "is_admin": True}

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    is_admin = admin_password_matches(password)
    if not (is_admin or password_matches(password)):
        raise HTTPException(status_code=401, detail="Incorrect password")

    member = db.execute(
        select(TeamMember).where(func.lower(TeamMember.email) == email)
    ).scalar_one_or_none()

    if member is not None and not member.active:
        # Deactivating someone is how access is revoked, so it has to beat the
        # domain rule below — otherwise removing a leaver would do nothing.
        raise HTTPException(
            status_code=403,
            detail="This account has been deactivated. Ask an admin to re-enable it.",
        )

    if member is None:
        # Anyone at a company domain may sign in with the dashboard password,
        # without an admin adding them first. They are still recorded as a team
        # member, because approvals are attributed by email and the digest
        # needs somewhere to hang preferences.
        if not domain_allowed(email):
            raise HTTPException(
                status_code=403,
                detail="Sign in with your work email address, or ask an admin "
                       "to add you in Team & Lead Routing.",
            )
        # auto_send stays off: a new member has no keywords, and a member with
        # no keywords matches *everything*. Switching one on unattended would
        # send them a digest of many thousands of rows at the next 09:00 run.
        member = TeamMember(
            name=_name_from_email(email), email=email, keywords="", categories="",
            verticals="", auto_send=False, active=True,
        )
        db.add(member)
        db.commit()
        db.refresh(member)

    response.set_cookie(
        COOKIE_NAME, make_session_token(member.email, member.name, is_admin),
        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", path="/",
    )
    return {"authenticated": True, "name": member.name,
            "email": member.email, "is_admin": is_admin}


@router.post("/logout")
def logout(response: Response) -> dict:
    from app.core.auth import COOKIE_NAME

    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


def require_admin(request: Request) -> None:
    """Guards the panels that change how the system behaves.

    Reading opportunities and approving them is open to anyone with the
    dashboard password; starting a scrape, editing team routing or changing the
    email schedule is not. Separate cookie, separate password.
    """
    from app.core.auth import COOKIE_NAME, current_user

    if not current_user(request.cookies.get(COOKIE_NAME))["is_admin"]:
        raise HTTPException(
            status_code=403,
            detail="Admin only. Sign in with the admin password to use this.",
        )


def require_writable() -> None:
    """Blocks scraper/schedule control on the read-only cloud mirror (LOP_READ_ONLY=true)
    — it has no DevelopmentAid login session and no persistent disk, so it must
    only ever display a snapshot pushed from the primary machine."""
    if settings.read_only:
        raise HTTPException(
            status_code=403,
            detail="This is a read-only mirror. Scraper and schedule controls only work on the primary server.",
        )


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
    archived: bool = False,
    new_today: bool = False,
    approved: bool = False,
    work_type: str = "",
    study_type: str = "",
    page: int = 1,
    page_size: int = 25,
    sort_by: str = "deadline",
    sort_dir: str = "asc",
) -> OpportunityFilters:
    return OpportunityFilters(
        archived=archived, new_today=new_today, approved=approved,
        work_type=work_type, study_type=study_type,
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


@router.post(
    "/opportunities/{opportunity_id}/approve",
    response_model=OpportunityOut,
    dependencies=[Depends(require_writable)],
)
def approve_opportunity(
    opportunity_id: int, body: ApprovalRequest, request: Request,
    db: Session = Depends(get_db),
) -> OpportunityOut:
    """Dashboard sign-off. Writable instances only — on the public mirror the
    button is hidden and this returns 403, so a stranger with the link can't
    change what the team has approved.

    Attribution comes from the signed session, never from the request body: a
    client could otherwise claim to be anyone, and "who approved this" is the
    one field that has to be trustworthy.
    """
    from app.core.auth import COOKIE_NAME, current_user

    user = current_user(request.cookies.get(COOKIE_NAME))
    who = user.get("email") or user.get("name") or body.by
    opp = approval_service.set_approved(db, opportunity_id, body.approved, who)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return OpportunityOut.model_validate(opp)


@router.get("/approve/{token}", response_class=Response)
def approve_via_link(token: str, db: Session = Depends(get_db)) -> Response:
    """One-click approval from a digest email.

    Deliberately exempt from require_writable: the HMAC signature proves the
    link came from a digest this installation generated, which is a stronger
    claim than "is not the read-only mirror". Returns a small HTML page because
    the recipient is a person clicking in their mail client, not a script.
    """
    try:
        payload = approval_service.read_token(token)
    except approval_service.InvalidToken as exc:
        return Response(_approval_page("Link not valid", str(exc), ok=False), status_code=400,
                        media_type="text/html")

    opp = approval_service.set_approved(db, int(payload["id"]), True, payload.get("by", ""))
    if opp is None:
        return Response(_approval_page("Not found", "That opportunity no longer exists.", ok=False),
                        status_code=404, media_type="text/html")
    return Response(
        _approval_page(
            "Approved", opp.title, ok=True, url=opp.opportunity_url,
            # Approving from an email is a single click with no confirmation
            # step, so a mis-click is easy and the landing page is the only
            # chance to take it back. The same token undoes it — possession
            # already granted approval, and undoing is strictly less powerful.
            action_url=f"{settings.api_prefix}/approve/{token}/undo",
            action_label="Undo — I clicked this by mistake",
        ),
        media_type="text/html",
    )


@router.get("/approve/{token}/undo", response_class=Response)
def undo_approval_via_link(token: str, db: Session = Depends(get_db)) -> Response:
    """Reverse an approval made from an email, from the confirmation page."""
    try:
        payload = approval_service.read_token(token)
    except approval_service.InvalidToken as exc:
        return Response(_approval_page("Link not valid", str(exc), ok=False), status_code=400,
                        media_type="text/html")

    opp = approval_service.set_approved(db, int(payload["id"]), False, payload.get("by", ""))
    if opp is None:
        return Response(_approval_page("Not found", "That opportunity no longer exists.", ok=False),
                        status_code=404, media_type="text/html")
    return Response(
        _approval_page(
            "Approval undone", opp.title, ok=True, url=opp.opportunity_url,
            # Symmetrical: undoing by mistake is just as possible as approving
            # by mistake, so the way back is offered here too.
            action_url=f"{settings.api_prefix}/approve/{token}",
            action_label="Approve it after all",
        ),
        media_type="text/html",
    )


def _approval_page(
    heading: str, detail: str, ok: bool, url: str = "",
    action_url: str = "", action_label: str = "",
) -> str:
    colour = "#059669" if ok else "#dc2626"
    link = (
        f'<p><a href="{url}" style="color:#2563eb;">Open the opportunity</a></p>' if url else ""
    )
    if action_url:
        link += (
            f'<p style="margin-top:18px;"><a href="{action_url}" '
            'style="display:inline-block;padding:9px 16px;border:1px solid #cbd5e1;'
            'border-radius:8px;color:#334155;text-decoration:none;font-size:14px;">'
            f'{action_label}</a></p>'
        )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading}</title></head>
<body style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f8fafc;
margin:0;padding:48px 16px;">
<div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;
border-radius:12px;padding:32px;">
<h1 style="margin:0 0 12px;font-size:20px;color:{colour};">{heading}</h1>
<p style="margin:0 0 16px;color:#334155;line-height:1.5;">{detail}</p>{link}
<p style="margin:16px 0 0;color:#94a3b8;font-size:13px;">You can close this tab.</p>
</div></body></html>"""


@router.get("/keywords")
def get_keywords() -> dict:
    """The BD team's own search vocabulary, for the dashboard search box.

    Grouped the way the team thinks about it (Health, Waste, Funding, Leads…)
    rather than by our six verticals, because these are the words they actually
    type when hunting, not a taxonomy.
    """
    from app.services.keyword_inventory import ALL_SEARCH_TERMS, KEYWORD_INVENTORY

    return {"groups": KEYWORD_INVENTORY, "all": ALL_SEARCH_TERMS}


@router.get("/filters")
def get_filters(
    f: OpportunityFilters = Depends(filters_dep), db: Session = Depends(get_db)
) -> dict[str, list[str]]:
    """Filter options, narrowed to what the current selection can still reach.

    Passing the active filters in means the Source dropdown offers only the
    sources that actually have a row under the chosen vertical, instead of all
    86 configured scrapers.
    """
    return FilterService(db).facets(f)


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
@router.post("/scrape", status_code=202, dependencies=[Depends(require_writable), Depends(require_admin)])
async def start_scrape(req: ScrapeRequest) -> dict[str, str]:
    try:
        await manager.start(req.sources or None, req.verticals or None)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "started"}


@router.post("/scrape/pause", dependencies=[Depends(require_writable), Depends(require_admin)])
def pause_scrape() -> dict[str, str]:
    manager.pause()
    return {"status": manager.state}


@router.post("/scrape/resume", dependencies=[Depends(require_writable), Depends(require_admin)])
def resume_scrape() -> dict[str, str]:
    manager.resume()
    return {"status": manager.state}


@router.post("/stop", dependencies=[Depends(require_writable), Depends(require_admin)])
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


@router.put("/schedule", response_model=ScheduleStatusOut, dependencies=[Depends(require_writable), Depends(require_admin)])
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


@router.post("/team", response_model=TeamMemberOut, status_code=201,
             dependencies=[Depends(require_admin)])
def add_member(body: TeamMemberIn, db: Session = Depends(get_db)) -> TeamMemberOut:
    if db.query(TeamMember).filter(TeamMember.email == body.email).first():
        raise HTTPException(status_code=409, detail="A member with this email already exists")
    member = TeamMember(**body.model_dump())
    db.add(member)
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)


@router.put("/team/{member_id}", response_model=TeamMemberOut,
            dependencies=[Depends(require_admin)])
def update_member(member_id: int, body: TeamMemberIn, db: Session = Depends(get_db)) -> TeamMemberOut:
    member = db.get(TeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    for field, value in body.model_dump().items():
        setattr(member, field, value)
    db.commit()
    db.refresh(member)
    return TeamMemberOut.model_validate(member)


@router.delete("/team/{member_id}", status_code=204,
               dependencies=[Depends(require_admin)])
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
async def send_now(member_id: int, resend: bool = False) -> SendResult:
    """Email this member their matching opportunities.

    `?resend=true` reissues everything currently matching, including items
    already sent — the normal path skips those and would find nothing, so this
    is how an improved email reaches someone who already got the old one.
    """
    import asyncio

    try:
        result = await asyncio.to_thread(dispatch_service.send_to_member, member_id, resend)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except email_service.EmailNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # SMTP/auth failures surface clearly in the UI
        raise HTTPException(status_code=502, detail=f"Email send failed: {exc}") from exc
    return SendResult(**result)


@router.post("/opportunities/send", response_model=list[SendResult],
             dependencies=[Depends(require_writable)])
async def send_selection(payload: SendSelectionIn) -> list[SendResult]:
    """Email a chosen set of opportunities to chosen team members."""
    from app.services import dispatch_service

    try:
        results = await asyncio.to_thread(
            dispatch_service.send_selection, payload.opportunity_ids, payload.member_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [SendResult(**r) for r in results]


@router.get("/email/settings")
def get_email_settings() -> dict:
    """Automatic-email settings plus when the next run will actually happen."""
    from app.services.email_settings import load

    cfg = load()
    nxt = scheduler.next_digest_run()
    return {**cfg.model_dump(), "next_run": nxt.isoformat() if nxt else None}


@router.put("/email/settings",
            dependencies=[Depends(require_writable), Depends(require_admin)])
def update_email_settings(body: dict) -> dict:
    """Save and apply immediately — no restart, no editing .env."""
    from app.services.email_settings import EmailSettings, load, save

    current = load()
    try:
        merged = EmailSettings(**{**current.model_dump(), **body})
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid settings: {exc}") from exc

    cfg = save(merged)
    # Rebuild the cron job now, so a changed send time takes effect today.
    scheduler.apply_email_settings()
    nxt = scheduler.next_digest_run()
    return {**cfg.model_dump(), "next_run": nxt.isoformat() if nxt else None}


@router.post("/email/run-now", dependencies=[Depends(require_writable)])
async def run_digest_now() -> dict:
    """Fire the daily digest + reminders immediately — used to prove the
    automation works without waiting until tomorrow morning."""
    import asyncio

    from app.services.reminder_service import send_due_reminders

    results = await asyncio.to_thread(dispatch_service.send_to_all_active)
    reminders = await asyncio.to_thread(send_due_reminders)
    return {
        "members_emailed": sum(1 for r in results if r.get("sent", 0) > 0),
        "opportunities_sent": sum(r.get("sent", 0) for r in results),
        "reminders_sent": reminders,
        "detail": [r for r in results],
    }


@router.get("/email/status")
def email_status() -> dict[str, bool]:
    return {"configured": email_service.is_configured()}


# ----------------------------------------------------------------- expert pool
@router.get("/experts")
def experts() -> list[dict]:
    from app.services import experts_service

    return experts_service.get_counts()


@router.post("/experts/refresh", dependencies=[Depends(require_admin)])
async def refresh_experts() -> list[dict]:
    from app.services import experts_service

    try:
        return await experts_service.refresh_counts()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Experts refresh failed: {exc}") from exc


@router.get("/devaid/status", dependencies=[Depends(require_admin)])
def devaid_status() -> dict[str, bool]:
    from app.scrapers.devaid_auth import has_profile

    return {"connected": has_profile()}


@router.get("/devaid/session/export", dependencies=[Depends(require_admin)])
async def devaid_session_export() -> Response:
    """Download this machine's signed-in session as a file.

    Run on the machine where you logged in; upload the result to the server.
    Only the session moves — no password ever leaves your browser, and the
    login itself stays a manual human step.
    """
    import asyncio
    import json

    from app.scrapers.devaid_auth import export_session_state

    try:
        state = await asyncio.to_thread(export_session_state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(
        content=json.dumps(state),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="devaid_session.json"'},
    )


@router.post("/devaid/session/import", dependencies=[Depends(require_writable), Depends(require_admin)])
async def devaid_session_import(payload: dict) -> dict:
    """Install a session exported from a machine that has a screen.

    This is how DevelopmentAid works on a headless server: the login window
    cannot open here, so the session is carried across instead.
    """
    import asyncio

    from app.scrapers.devaid_auth import import_session_state, verify_session

    try:
        count = await asyncio.to_thread(import_session_state, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Prove it actually works here rather than reporting success on a file that
    # happens to be well-formed — an expired session is well-formed too.
    ok = await asyncio.to_thread(verify_session)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"Session installed ({count} cookies) but it is not signed in "
                   "on this server. It may have expired — log in again on your "
                   "own machine, download a fresh session and upload that.",
        )
    return {"status": "connected", "cookies": count}


@router.post("/devaid/connect", dependencies=[Depends(require_admin)])
async def devaid_connect() -> dict[str, str]:
    """Open a visible browser window for the user to log into DevelopmentAid.

    Returns once they close the window. The session is only reported as saved
    when it's verified as genuinely signed in — closing the window early used
    to report success and leave every scrape running as a guest.
    """
    import asyncio

    from app.scrapers.devaid_auth import NoDisplayError, connect_interactive_sync

    try:
        ok = await asyncio.to_thread(connect_interactive_sync)
    except NoDisplayError as exc:
        # 409, not 502: nothing failed. This host simply cannot show a window,
        # and the message says what to do instead. A 502 would read as an
        # outage and send someone debugging a problem that doesn't exist.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not open login window: {exc}") from exc
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Login was not completed — the site still shows 'Sign in'. "
                   "Click Connect account again and finish signing in (including "
                   "the reCAPTCHA) before closing the window.",
        )
    return {"status": "saved"}
