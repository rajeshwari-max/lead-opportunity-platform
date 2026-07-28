"""Sector (vertical) system — the six organisational sectors and a keyword
classifier that tags every opportunity with one or more of them.

Sectors are stored on Opportunity.sectors as a comma-separated canonical list
(e.g. "Health, Climate/Sustainability"). Classification happens during
scraping (ScraperManager._ingest) and via the one-time startup backfill.

Swap in an ML/LLM classifier later by implementing `classify_sectors` with the
same signature.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("scraper")

# Canonical sector labels — single source of truth (mirrored in frontend types.ts).
SECTOR_LIVELIHOOD = "Livelihood"
SECTOR_HEALTH = "Health"
SECTOR_E4C = "E4C(Evidence for Change)"
SECTOR_CLIMATE = "Climate/Sustainability(ESG)"
SECTOR_WWB = "Worker Wellbeing"
SECTOR_FINANCE = "Innovative Finance"

SECTORS: list[str] = [
    SECTOR_LIVELIHOOD,
    SECTOR_HEALTH,
    SECTOR_E4C,
    SECTOR_CLIMATE,
    SECTOR_WWB,
    SECTOR_FINANCE,
]

# Human-readable descriptions (used by /sectors endpoint and tooltips).
SECTOR_DESCRIPTIONS: dict[str, str] = {
    SECTOR_LIVELIHOOD: "Agriculture and Rural Management",
    SECTOR_HEALTH: "Health",
    SECTOR_E4C: "Research and Community Engagement",
    SECTOR_CLIMATE: "Climate / Sustainability",
    SECTOR_WWB: "Worker Wellbeing (WWB)",
    SECTOR_FINANCE: "Innovative Finance",
}

# Keyword rules. Title hits weigh 3x, body hits 1x; every sector whose score
# crosses the threshold is assigned (multi-label).
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    SECTOR_LIVELIHOOD: [
        r"agricultur", r"\bfarm(er|ing|s)?\b", r"\brural\b", r"livelihood",
        r"\bagri[\s-]?", r"\bcrop(s)?\b", r"fisher(y|ies|men)", r"livestock",
        r"\bdairy\b", r"irrigation", r"food\s+security", r"smallholder",
        r"horticultur", r"\bseed(s)?\b", r"agroforestry", r"land\s+restoration",
        r"village\s+develop", r"\bfpo(s)?\b", r"farmer\s+producer",
    ],
    SECTOR_HEALTH: [
        r"\bhealth", r"medical", r"disease", r"nutrition", r"hospital",
        r"mental\s+health", r"hygiene", r"sanitation", r"\bwash\b", r"\bhiv\b",
        r"malaria", r"tuberculosis", r"maternal", r"vaccin", r"epidemi",
        r"immuni[sz]ation", r"telemedicine", r"public\s+health", r"clinic",
        r"pharmac", r"wellness", r"disabilit",
    ],
    SECTOR_E4C: [
        r"\bresearch\b", r"community\s+engagement", r"monitoring\s+(and|&)\s+evaluation",
        r"\bm&e\b", r"baseline\s+(study|survey)", r"fellowship", r"scholarship",
        r"\beducation\b", r"\blearning\b", r"academic", r"universit",
        r"data\s+collection", r"impact\s+(assessment|evaluation)", r"\bstudy\b",
        r"knowledge\s+management", r"civic\s+engagement", r"youth\s+engagement",
    ],
    SECTOR_CLIMATE: [
        r"climate", r"environment", r"sustainab", r"renewable", r"\bsolar\b",
        r"carbon", r"emission", r"biodiversity", r"conservation", r"green\s+energy",
        r"clean\s+energy", r"resilience", r"\bforest", r"waste\s+management",
        r"circular\s+economy", r"\bwind\s+energy\b", r"net[\s-]?zero", r"adaptation",
        r"mitigation", r"ecolog", r"pollution", r"water\s+resource",
    ],
    SECTOR_WWB: [
        r"\bworker(s)?\b", r"\blabou?r\b", r"occupational", r"workplace",
        r"garment", r"factor(y|ies)", r"supply\s+chain", r"\bwage(s)?\b",
        r"migrant\s+worker", r"social\s+protection", r"decent\s+work",
        r"informal\s+(sector|economy|worker)", r"gig\s+(worker|economy)",
        r"employee\s+well[\s-]?being", r"child\s+labou?r", r"forced\s+labou?r",
    ],
    SECTOR_FINANCE: [
        r"impact\s+invest", r"blended\s+finance", r"microfinance",
        r"financial\s+inclusion", r"fintech", r"social\s+enterprise",
        r"\bventure\b", r"innovative\s+financ", r"outcome[\s-]based\s+financ",
        r"social\s+impact\s+bond", r"\bmicro[\s-]?credit\b", r"\bmicro[\s-]?insurance\b",
        r"catalytic\s+capital", r"development\s+finance", r"\bcrowdfund",
        r"results[\s-]based\s+financ", r"pay[\s-]for[\s-]success",
    ],
}

_COMPILED: dict[str, list[re.Pattern[str]]] = {
    sector: [re.compile(p, re.IGNORECASE) for p in patterns]
    for sector, patterns in _SECTOR_KEYWORDS.items()
}

_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1
_THRESHOLD = 2  # a single body hit alone is too weak; title hit or 2+ body hits qualify


def classify_sectors(title: str, body: str = "") -> list[str]:
    """Return every canonical sector this opportunity belongs to (may be empty).

    Multi-label by design: a "solar irrigation for farmers" grant is both
    Climate/Sustainability and Livelihood.
    """
    matched: list[str] = []
    for sector in SECTORS:  # preserve canonical ordering in the output
        score = 0
        for pat in _COMPILED[sector]:
            if pat.search(title):
                score += _TITLE_WEIGHT
            if body and pat.search(body):
                score += _BODY_WEIGHT
            if score >= _THRESHOLD:
                break
        if score >= _THRESHOLD:
            matched.append(sector)
    return matched


def sectors_to_str(sectors: list[str]) -> str:
    return ", ".join(sectors)


def str_to_sectors(value: str) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def backfill_sectors() -> int:
    """One-time enrichment: classify rows that don't have canonical sectors yet.

    Runs in the background at startup; safe to run repeatedly (idempotent —
    only touches rows with an empty sectors column).
    """
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    updated = 0
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity).where(Opportunity.sectors == "")
        ).scalars().all()
        for opp in rows:
            body = " ".join(filter(None, [opp.summary, opp.sector, opp.eligibility]))
            tags = classify_sectors(opp.title, body)
            if tags:
                opp.sectors = sectors_to_str(tags)
                updated += 1
    if updated:
        log.info("Sector backfill: tagged %s existing opportunities", updated)
    return updated
