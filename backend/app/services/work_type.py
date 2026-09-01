"""Work type — is this a research assignment or a delivery/implementation one?

Category (Grant / RFP / Tender) says what KIND of document an opportunity is.
It doesn't say who should pick it up. A ToR for an impact evaluation and a
tender for supplying vegetable seed are both "RFP", but they go to entirely
different teams.

This adds that second axis so an opportunity can be routed to the research team
or to a delivery/brand team without someone reading every title.

Deliberately three-valued. "Unclear" is a real answer — a title like
"Consultancy services required" genuinely doesn't say which it is, and guessing
would send work to the wrong team, which is worse than saying nothing.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("scraper")

RESEARCH = "Research"
IMPLEMENTATION = "Implementation"
UNCLEAR = ""

WORK_TYPES: list[str] = [RESEARCH, IMPLEMENTATION]

# Producing knowledge: studies, evaluations, assessments, analysis, advisory.
_RESEARCH = [
    r"\bresearch\b", r"\bstud(y|ies)\b", r"evaluation", r"\bassessment\b",
    r"baseline", r"endline", r"midline", r"midterm\s+review", r"\bappraisal\b",
    r"impact\s+(assessment|evaluation|study)", r"needs\s+assessment",
    r"feasibility\s+(study|assessment)", r"scoping\s+(study|exercise|review)",
    r"situation(al)?\s+analysis", r"landscape\s+(study|analysis|mapping)",
    r"market\s+(study|research|assessment)", r"value\s+chain\s+(study|analysis)",
    r"data\s+collection", r"\bsurvey\b", r"\bcensus\b", r"sampling",
    r"qualitative\s+research", r"quantitative\s+research", r"mixed[\s-]methods",
    r"monitoring\s*(and|&|,)\s*evaluation", r"\bm\s*&\s*e\b", r"\bmel\b",
    r"third[\s-]party\s+monitoring", r"verification\s+agency",
    r"documentation\s+of\s+(learning|best practice)", r"case\s+stud(y|ies)",
    r"knowledge\s+(product|management|synthesis)", r"literature\s+review",
    r"policy\s+(research|analysis|brief)", r"\bwhite\s+paper\b",
    r"outcome\s+harvesting", r"\bsroi\b", r"cost[\s-]benefit\s+analysis",
    r"process\s+documentation", r"\bconsultanc(y|ies)\b", r"\bconsultant\b",
    r"advisory\s+services", r"technical\s+assistance", r"\baudit\b",
    r"due\s+diligence", r"\breview\s+of\b", r"analytics", r"\bdashboard\b",
]

# Doing the work on the ground: delivery, supply, construction, services.
_IMPLEMENTATION = [
    r"implement(ation|ing)?\b", r"\bdeliver(y|ing)\b", r"roll[\s-]?out",
    r"\bprocurement\b", r"\bsupply\s+(of|and)\b", r"\bsupplies\b",
    r"\bpurchase\b", r"invitation\s+to\s+bid", r"\bitb\b",
    r"construction", r"\bcivil\s+works?\b", r"\brenovation\b", r"refurbish",
    r"\binstallation\b", r"\berection\b", r"\bmaintenance\b", r"\brepair\b",
    r"\bequipment\b", r"\bhardware\b", r"\bvehicles?\b", r"\bfurniture\b",
    r"\bstationery\b", r"\bprinting\b", r"\bcatering\b", r"\blogistics\b",
    r"\btransport(ation)?\b", r"\bwarehous", r"\bdistribution\b",
    r"service\s+provider", r"\boutsourc", r"\bstaffing\b", r"\bmanpower\b",
    r"training\s+(delivery|of|for)", r"capacity\s+building\s+(of|for)",
    r"\bcommunity\s+mobili", r"\bawareness\s+campaign\b", r"\boutreach\b",
    r"\bpilot\s+implementation\b", r"\bscale[\s-]?up\b",
    r"\bsoftware\s+develop", r"\bapp\s+develop", r"website\s+(develop|rebuild)",
    r"\bmedia\s+production\b", r"\bvideo\s+(film|production)\b",
    r"grant\s+(making|management)\s+services", r"\bfund\s+manage",
]

_R = [re.compile(p, re.IGNORECASE) for p in _RESEARCH]
_I = [re.compile(p, re.IGNORECASE) for p in _IMPLEMENTATION]

_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1
# A single body mention is too weak to route on; a title hit alone is enough.
_THRESHOLD = 3
# How far ahead one side must be before it wins. Titles like "evaluation of the
# supply chain implementation" hit both; a near-tie should stay Unclear rather
# than send the wrong team a bid.
_MARGIN = 2


def _score(patterns, title: str, body: str) -> int:
    total = 0
    for pat in patterns:
        if pat.search(title):
            total += _TITLE_WEIGHT
        if body and pat.search(body):
            total += _BODY_WEIGHT
    return total


def classify_work_type(title: str, body: str = "") -> str:
    """Return Research, Implementation, or "" when it genuinely isn't clear."""
    title = title or ""
    body = body or ""
    r = _score(_R, title, body)
    i = _score(_I, title, body)
    if max(r, i) < _THRESHOLD:
        return UNCLEAR
    if abs(r - i) < _MARGIN:
        return UNCLEAR          # both signals present and balanced — don't guess
    return RESEARCH if r > i else IMPLEMENTATION


def backfill_work_types() -> int:
    """Classify existing rows. Safe to run repeatedly."""
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    updated = 0
    # See services/backfill.py: `.scalars().all()` here loaded the whole table
    # into memory on every worker start.
    from app.services.backfill import iter_opportunities

    with session_scope() as db:
        for opp in iter_opportunities(db):
            body = " ".join(filter(None, [opp.summary, opp.eligibility, opp.vertical]))
            new = classify_work_type(opp.title, body)
            if new != (opp.work_type or ""):
                opp.work_type = new
                updated += 1
    if updated:
        log.info("Work-type backfill: classified %s opportunities", updated)
    return updated
