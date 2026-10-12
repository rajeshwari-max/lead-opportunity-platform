"""Discover source PDFs after scrapes and in resumable backfill batches.

Dashboard and email links expose only source PDFs. Missing, unchecked, blocked
or login-only documents are unavailable; generated summaries are never a fallback.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app.core.config import settings

log = logging.getLogger("scraper")

FOUND, NONE, BLOCKED, ERROR, SKIPPED = "found", "none", "blocked", "error", "skipped"

_GOOD = re.compile(
    r"(rfp|rfq|rfa|tor\b|terms[ _-]*of[ _-]*reference|call|solicitation|tender|"
    r"notice|guideline|guidance|eoi|expression[ _-]*of[ _-]*interest|"
    r"application|proposal|grant|funding|prospectus|announcement|nofo|bid|"
    r"concept[ _-]*note|scope[ _-]*of[ _-]*work|sow\b)",
    re.IGNORECASE,
)
_BAD = re.compile(
    r"(privacy|cookie|annual[ _-]*report|brochure|newsletter|code[ _-]*of[ _-]*conduct|"
    r"terms[ _-]*(of|and)[ _-]*(use|conditions|service)|catalog|magazine|press[ _-]*release|"
    r"sitemap|media[ _-]*kit|safeguarding[ _-]*policy|logo)",
    re.IGNORECASE,
)
_CHALLENGE = ("just a moment", "attention required", "checking your browser", "cf-chl",
              "challenge-platform", "turnstile", "captcha", "you have been blocked")
_STOP = {"the", "and", "for", "with", "from", "this", "that", "call", "grant", "grants",
         "proposals", "proposal", "request", "programme", "program", "fund", "2025", "2026", "2027"}
MAX_HTML_BYTES = 2_000_000


# --------------------------------------------------------------- choosing

def _is_pdf_href(url: str, anchor) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return True
    if (anchor.get("type") or "").lower() == "application/pdf":
        return True
    return bool(re.search(r"[?&](format|type|ext)=pdf\b", url, re.IGNORECASE))


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (title or "").lower()) if w not in _STOP}


def pdf_candidates(html: str, page_url: str, title: str = "") -> list[tuple[float, str, str]]:
    """Every PDF link on the page with a relevance score, best first."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    host = urlparse(page_url).netloc.lower()
    words = _title_words(title)
    seen: set[str] = set()
    out: list[tuple[float, str, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        url = urljoin(page_url, href)
        if not url.startswith(("http://", "https://")) or not _is_pdf_href(url, a):
            continue
        key = url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        text = " ".join(filter(None, [a.get_text(" ", strip=True), a.get("title", ""),
                                      a.get("aria-label", "")]))
        blob = f"{text} {urlparse(url).path}"
        score = 0.0
        if _GOOD.search(blob):
            score += 3
        overlap = len(words & _title_words(blob.replace("-", " ").replace("_", " ")))
        score += min(overlap, 3)
        if urlparse(url).netloc.lower() == host:
            score += 0.5
        if _BAD.search(blob):
            score -= 6
        out.append((score, key, text[:120]))
    out.sort(key=lambda c: -c[0])
    return out


def best_pdf(html: str, page_url: str, title: str = "") -> str | None:
    """The PDF most likely to be this call's document, or None.

    A score of at least 1 is required: a same-site PDF with nothing tying it
    to the call (a footer "Our strategy" PDF) scores 0.5 and is not used.
    """
    for score, url, _ in pdf_candidates(html, page_url, title):
        if score >= 1:
            return url
        break
    return None


# Verified original call pages. Match the source record ID, never just a title.
# Verified against the funder's public call page on 2026-10-12.
def discovery_page(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname in {"developmentaid.org", "www.developmentaid.org"} and re.match(
            r"^/grants/view/1697956(?:/|$)", parsed.path):
        return ("https://eit-culture-creativity.eu/your-opportunities/calls-funding/"
                "financial-support-startups-and-scaleups-shape-acceleration-scale")
    return url


# --------------------------------------------------------------- fetching

def _login_only_sources() -> set[str]:
    from app.scrapers.registry import SCRAPER_REGISTRY
    from app.services.source_manifest import contract_for

    names: set[str] = set()
    for key, cls in SCRAPER_REGISTRY.items():
        if getattr(cls, "requires_login", False) or contract_for(key).requires_login:
            names.add(getattr(cls, "display_name", key))
    names.add("DevelopmentAid")      # session-only; never fetched outside its scraper
    return names


async def inspect(client, url: str, title: str = "") -> tuple[str | None, str]:
    """(pdf_url, status) for one opportunity page."""
    from app.services.links import link_kind

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None, SKIPPED
    if urlparse(url).path.lower().endswith(".pdf"):
        return url, FOUND                      # the listing already links the PDF
    if link_kind(url) == "listing":
        return None, SKIPPED                   # an index page's PDFs are not this call's
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code in (401, 403, 407, 429, 503):
                return None, BLOCKED
            if resp.status_code >= 400:
                return None, ERROR
            ctype = (resp.headers.get("content-type") or "").lower()
            if "application/pdf" in ctype:
                return str(resp.url), FOUND
            if "html" not in ctype and ctype:
                return None, NONE
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_HTML_BYTES:
                    break
            final = str(resp.url)
    except Exception as exc:                                    # noqa: BLE001
        log.debug("[pdf] %s: %s", url, exc)
        return None, ERROR
    html = body.decode("utf-8", errors="replace")
    head = html[:20000].lower()
    if any(marker in head for marker in _CHALLENGE):
        return None, BLOCKED
    found = best_pdf(html, final, title)
    return (found, FOUND) if found else (None, NONE)


async def _discover(rows: list[tuple[int, str, str]], concurrency: int, budget_s: float):
    import httpx

    gate = asyncio.Semaphore(max(1, concurrency))
    deadline = time.monotonic() + budget_s
    results: dict[int, tuple[str | None, str]] = {}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0), follow_redirects=True,
        headers={"User-Agent": settings.user_agent, "Accept": "text/html,application/pdf,*/*"},
    ) as client:
        async def one(row_id: int, url: str, title: str) -> None:
            if time.monotonic() > deadline:
                return
            async with gate:
                if time.monotonic() > deadline:
                    return
                results[row_id] = await inspect(client, url, title)

        await asyncio.gather(*(one(*r) for r in rows))
    return results


def discover_pdfs(limit: int | None = None, concurrency: int | None = None,
                  budget_s: float | None = None, *, include_archived: bool = False,
                  retry_missing: bool = False) -> dict:
    """Check opportunities that have not been checked yet, newest first.

    Network work happens with no database transaction open; results are written
    afterwards in short batches, so a slow site never holds the write lock.
    """
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity
    from app.services.actionable import actionable_clause

    limit = limit or settings.pdf_discovery_limit
    skip_sources = _login_only_sources()
    from sqlalchemy import or_
    predicates = [] if include_archived else [actionable_clause()]
    predicates.append(or_(Opportunity.pdf_checked_at.is_(None), Opportunity.pdf_status != FOUND)
                      if retry_missing else Opportunity.pdf_checked_at.is_(None))
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity.id, Opportunity.opportunity_url, Opportunity.title,
                   Opportunity.source_website)
            .where(*predicates)
            .order_by(Opportunity.id.desc())
            .limit(limit)
        ).all()
    todo = [(r[0], discovery_page(r[1] or ""), r[2] or "") for r in rows
            if (r[3] or "") not in skip_sources or discovery_page(r[1] or "") != (r[1] or "")]
    todo_ids = {r[0] for r in todo}
    skipped = [r[0] for r in rows if r[0] not in todo_ids]

    results = asyncio.run(_discover(
        todo, concurrency or settings.pdf_discovery_concurrency,
        budget_s or settings.pdf_discovery_budget_s)) if todo else {}
    for row_id in skipped:
        results[row_id] = (None, SKIPPED)

    now = datetime.now(timezone.utc)
    items = list(results.items())
    for start in range(0, len(items), 50):
        with session_scope() as db:
            for row_id, (pdf_url, status) in items[start:start + 50]:
                row = db.get(Opportunity, row_id)
                if row is None:
                    continue
                row.pdf_url = (pdf_url or None) and pdf_url[:1024]
                row.pdf_status = status
                row.pdf_checked_at = now
    counts: dict[str, int] = {}
    for _, status in results.values():
        counts[status] = counts.get(status, 0) + 1
    from sqlalchemy import func
    with session_scope() as db:
        counts["unchecked_left_for_next_run"] = db.scalar(
            select(func.count()).select_from(Opportunity).where(
                Opportunity.pdf_checked_at.is_(None),
                *([] if include_archived else [actionable_clause()])))
    counts["selected"] = len(rows)
    return counts


# --------------------------------------------------------------- links

def brief_signature(opportunity_id: int) -> str:
    from app.core.auth import _sign
    return _sign(f"brief-pdf:v1:{int(opportunity_id)}")


def brief_signature_ok(opportunity_id: int, sig: str) -> bool:
    import hmac
    return hmac.compare_digest(sig or "", brief_signature(opportunity_id))


def brief_url(opportunity_id: int) -> str:
    base = (settings.public_base_url or settings.dashboard_url or "").rstrip("/")
    return (f"{base}{settings.api_prefix}/brief/{int(opportunity_id)}.pdf"
            f"?sig={brief_signature(opportunity_id)}")


def lead_link(o) -> tuple[str, str]:
    """Only source documents; never substitute a generated brief."""
    if getattr(o, "pdf_status", None) == FOUND and getattr(o, "pdf_url", None):
        return o.pdf_url, "source_pdf"
    return "", "unavailable"


