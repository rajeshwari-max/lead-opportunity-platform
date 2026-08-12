"""Named sessions for the dashboard.

Sign-in is an email plus a shared password. The email must belong to an active
team member, which is where the name and identity come from — the team table
already exists and is already maintained, so a second user table would only
create a list to keep in step with it.

Two passwords, two tiers:

  dashboard_password  read opportunities, approve them
  admin_password      scraper controls, team routing, email schedule

The signed cookie carries who you are, so the header can say it and — more
usefully — an approval records the actual person rather than "dashboard".

One thing stays exempt from all of this: ``/api/approve/{token}`` from a digest
email. That link's HMAC is stronger proof than a shared password, and the
recipient is in their inbox, not the dashboard.

Leaving both passwords unset disables the gate, so local development is
unchanged.
"""
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
    return bool(settings.dashboard_password)


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
    return bool(settings.admin_password)


def _sign(body: str) -> str:
    digest = hmac.new(settings.approval_secret.encode(), body.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_session_token(email: str, name: str, is_admin: bool) -> str:
    payload = {
        "email": email,
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


def current_user(token: str | None) -> dict:
    """Who this request is, in a shape the frontend can render directly.

    With no password configured there is nobody to identify, so it reports a
    local admin — that keeps single-user development working without pretending
    someone is signed in.
    """
    if not auth_required():
        return {"authenticated": True, "email": "", "name": "Local", "is_admin": True}
    session = read_session(token)
    if not session:
        return {"authenticated": False, "email": "", "name": "", "is_admin": False}
    return {
        "authenticated": True,
        "email": session.get("email", ""),
        "name": session.get("name", ""),
        # An admin password that is set but wasn't used still means "not admin",
        # even for a valid session.
        "is_admin": bool(session.get("admin")) or not admin_required(),
    }


def password_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", settings.dashboard_password)


def admin_password_matches(candidate: str) -> bool:
    return bool(settings.admin_password) and hmac.compare_digest(
        candidate or "", settings.admin_password
    )
