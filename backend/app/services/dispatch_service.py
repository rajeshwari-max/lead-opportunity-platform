"""Dispatch — orchestrates match → email → mark-sent, manually or after a scrape."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.database.db import session_scope
from app.database.models import Opportunity, TeamMember
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


def send_selection(opportunity_ids: list[int], member_ids: list[int]) -> list[dict]:
    """Email a hand-picked set of opportunities to chosen team members.

    Deliberately separate from the keyword-matched digest. That one answers
    "what is relevant to this person"; this one answers "I have read these and
    I want these people to see them", which no keyword rule can express.

    Recipients are team members rather than free-text addresses on purpose: the
    approval buttons in the email are signed per-recipient, and accepting
    arbitrary addresses would turn this endpoint into an open relay that
    anyone with dashboard access could point anywhere.

    Rows are NOT marked as sent. This is an extra copy on top of whatever the
    normal digest does, so marking them would suppress the scheduled email for
    opportunities the person may never have received through that route.
    """
    if not opportunity_ids:
        raise ValueError("No opportunities selected")
    if not member_ids:
        raise ValueError("No recipients selected")

    results: list[dict] = []
    with session_scope() as db:
        opportunities = list(
            db.execute(select(Opportunity).where(Opportunity.id.in_(opportunity_ids)))
            .scalars()
        )
        if not opportunities:
            raise ValueError("None of those opportunities exist any more")

        # Preserve the order the user picked them in, rather than database order.
        order = {oid: i for i, oid in enumerate(opportunity_ids)}
        opportunities.sort(key=lambda o: order.get(o.id, 10**6))

        for member_id in member_ids:
            member = db.get(TeamMember, member_id)
            if member is None:
                results.append({"member": f"#{member_id}", "sent": 0,
                                "detail": "No such team member"})
                continue
            try:
                email_service.send_digest(member, opportunities)
                results.append({"member": member.name, "sent": len(opportunities)})
            except Exception as exc:                      # one bad address must
                log.exception("Selection email to %s failed", member.email)
                results.append({"member": member.name, "sent": 0,   # not stop the rest
                                "detail": str(exc)})
    return results


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
