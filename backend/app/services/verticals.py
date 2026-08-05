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
        r"\bagri[\s-]?", r"\bagro[\s-]?", r"\bcrop(s)?\b", r"fisher(y|ies|men)",
        r"livestock",
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
        # --- from "Keyword Searches Vertical Wise.xlsx", Livelihood row ---
        # Terms in that row that belong to another vertical are routed there
        # instead (M&E/Research -> E4C, Environment & Climate -> Climate,
        # WASH -> Health, HR & Employment -> Worker Wellbeing), keeping
        # Livelihood focused on agriculture / food / rural / livelihoods.
        r"aquaculture", r"food\s+system", r"sustainable\s+livelihood",
        r"social\s+development", r"agriculture\s*(&|and)\s*rural",
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
        # --- from "Keyword Searches Vertical Wise.xlsx", E4C row ---
        r"impact\s+(assessment|study|evaluation)",
        r"(baseline|endline|midline|midterm|longitudinal)\s+(study|assessment)",
        r"research\s+project", r"csr\s+project", r"\bevaluation\b",
        r"data\s+collection", r"consult(ing|ancy)", r"\bsroi\b",
        r"outcome\s+harvesting", r"outcome\s+assessment",
        r"policy\s+evaluation", r"value\s+chain\s+stud",
        # routed here from the Livelihood row (research/evidence work)
        r"monitoring\s*(&|and)\s*evaluation", r"research\s*(&|and)\s*innovation",
        r"statistics\s*(and|&)\s*data\s+analysis",
        r"education,?\s*training\s*(&|and)\s*capacity",
        r"organi[sz]ational\s+development",
    ],
    VERTICAL_CLIMATE: [
        r"climate", r"environment", r"sustainab", r"renewable", r"\bsolar\b",
        r"carbon", r"emission", r"biodiversity", r"conservation", r"green\s+energy",
        r"clean\s+energy", r"resilien(ce|t)", r"\bforest", r"waste\s+management",
        r"blue\s+(economy|ocean)",
        r"circular\s+economy", r"\bwind\s+energy\b", r"net[\s-]?zero", r"adaptation",
        r"mitigation", r"ecolog", r"pollution", r"water\s+resource",
        # broadened: more specific environmental/climate terms
        r"decarbon", r"afforestation", r"reforestation", r"deforestation",
        r"\bocean\b", r"marine", r"coastal", r"wildlife", r"ecosystem",
        r"natural\s+resource\s+manag", r"\bnrm\b", r"disaster\s+risk\s+reduc",
        r"\bdrr\b", r"\bflood(s|ing)?\b", r"drought", r"air\s+quality",
        r"plastic\s+waste", r"recycl", r"green\s+build", r"electric\s+vehicle",
        # --- routed here from the spreadsheet's Livelihood row ---
        # ("Environment & Climate" and "Energy" belong to this vertical rather
        #  than Livelihood; the Climate row itself was blank in the sheet, so
        #  the existing Climate keywords above are kept unchanged.)
        r"environment\s*(&|and)\s*climate", r"\benergy\b",
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
        # --- from "Keyword Searches Vertical Wise.xlsx", Worker Well Being row ---
        # The sheet lists ~35 phrasings of the same few ideas (employee
        # wellbeing, workplace gender equality, occupational health); these
        # patterns cover the whole set rather than repeating each variant.
        r"employee(s)?\s+(well[\s-]?being|wellbeing|wellness|mental)",
        r"well[\s-]?being\s+(of|for)\s+(employee|worker|staff)",
        r"well[\s-]?being\s+at\s+work", r"social\s+well[\s-]?being\s+at\s+work",
        r"wellness\s+of\s+employee", r"health\s+and\s+wellness\s+of\s+employee",
        r"workforce\s+(well[\s-]?being|wellbeing|development|empowerment)",
        r"skilled\s+workforce", r"workplace\s+(health|wellbeing|well[\s-]?being)",
        r"health(y)?\s+work(place|ing)\s+environment",
        r"occupational\s+(health|safety)",
        r"gender\s+(equality|justice|sensitivity)[^.]{0,25}workplace",
        r"workplace[^.]{0,25}gender\s+(equality|justice|security)",
        r"inclusion\s+at\s+(the\s+)?workplace",
        r"mental\s+health\s+support[^.]{0,20}workplace",
        r"mentorship\s+program[^.]{0,15}employee",
        r"resilience\s+training\s+at\s+work",
        r"menstrual\s+hygiene", r"women\s+safety[^.]{0,15}work",
        r"women('s)?\s+empowerment", r"gender[\s-]based\s+violence",
        # routed here from the Livelihood row ("HR & Employment")
        r"\bhr\s*(&|and)\s*employment\b",
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
        r"start[\s-]?up(s)?", r"seed\s+capital", r"seed\s+fund", r"angel\s+invest",
        r"\bmsme(s)?\b", r"micro[\s-]enterprise", r"entrepreneur",
        r"venture\s+capital", r"private\s+equity", r"green\s+bond",
        r"guarantee\s+fund", r"financial\s+literacy", r"digital\s+finance",
        r"mobile\s+money", r"savings\s+group", r"\bvsla(s)?\b",
        # --- from "Keyword Searches Vertical Wise.xlsx", Innovative Finance row ---
        r"catalytic\s+(capital|finance)", r"social\s+finance",
        r"sustainable\s+finance", r"climate\s+finance", r"resilience\s+finance",
        r"investment\s+readiness", r"technical\s+assistance\s+facility",
        r"capital\s+mobili[sz]ation", r"private\s+capital\s+mobili[sz]ation",
        r"first[\s-]loss\s+capital", r"guarantee\s+facility",
        r"revolving\s+grant", r"interest\s+subvention",
        r"concessional\s+(finance|grant)", r"outcomes?\s+fund",
        r"impact[\s-]linked\s+finance", r"development\s+impact\s+bond",
        r"patient\s+capital", r"venture\s+philanthropy",
        r"social\s+impact\s+invest", r"inclusive\s+finance",
        r"innovative\s+financing\s+mechanism", r"structured\s+finance",
        r"outcome[\s-]based\s+funding", r"\bgap\s+funding\b",
        # routed here from the Livelihood row (funding/economic terms)
        r"fundraising", r"grant\s+management",
        r"macro[\s-]economy", r"public\s+finance",
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
    """Re-classify every row against the current keyword rules.

    Runs in the background at startup and is safe to run repeatedly — a row
    whose tags don't change is not written.

    This deliberately re-checks *all* rows rather than only blank ones. Tags are
    derived purely from the keyword rules (never hand-edited), so whenever those
    rules change the stored values need to catch up. Blank-only backfilling left
    two problems behind: rows tagged under an earlier keyword set never picked
    up new keywords, and ~1,000 rows still carried pre-rename labels ("E4C",
    "Climate/Sustainability") that no longer matched the canonical filter values
    ("E4C(Evidence for Change)", "Climate/Sustainability(ESG)") — so filtering by
    those two verticals silently skipped them.
    """
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    updated = 0
    with session_scope() as db:
        rows = db.execute(select(Opportunity)).scalars().all()
        for opp in rows:
            body = " ".join(filter(None, [opp.summary, opp.vertical, opp.eligibility]))
            new_value = verticals_to_str(classify_verticals(opp.title, body))
            if new_value != (opp.verticals or ""):
                opp.verticals = new_value
                updated += 1
    if updated:
        log.info("Vertical backfill: re-tagged %s opportunities", updated)
    return updated
