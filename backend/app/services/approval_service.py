"""Approval — the human sign-off that gates everything downstream.

Two ways in, with deliberately different trust models:

  * the dashboard button, which the read-only mirror blocks outright; and
  * a one-click link in the digest email, which carries an HMAC signature.

The signature is what makes the second one safe. The read-only mirror exists so
that anyone holding the public URL can look but not touch, and a bare
``/approve?id=123`` endpoint would hand that power straight back — anybody could
walk the id range and approve the entire database. A signed token can only have
come from a digest we generated, names the single opportunity it applies to, and
expires, so possession of the link is the authorisation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.database.models import Opportunity

# A month. Digests are read late; a token that dies over a long weekend would
# push people back to hunting the row down by hand, which defeats the point.
TOKEN_TTL_SECONDS = 30 * 24 * 3600


class InvalidToken(Exception):
    """Token was tampered with, malformed, or has expired."""


def _secret() -> bytes:
    return settings.approval_secret.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(opportunity_id: int, by: str = "", ttl: int = TOKEN_TTL_SECONDS) -> str:
    """Signed, self-describing approval token for a single opportunity."""
    payload = {"id": int(opportunity_id), "by": by, "exp": int(time.time()) + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> dict:
    """Verify and decode a token, or raise InvalidToken."""
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise InvalidToken("malformed token") from exc

    expected = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    # compare_digest, not ==, so a wrong signature can't be discovered one byte
    # at a time by timing the response.
    if not hmac.compare_digest(sig, expected):
        raise InvalidToken("bad signature")

    try:
        payload = json.loads(_b64d(body))
    except Exception as exc:
        raise InvalidToken("unreadable payload") from exc

    if int(payload.get("exp", 0)) < time.time():
        raise InvalidToken("this approval link has expired")
    return payload


def approve_url(opportunity_id: int, by: str = "") -> str:
    """Absolute one-click URL for an email. Relative links don't work in mail."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}{settings.api_prefix}/approve/{make_token(opportunity_id, by)}"


def set_approved(db, opportunity_id: int, approved: bool, by: str = "") -> Opportunity | None:
    """Apply an approval decision. Returns None when the row doesn't exist."""
    opp = db.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id)
    ).scalar_one_or_none()
    if opp is None:
        return None

    # Re-approving an already-approved row keeps the original attribution: the
    # first sign-off is the decision, and a second click (a forwarded email, a
    # double tap) shouldn't rewrite who made it.
    if approved and not opp.approved:
        opp.approved_at = datetime.now(timezone.utc)
        opp.approved_by = by or "dashboard"
    elif not approved:
        opp.approved_at = None
        opp.approved_by = ""
    opp.approved = approved
    db.commit()
    db.refresh(opp)
    return opp
