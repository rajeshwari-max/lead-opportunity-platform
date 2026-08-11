"""Opportunity link validation and repair.

The recurring complaint is that clicking an opportunity opens the funder's
homepage rather than the call itself. Auditing the database found three
distinct causes, none of which is "the scraper picked the wrong anchor":

  1. 5,234 DevelopmentAid rows hold a bare slug with no scheme or domain
     ("loan-51157-001-ino-flood-management..."). A browser resolves that
     relative to whatever page it is on, which is how a link in an email ends
     up somewhere unrelated. These pre-date the fix in the DevelopmentAid
     scraper and were never re-written.

  2. 943 rows carry the old donorId links ("/tenders/view/118345,118364"),
     where several rows share one URL that opens nothing in particular.

  3. 17 rows are not opportunities at all — "mailto:" and "javascript:void(0)"
     anchors scraped from page furniture, with titles like an email address or
     "Skip to main content".

A link that goes somewhere wrong is worse than a link that is absent: it costs
the reader a click and their trust in every other link in the list. So anything
that cannot be verified as a deep link is cleared, and the UI falls back to
offering the source site explicitly labelled as such.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Several ids joined by commas — the donorIds bug. The record's own id is not
# recoverable from these, so they cannot be repaired, only dropped.
_MULTI_ID = re.compile(r"/view/\d+(?:,\d+)+/?$")

# Anchors that are page furniture rather than a destination.
_NOT_A_DESTINATION = ("mailto:", "javascript:", "tel:", "#", "data:")


def is_usable_link(url: str, website: str = "") -> bool:
    """True when the URL plausibly points at one specific opportunity."""
    u = (url or "").strip()
    if not u or u.lower().startswith(_NOT_A_DESTINATION):
        return False

    parsed = urlparse(u)
    # No scheme/host: a bare slug or a relative path that only resolves
    # correctly on the page it was scraped from.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # Domain root with no path — that is the homepage, which is exactly the
    # complaint this module exists to fix.
    path = parsed.path.strip("/")
    if not path and not parsed.query:
        return False

    if _MULTI_ID.search(u):
        return False

    # Identical to the source's own homepage.
    if website and u.rstrip("/") == website.rstrip("/"):
        return False

    return True


_LISTING_PATH = re.compile(
    r"/(funding|grants?|tenders?|opportunit(y|ies)|calls?(-for-[\w-]+)?|rfp\w*|"
    r"programs?|programmes?|apply|how-to-apply|search|search-results|"
    r"grants-funding|funding-opportunities|open-calls?)/?$",
    re.IGNORECASE,
)
_SEARCH_QUERY = re.compile(r"[?&](s|q|search|keyword)=", re.IGNORECASE)


def link_kind(url: str) -> str:
    """'deep' for a per-opportunity page, 'listing' for an index/search page.

    Some sources genuinely never publish a per-call URL — DevNetJobsIndia lists
    every RFP on one .aspx page, SNF exposes only a search view. For those, the
    listing page is the sole route to the opportunity, so clearing it would
    remove the only way in. Labelling it is the honest option: the reader is
    told they will land on a list and still need to find the row, rather than
    discovering that after the click.

    Limits of doing this from the URL alone: a programme landing page like
    /programs/global-foods/ is indistinguishable from a real call at
    /programs/water-fund-2026/. Only fetching the page settles those, which is
    what scripts/check_links.py is for.
    """
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    path = (parsed.path or "").rstrip("/")
    depth = len([seg for seg in path.split("/") if seg])
    if _SEARCH_QUERY.search(u) or _LISTING_PATH.search(path):
        return "listing"
    if depth <= 1 and not parsed.query:
        return "listing"
    return "deep"


# Some boards expose each record twice: a machine endpoint that serves a file
# download, and the human page. Scrapers naturally grab whichever anchor is in
# the markup, and on the UN Partner Portal that is the export API — clicking it
# in the dashboard downloads a blob instead of opening the call. Same record,
# so this is a rewrite, not a deletion.
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(https://(?:www\.)?unpartnerportal\.org)/api/public/export/projects/(\d+)/?$",
                re.IGNORECASE), r"\1/landing/opportunities/\2/"),
    (re.compile(r"^(https://(?:www\.)?unpartnerportal\.org)/api/public/projects/(\d+)/?$",
                re.IGNORECASE), r"\1/landing/opportunities/\2/"),
)


def canonical_link(url: str) -> str:
    """Swap a known machine/download endpoint for its human-readable page."""
    u = (url or "").strip()
    for pattern, repl in _REWRITES:
        new = pattern.sub(repl, u)
        if new != u:
            return new
    return u


def repair_links() -> dict:
    """Clear unusable opportunity_url values on existing rows.

    Idempotent. Returns a small summary so the effect is visible in the log
    rather than being a silent mass update.
    """
    import logging

    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    stats = {"checked": 0, "cleared": 0, "kept": 0, "rewritten": 0}

    with session_scope() as db:
        for opp in db.execute(select(Opportunity)).scalars():
            stats["checked"] += 1
            if not (opp.opportunity_url or "").strip():
                continue
            fixed = canonical_link(opp.opportunity_url)
            if fixed != opp.opportunity_url:
                opp.opportunity_url = fixed
                stats["rewritten"] += 1
            if is_usable_link(opp.opportunity_url, opp.website):
                stats["kept"] += 1
            else:
                opp.opportunity_url = ""
                stats["cleared"] += 1

    if stats["cleared"] or stats["rewritten"]:
        log.info("Link repair: cleared %s, rewritten %s, of %s checked",
                 stats["cleared"], stats["rewritten"], stats["checked"])
    return stats


def junk_rows() -> list[tuple[int, str, str, str]]:
    """Rows that look like scraped page furniture rather than opportunities.

    Reported, never deleted automatically — removing rows is the user's call.
    """
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    email_like = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
    furniture = {"skip to main content", "read more", "apply now", "click here",
                 "learn more", "subscribe", "menu", "home"}

    out = []
    with session_scope() as db:
        for opp in db.execute(select(Opportunity)).scalars():
            title = (opp.title or "").strip()
            if email_like.match(title) or title.lower() in furniture or len(title) < 8:
                out.append((opp.id, opp.source_website, title, opp.opportunity_url or ""))
    return out
