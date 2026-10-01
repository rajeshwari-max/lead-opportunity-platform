"""Personal login and administrator-issued, single-use account invitations."""
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from threading import Lock
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session

from app.core.auth import (COOKIE_NAME, SESSION_DAYS, current_user, hash_password,
                           verify_password, make_session_token, password_version,
                           password_matches)
from app.core.config import settings
from app.database.db import get_db
from app.database.models import TeamMember, WorkspaceCredential

router = APIRouter()
_attempts = {}
_mutex = Lock()


def guard(request: Request):
    from app.core.auth import validate_origin
    validate_origin(request)


def throttle(key):
    now = time.time()
    with _mutex:
        for k in list(_attempts):
            _attempts[k] = [t for t in _attempts[k] if t > now-300]
            if not _attempts[k]:
                del _attempts[k]
        attempts = _attempts.setdefault(key, [])
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many attempts. Please wait five minutes.")
        attempts.append(now)


def administrator(request: Request, db: Session = Depends(get_db)):
    guard(request)
    if settings.read_only:
        raise HTTPException(403, "Account management is unavailable in read-only mode")
    user = current_user(request.cookies.get(COOKIE_NAME), db)
    if not user["authenticated"] or not user["is_admin"]:
        raise HTTPException(403, "Administrator access required")
    return user


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


def sign_in(response, request, member, credential):
    response.set_cookie(COOKIE_NAME, make_session_token(member.email.lower(), member.name, credential.is_admin, password_version(credential.password_hash)),
        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/", max_age=SESSION_DAYS*86400)
    response.delete_cookie("lop_workspace", path="/api/workspace")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True, "email": member.email, "name": member.name, "is_admin": credential.is_admin}


@router.post("/login")
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    guard(request)
    email = body.email.strip().lower()
    throttle("email:"+email)
    throttle("ip:"+(request.client.host if request.client else "unknown"))
    credential = db.get(WorkspaceCredential, email)
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
    valid = verify_password(body.password, credential.password_hash if credential else "")

    # Transition accounts from the former shared-password dashboard without
    # trapping existing team members between Sign in and Register.  The legacy
    # password is accepted only for an active, pre-existing member who has not
    # established a personal password.  On the first successful use it is
    # immediately stored as that member's salted personal hash; once they reset
    # or change it, the shared password can never override the personal one.
    legacy = bool(
        member and member.active
        and not (credential and credential.password_hash)
        and settings.dashboard_password
        and password_matches(body.password)
    )
    if legacy:
        credential = credential or WorkspaceCredential(
            owner=email, password_hash="", is_admin=False
        )
        credential.password_hash = hash_password(body.password)
        credential.invitation_hash = None
        credential.invitation_expires = None
        db.add(credential)
        db.commit()
        valid = True
    if not valid or not member or not member.active:
        raise HTTPException(
            401,
            "Sign-in failed. Existing team members can use the password previously "
            "provided by the administrator or choose Forgot password. New users "
            "should choose Register.",
        )
    return sign_in(response, request, member, credential)


class Accept(Input):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=200)


@router.post("/login/activate")
def activate(body: Accept, request: Request, response: Response, db: Session = Depends(get_db)):
    guard(request)
    if settings.read_only:
        raise HTTPException(403, "Read-only mode")
    throttle("activate:"+(request.client.host if request.client else "unknown"))
    encoded = hash_password(body.password)
    db.execute(text("BEGIN IMMEDIATE"))
    credential = db.scalar(select(WorkspaceCredential).where(WorkspaceCredential.invitation_hash == hashlib.sha256(body.token.encode()).hexdigest()))
    if not credential or not credential.invitation_expires or credential.invitation_expires < datetime.utcnow():
        raise HTTPException(400, "Invitation expired or already used. Ask your admin for a new link.")
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == credential.owner))
    if not member or not member.active:
        raise HTTPException(403, "Account unavailable")
    credential.password_hash = encoded
    credential.invitation_hash = None
    credential.invitation_expires = None
    db.commit()
    return sign_in(response, request, member, credential)


