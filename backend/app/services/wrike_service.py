import re
import time
from threading import Lock
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database.models import WrikeConnection

AUTHORIZE_URL = "https://login.wrike.com/oauth2/authorize/v4"
TOKEN_URL = "https://login.wrike.com/oauth2/token"
_REFRESH_LOCK = Lock()


class WrikeConnectionError(Exception):
    """A safe-to-report Wrike connection failure (without token details)."""


class WrikeNotConnected(WrikeConnectionError):
    pass


def valid_wrike_host(host: str) -> bool:
    """Reject URLs, ports and lookalike domains before using a token host."""
    normalized = host.lower()
    return bool(re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+wrike\.com",
        normalized,
    ))


def authorization_url(state: str) -> str:
    if not settings.wrike_client_id or not settings.wrike_redirect_uri:
        raise RuntimeError("Wrike OAuth settings are incomplete")

    params = {
        "client_id": settings.wrike_client_id,
        "response_type": "code",
        "redirect_uri": settings.wrike_redirect_uri,
        "scope": "wsReadWrite",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_authorization_code(code: str) -> dict:
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.wrike_client_id,
            "client_secret": settings.wrike_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.wrike_redirect_uri,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def _token_cipher() -> Fernet:
    key = settings.wrike_token_encryption_key
    if not key:
        raise RuntimeError("Wrike token encryption key is not configured")
    return Fernet(key.encode())


def encrypt_token(token: str) -> str:
    return _token_cipher().encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    return _token_cipher().decrypt(encrypted_token.encode()).decode()


def _stored_token(encrypted_token: str) -> str:
    try:
        return decrypt_token(encrypted_token)
    except (InvalidToken, ValueError, RuntimeError) as exc:
        raise WrikeConnectionError("Saved Wrike credentials cannot be decrypted") from exc


def _refresh_access_token(
    db: Session, connection: WrikeConnection, *, force: bool = False,
    failed_token: str | None = None,
) -> tuple[str, str]:
    # This deployment runs one backend worker. The lock prevents requests in
    # that worker from reusing a refresh token that another request just rotated.
    with _REFRESH_LOCK:
        db.refresh(connection)
        if not valid_wrike_host(connection.host):
            raise WrikeConnectionError("The saved Wrike host is invalid")
        current_access = _stored_token(connection.access_token_encrypted)
        if failed_token is not None and current_access != failed_token:
            return current_access, connection.host
        if not force and connection.expires_at > int(time.time()) + 60:
            return current_access, connection.host

        try:
            response = httpx.post(
                f"https://{connection.host}/oauth2/token",
                data={
                    "client_id": settings.wrike_client_id,
                    "client_secret": settings.wrike_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": _stored_token(connection.refresh_token_encrypted),
                },
                timeout=15.0,
            )
            response.raise_for_status()
            payload = response.json()
            access = payload["access_token"]
            refresh = payload["refresh_token"]
            token_type = payload["token_type"]
            host = payload.get("host", connection.host)
            expires_in = int(payload["expires_in"])
            if not (
                isinstance(access, str) and access
                and isinstance(refresh, str) and refresh
                and isinstance(token_type, str) and token_type.lower() == "bearer"
                and isinstance(host, str) and valid_wrike_host(host)
                and 0 < expires_in <= 86400
            ):
                raise ValueError("Invalid Wrike refresh response")
            # Encrypt both replacements before updating the row. Never keep the
            # rotated refresh token only in process memory.
            connection.access_token_encrypted = encrypt_token(access)
            connection.refresh_token_encrypted = encrypt_token(refresh)
            connection.host = host.lower()
            connection.expires_at = int(time.time()) + expires_in
            db.commit()
            return access, connection.host
        except (httpx.HTTPError, SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            db.rollback()
            raise WrikeConnectionError("Wrike token refresh failed") from exc
        except Exception:
            db.rollback()
            raise


def _access_token(db: Session, *, force: bool = False,
                  failed_token: str | None = None) -> tuple[str, str]:
    connection = db.get(WrikeConnection, 1)
    if connection is None:
        raise WrikeNotConnected("Wrike has not been connected")
    if not valid_wrike_host(connection.host):
        raise WrikeConnectionError("The saved Wrike host is invalid")
    if not force and connection.expires_at > int(time.time()) + 60:
        return _stored_token(connection.access_token_encrypted), connection.host
    return _refresh_access_token(
        db, connection, force=force, failed_token=failed_token
    )


def verify_wrike_connection(db: Session) -> bool:
    """Read only the connected user's contact; return no personal details."""
    token, host = _access_token(db)
    for attempt in range(2):
        try:
            response = httpx.get(
                f"https://{host}/api/v4/contacts",
                params={"me": "true"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if response.status_code == 401 and attempt == 0:
                token, host = _access_token(
                    db, force=True, failed_token=token
                )
                continue
            response.raise_for_status()
            payload = response.json()
            if not (
                isinstance(payload, dict)
                and payload.get("kind") == "contacts"
                and isinstance(payload.get("data"), list)
                and payload["data"]
            ):
                raise ValueError("Unexpected Wrike contact response")
            return True
        except (httpx.HTTPError, ValueError) as exc:
            raise WrikeConnectionError("Wrike connection check failed") from exc
    raise WrikeConnectionError("Wrike connection check failed")
