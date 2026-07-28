"""Vertical system — the six organisational verticals and a keyword
classifier that tags every opportunity with one or more of them.

Verticals are stored on Opportunity.verticals as a comma-separated canonical
list (e.g. "Health, Climate/Sustainability"). Classification happens during
scraping (ScraperManager._ingest) and via the one-time startup backfill.

Swap in an ML/LLM classifier later by implementing `classify_verticals` with
the same signature.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("scraper")

# Canonical vertical labels — single source of truth (mirrored in frontend types.ts).
VERTICAL_LIVELIHOOD = "Livelihood"
VERTICAL_HEALTH = "Health"
VERTICAL_E4C = "E4C(Evidence for Change)"
VERTICAL_CLIMATE = "Climate/Sustainability(ESG)"
VERTICAL_WWB = "Worker Wellbeing"
VERTICAL_FINANCE = "Innovative Finance"

VERTICALS: list[str] = [
    VERTICAL_LIVELIHOOD,
    VERTICAL_HEALTH,
    VERTICAL_E4C,
    VERTICAL_CLIMATE,
    VERTICAL_WWB,
    VERTICAL_FINANCE,
]

# Human-readable descriptions (used by /verticals endpoint and tooltips).
VERTICAL_DESCRIPTIONS: dict[str, str] = {
    VERTICAL_LIVELIHOOD: "Agriculture and Rural Management",
    VERTICAL_HEALTH: "Health",
    VERTICAL_E4C: "Research and Community Engagement",
    VERTICAL_CLIMATE: "Climate / Sustainability",
    VERTICAL_WWB: "Worker Wellbeing (WWB)",
    VERTICAL_FINANCE: "Innovative Finance",
}

# Keyword rules. Title hits weigh 3x, body hits 1x; every vertical whose score
# crosses the threshold is assigned (multi-label).
_VERTICAL_KEYWORDS: dict[str, list[str]] = {
    VERTICAL_LIVELIHOOD: [
        r"agricultur", r"\bfarm(er|ing|s)?\b", r"\brural\b", r"livelihood",
        r"\bagri[\s-]?", r"\bcrop(s)?\b", r"fisher(y|ies|men)", r"livestock",
        r"\bdairy\b", r"irrigation", r"food\s+security", r"smallholder",
        r"horticultur", r"\bseed(s)?\b", r"agroforestry", r"land\s+restoration",
        r"village\s+develop", r"\bfpo(s)?\b", r"farmer\s+producer",
        # broadened: livelihoods/economic-empowerment phrasing beyond farming
        r"poverty\s+(alleviation|reduction)", r"economic\s+empowerment",
        r"vocational\s+train", r"skills?\s+train", r"income\s+generat",
        r"value\s+chain", r"cooperative(s)?", r"market\s+access",
        r"rural\s+develop", r"pastoral(ist)?", r"post[\s-]?harvest",
        r"extension\s+services", r"self[\s-]help\s+group", r"\bshg(s)?\b",
        r"artisan(s)?", r"handicraft", r"vendor(s)?\s+support",
    ],
    VERTICAL_HEALTH: [
        r"\bhealth", r"medical", r"disease", r"nutrition", r"hospital",
        r"mental\s+health", r"hygiene", r"sanitation", r"\bwash\b", r"\bhiv\b",
        r"malaria", r"tuberculosis", r"maternal", r"vaccin", r"epidemi",
        r"immuni[sz]ation", r"telemedicine", r"public\s+health", r"clinic",
        r"pharmac", r"wellness", r"disabilit",
        # broadened: more specific conditions/programs & health-system terms
        r"reproductive\s+health", r"family\s+plan", r"\bcancer\b", r"diabet",
        r"non[\s-]?communicable\s+disease", r"\bncd(s)?\b", r"\bcovid",
        r"pandemic", r"outbreak", r"health\s+system", r"primary\s+health",
        r"\bphc\b", r"diagnostic(s)?", r"\bsurger", r"patient(s)?",
        r"health(care)?\s+worker", r"medicine(s)?", r"\bdrug(s)?\b",
    ],
    VERTICAL_E4C: [
        r"\bresearch\b", r"community\s+engagement", r"monitoring\s+(and|&)\s+evaluation",
        r"\bm&e\b", r"baseline\s+(study|survey)", r"fellowship", r"scholarship",
        r"\beducation\b", r"\blearning\b", r"academic", r"universit",
        r"data\s+collection", r"impact\s+(assessment|evaluation)", r"\bstudy\b",
        r"knowledge\s+management", r"civic\s+engagement", r"youth\s+engagement",
        # broadened: capacity-building, evidence, and dissemination work
        r"capacity\s+(building|strengthening|develop)", r"technical\s+assistance",
        r"policy\s+research", r"\badvocacy\b", r"think\s+tank",
        r"curriculum", r"training\s+program", r"peer\s+review",
        r"qualitative\s+research", r"quantitative\s+research", r"data\s+analysis",
        r"pilot\s+project", r"proof\s+of\s+concept", r"innovation\s+lab",
        r"conference|symposium|workshop",
    ],
    VERTICAL_CLIMATE: [
        r"climate", r"environment", r"sustainab", r"renewable", r"\bsolar\b",
        r"carbon", r"emission", r"biodiversity", r"conservation", r"green\s+energy",
        r"clean\s+energy", r"resilience", r"\bforest", r"waste\s+management",
        r"circular\s+economy", r"\bwind\s+energy\b", r"net[\s-]?zero", r"adaptation",
        r"mitigation", r"ecolog", r"pollution", r"water\s+resource",
        # broadened: more specific environmental/climate terms
        r"decarbon", r"afforestation", r"reforestation", r"deforestation",
        r"\bocean\b", r"marine", r"coastal", r"wildlife", r"ecosystem",
        r"natural\s+resource\s+manag", r"\bnrm\b", r"disaster\s+risk\s+reduc",
        r"\bdrr\b", r"\bflood(s|ing)?\b", r"drought", r"air\s+quality",
        r"plastic\s+waste", r"recycl", r"green\s+build", r"electric\s+vehicle",
    ],
    VERTICAL_WWB: [
        r"\bworker(s)?\b", r"\blabou?r\b", r"occupational", r"workplace",
        r"garment", r"factor(y|ies)", r"supply\s+chain", r"\bwage(s)?\b",
        r"migrant\s+worker", r"social\s+protection", r"decent\s+work",
        r"informal\s+(sector|economy|worker)", r"gig\s+(worker|economy)",
        r"employee\s+well[\s-]?being", r"child\s+labou?r", r"forced\s+labou?r",
        # broadened: workplace-rights and worker-protection terms
        r"human\s+traffick", r"modern\s+slavery",
        r"occupational\s+health\s+and\s+safety", r"\bohs\b",
        r"trade\s+union", r"collective\s+bargain", r"minimum\s+wage",
        r"workers?\s+rights", r"labou?r\s+rights", r"domestic\s+worker",
        r"safe\s+workplace", r"grievance\s+mechanism",
    ],
    VERTICAL_FINANCE: [
        r"impact\s+invest", r"blended\s+finance", r"microfinance",
        r"financial\s+inclusion", r"fintech", r"social\s+enterprise",
        r"\bventure\b", r"innovative\s+financ", r"outcome[\s-]based\s+financ",
        r"social\s+impact\s+bond", r"\bmicro[\s-]?credit\b", r"\bmicro[\s-]?insurance\b",
        r"catalytic\s+capital", r"development\s+finance", r"\bcrowdfund",
        r"results[\s-]based\s+financ", r"pay[\s-]for[\s-]success",
        # broadened: enterprise/business-support and financial-services terms
        r"business\s+grant", r"enterprise\s+develop", r"\bsme(s)?\b",
        r"small\s+and\s+medium\s+enterprise", r"small\s+business",
        r"start[\s-]?up(s)?", r"seed\s+capital", r"angel\s+invest",
        r"venture\s+capital", r"private\s+equity", r"green\s+bond",
        r"guarantee\s+fund", r"financial\s+literacy", r"digital\s+finance",
        r"mobile\s+money", r"savings\s+group", r"\bvsla(s)?\b",
    ],
}

_COMPILED: dict[str, list[re.Pattern[str]]] = {
    vertical: [re.compile(p, re.IGNORECASE) for p in patterns]
    for vertical, patterns in _VERTICAL_KEYWORDS.items()
}

_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1
_THRESHOLD = 2  # a single body hit alone is too weak; title hit or 2+ body hits qualify


def classify_verticals(title: str, body: str = "") -> list[str]:
    """Return every canonical vertical this opportunity belongs to (may be empty).

    Multi-label by design: a "solar irrigation for farmers" grant is both
    Climate/Sustainability and Livelihood.
    """
    matched: list[str] = []
    for vertical in VERTICALS:  # preserve canonical ordering in the output
        score = 0
        for pat in _COMPILED[vertical]:
            if pat.search(title):
                score += _TITLE_WEIGHT
            if body and pat.search(body):
                score += _BODY_WEIGHT
            if score >= _THRESHOLD:
                break
        if score >= _THRESHOLD:
            matched.append(vertical)
    return matched


def verticals_to_str(verticals: list[str]) -> str:
    return ", ".join(verticals)


def str_to_verticals(value: str) -> list[str]:
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def backfill_verticals() -> int:
    """One-time enrichment: classify rows that don't have canonical verticals yet.

    Runs in the background at startup; safe to run repeatedly (idempotent —
    only touches rows with an empty verticals column).
    """
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    updated = 0
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity).where(Opportunity.verticals == "")
        ).scalars().all()
        for opp in rows:
            body = " ".join(filter(None, [opp.summary, opp.vertical, opp.eligibility]))
            tags = classify_verticals(opp.title, body)
            if tags:
                opp.verticals = verticals_to_str(tags)
                updated += 1
    if updated:
        log.info("Vertical backfill: tagged %s existing opportunities", updated)
    return updated