class Invite(Input):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)


class Recovery(Input):
    email: str = Field(min_length=3, max_length=320)


class ChangePassword(Input):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


@router.post("/accounts/password")
def change_password(body: ChangePassword, request: Request, response: Response, db: Session = Depends(get_db)):
    guard(request)
    if settings.read_only:
        raise HTTPException(403, "Read-only mode")
    user = current_user(request.cookies.get(COOKIE_NAME), db)
    if not user["authenticated"] or not user["email"]:
        raise HTTPException(401, "Sign in first")
    throttle("change-password:" + user["email"])
    credential = db.get(WorkspaceCredential, user["email"])
    if not credential or not verify_password(body.current_password, credential.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    credential.password_hash = hash_password(body.new_password)
    credential.invitation_hash = None
    credential.invitation_expires = None
    db.commit()
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == user["email"]))
    return sign_in(response, request, member, credential)


def send_account_link(email: str, token: str, registration: bool, *, dashboard_base: str = ""):
    from app.services import email_service
    base = (dashboard_base or settings.dashboard_url).rstrip("/")
    if urlparse(base).scheme not in ("http", "https") or not urlparse(base).netloc:
        raise HTTPException(503, "Account email service is not configured. Please contact your administrator.")
    link = base + "/#setup=" + token
    action = "Verify your email and create your password" if registration else "Reset your password"
    email_service.send_alert(
        "CMS: " + action,
        action + " using this single-use link:\n\n" + link +
        "\n\nThis link expires in one hour. If you did not request it, ignore this email. Your existing password remains unchanged until you complete the reset.",
        to=email,
    )


def request_account_email(body, request, db, registration=False):
    from app.services import email_service
    guard(request)
    if settings.read_only:
        raise HTTPException(403, "Account changes are unavailable in read-only mode")
    email = body.email.strip().lower()
    if email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@")):
        raise HTTPException(422, "Enter a valid email address")
    throttle("account-mail:" + email)
    throttle("account-mail-ip:" + (request.client.host if request.client else "unknown"))
    if not email_service.is_configured():
        raise HTTPException(503, "Account email service is not configured. Please contact your administrator.")
    message = {"message": "If this address is eligible, you will receive an email with the next steps. Please check your inbox and spam folder."}
    db.execute(text("BEGIN IMMEDIATE"))
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
    credential = db.get(WorkspaceCredential, email)
    if member and not member.active:
        db.rollback()
        return message
    if registration:
        # Register never resets an existing account or changes its name/role.
        if credential and credential.password_hash:
            db.rollback()
            return message
        if not member:
            member = TeamMember(email=email, name=body.name.strip(), auto_send=False)
            db.add(member)
    elif not member:
        db.rollback()
        return message
    credential = credential or WorkspaceCredential(owner=email, password_hash="", is_admin=False)
    token = secrets.token_urlsafe(32)
    credential.invitation_hash = hashlib.sha256(token.encode()).hexdigest()
    credential.invitation_expires = datetime.utcnow() + timedelta(hours=1)
    db.add(credential)
    db.commit()
    try:
        # ``guard`` has already proved that Origin is either the same origin as
        # this request or an explicitly configured frontend.  Prefer that
        # validated browser origin for the email link: it is the address the
        # person is demonstrably using.  Looking only in ``cors_origins`` broke
        # production on EC2 because same-origin traffic does not need to appear
        # in the CORS allow-list, so a stale HTTPS setting was used even though
        # the dashboard was actually served over HTTP.
        origin = request.headers.get("origin", "").rstrip("/")
        frontend = origin if origin and origin != "*" else ""
        send_account_link(email, token, registration, dashboard_base=frontend)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "The email could not be sent. Please try again later or contact your administrator.")
    return message


class Register(Invite):
    password: str = Field(min_length=12, max_length=200)


