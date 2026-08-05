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


def send_to_member(member_id: int, resend: bool = False) -> dict:
    """Send matches to one member. Returns a small result summary.

    `resend=True` reissues the full current match set, including opportunities
    already sent before. That's the only way to get an improved email — a new
    layout, a corrected link, approval buttons — in front of someone who has
    already received the old one, since the normal path deliberately skips
    anything in the sent log and would find nothing to send.
    """
    with session_scope() as db:
        member = db.get(TeamMember, member_id)
        if member is None:
            raise LookupError(f"Team member {member_id} not found")
        svc = MatchingService(db)
        matches = svc.matches_for(member, include_sent=resend)
        if not matches:
            detail = ("Nothing currently matches this member's keywords" if resend
                      else "No new matching opportunities")
            return {"member": member.name, "sent": 0, "detail": detail}
        email_service.send_digest(member, matches)   # raises if SMTP unset/fails
        svc.mark_sent(member, matches)               # only marked sent on success
        return {"member": member.name, "sent": len(matches), "resent": resend}


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
    """Called by ScraperManager after every completed scrape.

    Sends only what is genuinely new — matches_for() excludes anything already
    in the sent log — and only to members with auto_send on. Controlled by the
    "send as soon as a scrape finishes" switch in the dashboard; with it off,
    new opportunities simply wait for the next daily run instead.
    """
    from app.services.email_settings import load

    if not load().send_on_scrape:
        log.info("Post-scrape email skipped — send_on_scrape is off")
        return

    results = await asyncio.to_thread(send_to_all_active)
    sent = sum(r.get("sent", 0) for r in results)
    if sent:
        log.info("Auto-digest: emailed %s new opportunities across %s member(s)", sent, len(results))
    else:
        log.info("Post-scrape email: nothing new to send")
