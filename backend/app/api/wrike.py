import secrets
import time
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.accounts import administrator
from app.api.workspace import account as workspace_account
from app.core.auth import auth_required
from app.core.config import settings
from app.database.db import get_db
from app.database.models import Opportunity, TeamMember, WrikeConnection, WrikeTaskLink
from app.services.wrike_service import (
    authorization_url,
    encrypt_token,
    exchange_authorization_code,
    valid_wrike_host,
    verify_wrike_connection,
    WrikeConnectionError,
    WrikeNotConnected,
)
from app.services.wrike_tasks import (
    WrikeTaskRejected,
    WrikeTaskUncertain,
    contact_ids_by_email,
    create_task,
    get_folder,
    task_description,
    valid_task_permalink,
)

router = APIRouter(prefix="/wrike", tags=["Wrike"])
_STATE_COOKIE = "wrike_oauth_state"
_CALLBACK_PATH = f"{settings.api_prefix}/wrike/oauth"


@router.get("/status")
def wrike_status(db: Session = Depends(get_db)):
    connection = db.get(WrikeConnection, 1)
    return {
        "enabled": settings.wrike_enabled,
        "configured": all([
            settings.wrike_client_id,
            settings.wrike_client_secret,
            settings.wrike_redirect_uri,
            settings.wrike_folder_id,
        ]),
        "connected": connection is not None,
    }


def require_wrike_admin(request: Request, db: Session = Depends(get_db)):
    if not auth_required():
        raise HTTPException(
            status_code=403,
            detail="Sign-in must be enabled before connecting Wrike",
        )
    return administrator(request, db)


def require_wrike_member(request: Request, db: Session = Depends(get_db)) -> str:
    # workspace_account checks the active TeamMember, writable primary and
    # Origin on POST. Reject its synthetic local-development identity too.
    if not auth_required():
        raise HTTPException(403, "Sign-in is required for Wrike tasks")
    return workspace_account(request, db)


class CreateWrikeTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_ids: list[int] = Field(default_factory=list, max_length=20)


def _task_link_data(link: WrikeTaskLink | None) -> dict:
    if link is None:
        return {"status": "not_created", "task_id": None,
                "permalink": None, "assignment_warning": False}
    return {
        "status": link.status,
        "task_id": link.task_id,
        "permalink": link.permalink if link.permalink and valid_task_permalink(link.permalink) else None,
        "assignment_warning": link.assignment_warning,
    }


@router.get("/folder")
def wrike_folder(
    db: Session = Depends(get_db),
    _member: str = Depends(require_wrike_member),
):
    if not settings.wrike_folder_id:
        raise HTTPException(503, "Wrike destination folder is not configured")
    try:
        folder = get_folder(db, settings.wrike_folder_id)
    except WrikeConnectionError:
        raise HTTPException(502, "Wrike destination folder could not be verified")
    return {"id": folder["id"], "title": folder.get("title", "Wrike folder")}


@router.get("/assignees")
def wrike_assignees(
    db: Session = Depends(get_db),
    _member: str = Depends(require_wrike_member),
):
    members = db.scalars(
        select(TeamMember).where(TeamMember.active.is_(True)).order_by(TeamMember.name)
    ).all()
    try:
        matches = contact_ids_by_email(db, [member.email for member in members])
    except WrikeConnectionError:
        raise HTTPException(502, "Wrike assignees could not be loaded")
    return [{
        "id": member.id,
        "name": member.name,
        "email": member.email,
        "available": len(matches.get(member.email.strip().lower(), set())) == 1,
    } for member in members]


@router.get("/opportunities/{opportunity_id}")
def wrike_task_for_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_db),
    _member: str = Depends(require_wrike_member),
):
    if db.get(Opportunity, opportunity_id) is None:
        raise HTTPException(404, "Opportunity not found")
    link = db.scalar(select(WrikeTaskLink).where(WrikeTaskLink.opportunity_id == opportunity_id))
    return _task_link_data(link)


