"""Study type — what kind of study is being asked for?

`work_type` answers "research or implementation". When the answer is research,
the next question a research team asks is which kind: a baseline reads very
differently from an endline, and a pure data-collection contract is a different
proposition again.

Deliberately narrow. This only labels what the text actually says. A title that
doesn't name a study type gets "" rather than a guess — the column is there to
let someone filter to "all the baselines", and a wrong label is worse for that
than an empty one.

Order matters where a title mentions more than one: "baseline and endline
survey" is scored, not first-matched, so the dominant term wins.
"""
from __future__ import annotations

import re

BASELINE = "Baseline"
MIDLINE = "Midline"
ENDLINE = "Endline"
EVALUATION = "Evaluation"
DATA_COLLECTION = "Data Collection"
ASSESSMENT = "Assessment"
FEASIBILITY = "Feasibility"
FORMATIVE = "Formative/Scoping"
MONITORING = "Monitoring (MEL)"
UNSPECIFIED = ""

STUDY_TYPES: list[str] = [
    BASELINE, MIDLINE, ENDLINE, EVALUATION, DATA_COLLECTION,
    ASSESSMENT, FEASIBILITY, FORMATIVE, MONITORING,
]

# Patterns per type. Kept explicit rather than clever: these are read and edited
# by people who are not going to debug a regex.
_PATTERNS: dict[str, list[str]] = {
    BASELINE: [r"\bbaseline\b", r"\bbase[\s-]?line\s+(study|survey|assessment|data)\b"],
    MIDLINE: [r"\bmidline\b", r"\bmid[\s-]?line\b", r"\bmid[\s-]?term\s+(review|evaluation)\b"],
    ENDLINE: [r"\bendline\b", r"\bend[\s-]?line\b", r"\bfinal\s+evaluation\b",
              r"\bsummative\s+evaluation\b", r"\bclose[\s-]?out\s+evaluation\b"],
    EVALUATION: [r"\bevaluation\b", r"\bimpact\s+(evaluation|assessment|study)\b",
                 r"\bperformance\s+evaluation\b", r"\bprogram(me)?\s+evaluation\b",
                 r"\boutcome\s+(evaluation|harvesting)\b", r"\brct\b",
                 r"randomi[sz]ed\s+control"],
    DATA_COLLECTION: [r"\bdata\s+collection\b", r"\bfield\s+(work|data)\b",
                      r"\benumerat(or|ion)\b", r"\bhousehold\s+survey\b",
                      r"\bsurvey\s+(firm|agency|administration)\b", r"\bcensus\b",
                      r"\bsampling\b", r"\bdata\s+entry\b", r"\bkap\s+survey\b"],
    ASSESSMENT: [r"\bneeds\s+assessment\b", r"\brapid\s+assessment\b",
                 r"\bsituation(al)?\s+analysis\b", r"\bgap\s+analysis\b",
                 r"\bvulnerability\s+assessment\b", r"\brisk\s+assessment\b",
                 r"\bmarket\s+(assessment|study|research)\b",
                 r"\bvalue\s+chain\s+(study|analysis|assessment)\b"],
    FEASIBILITY: [r"\bfeasibility\b", r"\bpre[\s-]?feasibility\b",
                  r"\bviability\s+(study|assessment)\b", r"\bdue\s+diligence\b"],
    FORMATIVE: [r"\bformative\s+(research|study|evaluation)\b",
                r"\bscoping\s+(study|exercise|review|mission)\b",
                r"\blandscap(e|ing)\s+(study|analysis|mapping|review)\b",
                r"\bmapping\s+(study|exercise)\b", r"\bdiagnostic\b",
                r"\bliterature\s+review\b", r"\bdesk\s+review\b"],
    MONITORING: [r"\bmonitoring\s*(and|&|,)\s*evaluation\b", r"\bm\s*&\s*e\b",
                 r"\bmel\b", r"\bmeal\b", r"\bthird[\s-]party\s+monitoring\b",
                 r"\bverification\s+agency\b", r"\bprocess\s+monitoring\b",
                 r"\blearning\s+partner\b"],
}

_COMPILED = {
    label: [re.compile(p, re.IGNORECASE) for p in pats]
    for label, pats in _PATTERNS.items()
}

# A title is a far stronger signal than body text, where a passing mention of
# "baseline data" in background prose shouldn't decide the label.
_TITLE_WEIGHT = 3
_BODY_WEIGHT = 1

# Specific beats generic when both are present. "Endline evaluation" is an
# endline; "baseline and endline" needs the tie broken toward the earlier
# phase, since that is what is being commissioned first.
_SPECIFICITY = {
    BASELINE: 6, ENDLINE: 5, MIDLINE: 5, FEASIBILITY: 4, DATA_COLLECTION: 4,
    FORMATIVE: 3, ASSESSMENT: 2, MONITORING: 2, EVALUATION: 1,
}


def classify_study_type(title: str, body: str = "") -> str:
    """Best-supported study type for this text, or '' when none is named."""
    t = (title or "").lower()
    b = (body or "")[:2000].lower()
    if not t and not b:
        return UNSPECIFIED

    # Scored once per field, not once per matching pattern. Several patterns for
    # one label deliberately overlap — "\bendline\b" and "\bend[\s-]?line\b" both
    # match the word "endline" — and summing them let a label with more spellings
    # outscore a label with more actual evidence. "Baseline and Endline Survey"
    # came out as Endline for exactly that reason.
    scores: dict[str, int] = {}
    for label, patterns in _COMPILED.items():
        hits = 0
        if any(p.search(t) for p in patterns):
            hits += _TITLE_WEIGHT
        if b and any(p.search(b) for p in patterns):
            hits += _BODY_WEIGHT
        if hits:
            scores[label] = hits

    if not scores:
        return UNSPECIFIED

    best = max(scores.items(), key=lambda kv: (kv[1], _SPECIFICITY.get(kv[0], 0)))
    return best[0]


def backfill_study_types() -> int:
    """Label existing rows. Idempotent; only fills rows that have no label."""
    import logging

    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    updated = 0
    # Two changes, both about not reading what we do not need.
    #
    # The loop already skipped rows that have a study_type — but it skipped
    # them AFTER the database had returned them and SQLAlchemy had hydrated
    # them into ORM objects. On a table where most rows are already classified
    # that is almost the whole table read to do nothing. The same condition as
    # a WHERE means the database never sends them.
    #
    # And the walk is chunked, so the identity map cannot grow to hold the
    # whole result. See services/backfill.py for the measurement that prompted
    # this.
    #
    # The `continue` below stays: it is the same rule stated where the work
    # happens, and it keeps this correct if the WHERE is ever changed.
    from sqlalchemy import func, or_

    from app.services.backfill import iter_opportunities

    needs_one = or_(Opportunity.study_type.is_(None),
                    func.trim(Opportunity.study_type) == "")
    with session_scope() as db:
        for opp in iter_opportunities(db, where=needs_one):
            if (opp.study_type or "").strip():
                continue
            label = classify_study_type(opp.title or "", opp.summary or "")
            if label:
                opp.study_type = label
                updated += 1
    if updated:
        log.info("Study-type backfill: labelled %s rows", updated)
    return updated
