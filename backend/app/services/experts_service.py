"""Expert-pool counter — reads DevelopmentAid experts-search result totals
for each configured vertical (backend/data/expert_verticals.json).

Runs in a logged-in Playwright session (membership recommended); each URL's
"N total results" counter is stored per vertical for the dashboard card.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.database.db import session_scope
from app.database.models import ExpertCount

log = logging.getLogger("scraper")

VERTICALS_FILE = Path(__file__).resolve().parents[2] / "data" / "expert_verticals.json"

_TOTAL_PATTERNS = [
    re.compile(r"([\d,]+)\s+total\s+results", re.IGNORECASE),
    re.compile(r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)", re.IGNORECASE),
]


def load_verticals() -> dict[str, str]:
    if not VERTICALS_FILE.exists():
        return {}
    data = json.loads(VERTICALS_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and v.strip()}


def _count_all_sync(verticals: dict[str, str]) -> dict[str, int]:
    """One browser session: login once, visit each vertical URL, read its total."""
    from playwright.sync_api import sync_playwright

    from app.scrapers.devaid_auth import open_persistent

    results: dict[str, int] = {}
    with sync_playwright() as pw:
        browser = open_persistent(pw, headless=True)
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            for name, url in verticals.items():
                try:
                    page.goto(url, timeout=int(settings.request_timeout * 1000))
                    try:  # wait until the counter holds an actual number
                        page.wait_for_function(
                            """() => {
                                const el = document.querySelector('.search-total-items');
                                return el && /\d/.test(el.textContent);
                            }""",
                            timeout=25_000,
                        )
                    except Exception:
                        pass
                    count = _extract_total(page)
                    if count is not None:
                        results[name] = count
                        log.info("[experts] %s: %s experts", name, count)
                    else:
                        log.warning("[experts] %s: total counter not found — saving page", name)
                        try:
                            debug_dir = settings.log_dir.parent / "data" / "debug"
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            safe = name.split(" ")[0].lower()
                            (debug_dir / f"experts_{safe}.html").write_text(
                                page.content(), encoding="utf-8"
                            )
                        except OSError:
                            pass
                except Exception as exc:
                    log.error("[experts] %s failed: %s: %s", name, type(exc).__name__, exc)
        finally:
            browser.close()
    return results


def _extract_total(page) -> int | None:
    # counter widget (same component family as the grants search)
    for sel in (".search-total-items", ".mobile-search-counter", "da-search-counter"):
        el = page.query_selector(sel)
        if el is not None:
            digits = re.sub(r"[^\d]", "", el.inner_text())
            if digits:
                return int(digits)
    body = page.content()
    for pat in _TOTAL_PATTERNS:
        m = pat.search(body)
        if m:
            return int(m.group(1).replace(",", ""))
    # last resort: 'N results/experts/CVs' anywhere in visible text
    m = re.search(r"([\d,]{1,9})\s+(results|experts|CVs)", body, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


async def refresh_counts() -> list[dict]:
    verticals = load_verticals()
    if not verticals:
        raise ValueError(
            "No vertical URLs configured yet — fill backend/data/expert_verticals.json"
        )
    counts = await asyncio.to_thread(_count_all_sync, verticals)
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        for name, count in counts.items():
            row = db.query(ExpertCount).filter(ExpertCount.vertical == name).first()
            if row is None:
                row = ExpertCount(vertical=name)
                db.add(row)
            row.count = count
            row.search_url = verticals[name]
            row.updated_at = now
    return get_counts()


# Maps an Expert Pool vertical name (DevelopmentAid's own label, e.g. "E4C
# (Research & Community Engagement)") to a canonical vertical from the
# six-vertical system (services/verticals.py) via keyword hints — these are
# two distinct taxonomies that happen to share the word "vertical", so keep
# the canonical side named distinctly to avoid confusing the two.
_CANONICAL_VERTICAL_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("Climate/Sustainability", ("climate", "environment", "sustain", "energy", "water")),
    ("Health", ("health", "medical", "nutrition", "wash")),
    ("Livelihood", ("agri", "rural", "livelihood", "farm", "food")),
    ("Worker Wellbeing", ("worker", "labour", "labor", "workplace", "supply chain")),
    ("Innovative Finance", ("finance", "invest", "micro", "bank", "economic")),
    ("E4C", ("research", "education", "community", "evaluation", "monitoring", "social")),
]


def canonical_vertical_for(expert_vertical: str) -> str:
    low = expert_vertical.lower()
    for canonical, hints in _CANONICAL_VERTICAL_HINTS:
        if any(h in low for h in hints):
            return canonical
    return ""


def get_counts() -> list[dict]:
    with session_scope() as db:
        rows = db.query(ExpertCount).order_by(ExpertCount.vertical).all()
        return [
            {"vertical": r.vertical, "count": r.count, "search_url": r.search_url,
             "canonical_vertical": canonical_vertical_for(r.vertical),
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in rows
        ]