@router.post("/opportunities/{opportunity_id}/tasks", status_code=201)
def create_wrike_task_for_opportunity(
    opportunity_id: int,
    body: CreateWrikeTaskIn,
    db: Session = Depends(get_db),
    owner: str = Depends(require_wrike_member),
):
    if not settings.wrike_enabled:
        raise HTTPException(503, "Wrike task creation is not enabled")
    if not settings.wrike_folder_id:
        raise HTTPException(503, "Wrike destination folder is not configured")
    opportunity = db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "Opportunity not found")
    if db.scalar(select(WrikeTaskLink).where(WrikeTaskLink.opportunity_id == opportunity_id)):
        raise HTTPException(409, "A Wrike task already exists or is being created for this opportunity")
    title = (opportunity.title or "").strip()[:200] or f"Opportunity {opportunity_id}"
    description = task_description(opportunity)
    member_ids = list(dict.fromkeys(body.member_ids))
    if len(member_ids) != len(body.member_ids) or any(member_id <= 0 for member_id in member_ids):
        raise HTTPException(422, "Choose distinct, valid team members")
    members = db.scalars(select(TeamMember).where(
        TeamMember.id.in_(member_ids), TeamMember.active.is_(True)
    )).all() if member_ids else []
    if len(members) != len(member_ids):
        raise HTTPException(422, "A selected team member is not active")

    try:
        matches = contact_ids_by_email(db, [member.email for member in members]) if members else {}
        get_folder(db, settings.wrike_folder_id)
    except WrikeConnectionError:
        raise HTTPException(502, "Wrike contacts or folder could not be verified")
    responsible_ids: list[str] = []
    for member in members:
        ids = matches.get(member.email.strip().lower(), set())
        if len(ids) != 1:
            raise HTTPException(422, f"{member.name} does not have one active Wrike match")
        responsible_ids.append(next(iter(ids)))
    if len(set(responsible_ids)) != len(responsible_ids):
        raise HTTPException(422, "Selected team members resolve to the same Wrike user")

    link = WrikeTaskLink(
        opportunity_id=opportunity_id,
        status="creating",
        folder_id=settings.wrike_folder_id,
        created_by=owner,
        responsible_ids=json.dumps(responsible_ids),
    )
    db.add(link)
    try:
        db.commit()  # Reserve the unique opportunity before the external POST.
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A Wrike task already exists or is being created for this opportunity")

    try:
        result = create_task(
            db,
            folder_id=settings.wrike_folder_id,
            title=title,
            description=description,
            responsible_ids=responsible_ids,
        )
    except (WrikeTaskRejected, WrikeConnectionError) as exc:
        # These failures occurred before creation or were definitive 4xx
        # rejections, so a later explicit retry is safe.
        db.delete(link)
        db.commit()
        raise HTTPException(502, str(exc))
    except WrikeTaskUncertain:
        link.status = "uncertain"
        db.commit()
        raise HTTPException(502, "Wrike may have created the task. Do not retry; ask an administrator to reconcile it")

    link.status = "created"
    link.task_id = result["task_id"]
    link.permalink = result["permalink"]
    link.assignment_warning = result["assignment_warning"]
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(502, "Wrike created a task but saving its link failed. Do not retry")
    return _task_link_data(link)


@router.post("/verify")
def wrike_verify(
    db: Session = Depends(get_db),
    _admin=Depends(require_wrike_admin),
):
    try:
        return {"reachable": verify_wrike_connection(db)}
    except WrikeNotConnected:
        raise HTTPException(409, "Wrike has not been connected")
    except WrikeConnectionError:
        raise HTTPException(502, "Could not verify the Wrike connection")


@router.get("/connect")
def wrike_connect(
    request: Request,
    _admin=Depends(require_wrike_admin),
):
    if not all((
        settings.wrike_client_id,
        settings.wrike_client_secret,
        settings.wrike_redirect_uri,
        settings.wrike_token_encryption_key,
    )):
        raise HTTPException(503, "Wrike OAuth settings are incomplete")
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(authorization_url(state), status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE,
        value=state,
        max_age=600,
        httponly=True,
        secure=settings.wrike_redirect_uri.startswith("https://"),
        samesite="lax",
        path=_CALLBACK_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _callback_error(message: str, status_code: int) -> PlainTextResponse:
    response = PlainTextResponse(message, status_code=status_code)
    response.delete_cookie(_STATE_COOKIE, path=_CALLBACK_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oauth/callback")
def wrike_oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_wrike_admin),
):
    supplied_state = request.query_params.get("state")
    expected_state = request.cookies.get(_STATE_COOKIE)
    if not supplied_state or not expected_state or not secrets.compare_digest(
        supplied_state, expected_state
    ):
        return _callback_error("Wrike authorization state is invalid or expired.", 400)

    if request.query_params.get("error"):
        return _callback_error("Wrike authorization was not completed.", 400)

    code = request.query_params.get("code")
    if not code:
        return _callback_error("Wrike did not return an authorization code.", 400)

    try:
        payload = exchange_authorization_code(code)
        access_token = payload["access_token"]
        refresh_token = payload["refresh_token"]
        token_type = payload["token_type"]
        host = payload["host"]
        expires_in = int(payload["expires_in"])
        if not (
            isinstance(access_token, str) and access_token
            and isinstance(refresh_token, str) and refresh_token
            and isinstance(token_type, str) and token_type.lower() == "bearer"
            and isinstance(host, str) and valid_wrike_host(host)
            and 0 < expires_in <= 86400
        ):
            raise ValueError("Invalid Wrike token response")
        encrypted_access = encrypt_token(access_token)
        encrypted_refresh = encrypt_token(refresh_token)
    except (KeyError, TypeError, ValueError):
        return _callback_error("Wrike returned an invalid token response.", 502)
    except Exception:
        # Do not expose token values or the authorization code in responses.
        return _callback_error("Could not complete Wrike authorization.", 502)

    connection = db.get(WrikeConnection, 1)
    if connection is None:
        connection = WrikeConnection(id=1)
        db.add(connection)
    connection.access_token_encrypted = encrypted_access
    connection.refresh_token_encrypted = encrypted_refresh
    connection.host = host.lower()
    connection.expires_at = int(time.time()) + expires_in
    connection.connected_by = admin["email"]
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _callback_error("Could not save the Wrike connection.", 500)

    response = RedirectResponse(settings.dashboard_url, status_code=303)
    response.delete_cookie(_STATE_COOKIE, path=_CALLBACK_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