@router.post("/login/register")
def register(body: Register, request: Request, response: Response, db: Session = Depends(get_db)):
    guard(request)
    if settings.read_only:
        raise HTTPException(403, "Read-only mode")
    email = body.email.strip().lower()
    if email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@")) or not body.name.strip():
        raise HTTPException(422, "Enter a valid name and email address")
    throttle("register-ip:" + (request.client.host if request.client else "unknown"))
    throttle("register-email:" + email)
    encoded = hash_password(body.password)
    db.execute(text("BEGIN IMMEDIATE"))
    # Existing team members from the shared-password dashboard already have an
    # identity even though they may not have a WorkspaceCredential yet.  Let
    # them enter that configured shared password here as well as on Sign in, so
    # choosing the wrong tab cannot strand them.  Any other password must go
    # through Forgot password and email verification; registration never
    # overwrites an established personal password.
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
    credential = db.get(WorkspaceCredential, email)
    legacy = bool(
        member and member.active
        and not (credential and credential.password_hash)
        and settings.dashboard_password
        and password_matches(body.password)
    )
    if legacy:
        credential = credential or WorkspaceCredential(
            owner=email, password_hash="", is_admin=False
        )
        credential.password_hash = encoded
        credential.invitation_hash = None
        credential.invitation_expires = None
        db.add(credential)
        db.commit()
        return sign_in(response, request, member, credential)
    if member or credential:
        db.rollback()
        raise HTTPException(
            409,
            "This work email is already ready to use. Select Sign in and use the "
            "password previously provided by your administrator, or select Forgot "
            "password to create a new personal password.",
        )
    member = TeamMember(email=email, name=body.name.strip(), auto_send=False)
    credential = WorkspaceCredential(owner=email, password_hash=encoded, is_admin=False)
    db.add_all([member, credential])
    db.commit()
    return sign_in(response, request, member, credential)


@router.post("/login/forgot-password")
def forgot_password(body: Recovery, request: Request, db: Session = Depends(get_db)):
    return request_account_email(body, request, db)


@router.post("/accounts/invite")
def invite(body: Invite, user=Depends(administrator), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@")):
        raise HTTPException(422, "Enter a valid email address")
    db.execute(text("BEGIN IMMEDIATE"))
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
    if member and not member.active:
        raise HTTPException(403, "Reactivate this team member before inviting them")
    if not member:
        member = TeamMember(email=email, name=body.name.strip(), auto_send=False)
        db.add(member)
    credential = db.get(WorkspaceCredential, email) or WorkspaceCredential(owner=email, password_hash="", is_admin=False)
    token = secrets.token_urlsafe(32)
    credential.invitation_hash = hashlib.sha256(token.encode()).hexdigest()
    credential.invitation_expires = datetime.utcnow()+timedelta(hours=24)
    db.add(credential)
    db.commit()
    return {"setup_path": "/#setup="+token, "expires_hours":24}


@router.get("/accounts")
def accounts(user=Depends(administrator), db: Session = Depends(get_db)):
    return [{"email": m.email, "name": m.name, "active": m.active, "is_admin": bool(c and c.is_admin), "configured": bool(c and c.password_hash)}
            for m, c in db.execute(select(TeamMember, WorkspaceCredential).outerjoin(WorkspaceCredential, WorkspaceCredential.owner == func.lower(TeamMember.email)).order_by(TeamMember.name))]


class Role(Input):
    email: str = Field(max_length=320)
    is_admin: bool


@router.put("/accounts/role")
def role(body: Role, user=Depends(administrator), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if email == user["email"]:
        raise HTTPException(400, "You cannot change your own administrator role")
    credential = db.get(WorkspaceCredential, email)
    member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
    if not credential or not credential.password_hash or not member or not member.active:
        raise HTTPException(400, "The user must activate their account first")
    credential.is_admin = body.is_admin
    db.commit()
    return {"ok": True}
