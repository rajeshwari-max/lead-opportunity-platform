"""Individual sign-in with live database roles and revocable sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.core.config import settings

COOKIE_NAME = "lop_session"
SESSION_DAYS = 30


def auth_required() -> bool:
    return settings.personal_login


def validate_origin(request) -> None:
    """Allow same-origin writes and exact configured frontend origins (Vite proxy)."""
    from urllib.parse import urlsplit
    from fastapi import HTTPException
    origin = request.headers.get("origin")
    if not origin or request.method in ("GET", "HEAD"):
        return
    allowed = {str(request.base_url).rstrip("/")}
    allowed.update(o.rstrip("/") for o in settings.cors_origins if o != "*")
    dashboard = urlsplit(settings.dashboard_url)
    if dashboard.scheme in ("http", "https") and dashboard.netloc:
        allowed.add(f"{dashboard.scheme}://{dashboard.netloc}")
    if origin.rstrip("/") not in allowed:
        raise HTTPException(403, "Cross-origin request blocked")


def allowed_domains() -> list[str]:
    return [d.strip().lower().lstrip("@")
            for d in (settings.allowed_email_domains or "").split(",") if d.strip()]


def domain_allowed(email: str) -> bool:
    """Is this address at one of the company domains we auto-admit?

    Matches the domain exactly, and any subdomain of it, so
    india.catalysts.org passes for "catalysts.org" — but notcatalysts.org does
    not, which a bare "endswith" would have wrongly accepted.
    """
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[1]
    return any(domain == d or domain.endswith("." + d) for d in allowed_domains())


def admin_required() -> bool:
    return True


def _sign(body: str) -> str:
    digest = hmac.new(settings.approval_secret.encode(), body.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_session_token(email: str, name: str, is_admin: bool, version: str = "") -> str:
    payload = {
        "email": email,
        "version": version,
        "kind": "personal-v1",
        "name": name,
        "admin": bool(is_admin),
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body)}"


def read_session(token: str | None) -> dict | None:
    """Decode a session cookie, or None when it is absent, forged or expired."""
    if not token:
        return None
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    # compare_digest, not ==, so a wrong signature can't be discovered one byte
    # at a time from response timing.
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def current_user(token: str | None, db=None) -> dict:
    denied = {"authenticated": False, "email": "", "name": "", "is_admin": False}
    if not auth_required():
        return {"authenticated": True, "email": "", "name": "Local", "is_admin": True}
    session = read_session(token)
    if not session or session.get("kind") != "personal-v1" or not session.get("version"):
        return denied
    from app.database.db import SessionLocal
    from app.database.models import WorkspaceCredential, TeamMember
    from sqlalchemy import select, func
    owned = db is None
    db = db or SessionLocal()
    try:
        email = session.get("email", "").strip().lower()
        credential = db.get(WorkspaceCredential, email)
        member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email) == email))
        if not credential or not member or not member.active or not credential.password_hash:
            return denied
        if not hmac.compare_digest(session["version"], password_version(credential.password_hash)):
            return denied
        return {"authenticated": True, "email": email, "name": member.name, "is_admin": credential.is_admin}
    finally:
        if owned:
            db.close()


def password_version(encoded: str) -> str:
    return hashlib.sha256(encoded.encode()).hexdigest()


def hash_password(password: str) -> str:
    import secrets
    salt = secrets.token_hex(24)
    return salt + ":" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()


def verify_password(password: str, encoded: str) -> bool:
    salt, _, expected = (encoded or "not-configured:").partition(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return hmac.compare_digest(actual, expected)


def password_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", settings.dashboard_password)


def admin_password_matches(candidate: str) -> bool:
    return True and hmac.compare_digest(
        candidate or "", settings.admin_password
    )
