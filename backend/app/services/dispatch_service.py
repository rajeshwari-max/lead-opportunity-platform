"""Dispatch — orchestrates match → email → mark-sent, manually or after a scrape."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.database.db import session_scope
from app.database.models import TeamMember
from app.services import email_service
from app.services.matching_service import MatchingService

log = logging.getLogger("scraper")


def send_to_member(member_id: int) -> dict:
    """Send all un-sent matches to one member. Returns a small result summary."""
    with session_scope() as db:
        member = db.get(TeamMember, member_id)
        if member is None:
            raise LookupError(f"Team member {member_id} not found")
        svc = MatchingService(db)
        matches = svc.matches_for(member)
        if not matches:
            return {"member": member.name, "sent": 0, "detail": "No new matching opportunities"}
        email_service.send_digest(member, matches)   # raises if SMTP unset/fails
        svc.mark_sent(member, matches)               # only marked sent on success
        return {"member": member.name, "sent": len(matches)}


def send_to_all_active() -> list[dict]:
    """Digest every active member with auto_send enabled (used post-scrape)."""
    with session_scope() as db:
        member_ids = db.execute(
            select(TeamMember.id).where(TeamMember.active == True, TeamMember.auto_send == True)  # noqa: E712
        ).scalars().all()
    results = []
    for mid in member_ids:
        try:
            results.append(send_to_member(mid))
        except email_service.EmailNotConfiguredError:
            log.warning("Auto-digest skipped: SMTP not configured")
            break
        except Exception:
            log.exception("Auto-digest failed for member %s — continuing", mid)
    return results


async def post_scrape_hook() -> None:
    """Called by ScraperManager after every completed scrape."""
    results = await asyncio.to_thread(send_to_all_active)
    sent = sum(r.get("sent", 0) for r in results)
    if sent:
        log.info("Auto-digest: emailed %s new opportunities across %s member(s)", sent, len(results))
