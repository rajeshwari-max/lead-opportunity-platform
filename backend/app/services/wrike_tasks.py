"""Wrike folder, assignee and task calls. Tokens stay on the backend."""
from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.database.models import Opportunity
from app.services.wrike_service import (
    WrikeConnectionError,
    _access_token,
    valid_wrike_host,
)


class WrikeTaskRejected(Exception):
    """Wrike definitely rejected the POST without creating a task."""


class WrikeTaskUncertain(Exception):
    """The POST may have created a task; retrying could duplicate it."""


def _wrike_get(db: Session, path: str, params: dict[str, str] | None = None) -> dict:
    token, host = _access_token(db)
    for attempt in range(2):
        try:
            response = httpx.get(
                f"https://{host}/api/v4{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if response.status_code == 401 and attempt == 0:
                token, host = _access_token(db, force=True, failed_token=token)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Unexpected Wrike response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise WrikeConnectionError("Wrike could not be reached") from exc
    raise WrikeConnectionError("Wrike could not be reached")


def get_folder(db: Session, folder_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,256}", folder_id):
        raise WrikeConnectionError("The configured Wrike folder ID is invalid")
    payload = _wrike_get(db, f"/folders/{quote(folder_id, safe='')}")
    folders = payload.get("data")
    if not isinstance(folders, list) or len(folders) != 1:
        raise WrikeConnectionError("The configured Wrike folder is unavailable")
    folder = folders[0]
    if not isinstance(folder, dict) or folder.get("id") != folder_id:
        raise WrikeConnectionError("The configured Wrike folder is unavailable")
    return folder


def contact_ids_by_email(db: Session, emails: list[str]) -> dict[str, set[str]]:
    """Only exact, active Person matches; an ambiguous email is not usable."""
    result: dict[str, set[str]] = {email.strip().lower(): set() for email in emails}
    unique_emails = list(result)
    for start in range(0, len(unique_emails), 100):
        batch = unique_emails[start:start + 100]
        payload = _wrike_get(db, "/contacts", {
            "emails": json.dumps(batch),
            "types": json.dumps(["Person"]),
            "active": "true",
        })
        contacts = payload.get("data")
        if not isinstance(contacts, list):
            raise WrikeConnectionError("Wrike returned an invalid contact list")
        for contact in contacts:
            if not isinstance(contact, dict) or contact.get("type") != "Person" or contact.get("deleted"):
                continue
            contact_id = contact.get("id")
            if not isinstance(contact_id, str) or not re.fullmatch(r"[A-Z0-9]{8}", contact_id):
                continue
            profiles = contact.get("profiles")
            if not isinstance(profiles, list):
                continue
            for profile in profiles:
                if not isinstance(profile, dict) or profile.get("active") is False:
                    continue
                email = profile.get("email")
                if isinstance(email, str) and email.strip().lower() in result:
                    result[email.strip().lower()].add(contact_id)
    return result


def valid_task_permalink(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and valid_wrike_host(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        )
    except ValueError:
        return False


def task_description(opportunity: Opportunity) -> str:
    parts = [f"Platform opportunity ID: {opportunity.id}"]
    if opportunity.organization:
        parts.append(f"Organization: {opportunity.organization[:250]}")
    if opportunity.deadline:
        parts.append(f"Opportunity deadline: {opportunity.deadline.isoformat()}")
    source = (opportunity.opportunity_url or opportunity.website or "").strip()
    try:
        valid_source = urlsplit(source).scheme in ("http", "https")
    except ValueError:
        valid_source = False
    if valid_source:
        parts.append(f"Source: {source[:1000]}")
    if opportunity.summary:
        parts.append(f"Summary: {opportunity.summary[:1500]}")
    return "<br />".join(escape(part) for part in parts)


def create_task(
    db: Session, *, folder_id: str, title: str,
    description: str, responsible_ids: list[str],
) -> dict:
    """Send exactly one POST. A timeout or invalid success is never retried."""
    token, host = _access_token(db)
    if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,256}", folder_id):
        raise WrikeTaskRejected("The configured Wrike folder ID is invalid")
    try:
        response = httpx.post(
            f"https://{host}/api/v4/folders/{quote(folder_id, safe='')}/tasks",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "title": title,
                "description": description,
                **({"responsibles": json.dumps(responsible_ids)} if responsible_ids else {}),
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise WrikeTaskUncertain("Wrike task creation outcome is uncertain") from exc

    if 400 <= response.status_code < 500 and response.status_code != 429:
        raise WrikeTaskRejected(f"Wrike rejected task creation (HTTP {response.status_code})")
    if not 200 <= response.status_code < 300:
        raise WrikeTaskUncertain("Wrike task creation outcome is uncertain")
    try:
        payload = response.json()
        task = payload["data"][0]
        task_id = task["id"]
        permalink = task["permalink"]
        if not (
            payload.get("kind") == "tasks"
            and isinstance(task_id, str) and 0 < len(task_id) <= 128
            and isinstance(permalink, str) and valid_task_permalink(permalink)
        ):
            raise ValueError("Invalid Wrike task response")
        parent_ids = task.get("parentIds")
        if isinstance(parent_ids, list) and folder_id not in parent_ids:
            raise ValueError("Wrike created the task in an unexpected folder")
        actual_responsibles = task.get("responsibleIds")
        assignment_warning = bool(responsible_ids) and (
            not isinstance(actual_responsibles, list)
            or not set(responsible_ids).issubset(actual_responsibles)
        )
        return {
            "task_id": task_id,
            "permalink": permalink,
            "assignment_warning": assignment_warning,
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WrikeTaskUncertain("Wrike task creation outcome is uncertain") from exc
