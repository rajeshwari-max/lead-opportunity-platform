"""Classification Engine — keyword-mapping classifier with a pluggable interface.

Inspects title + description + source hint. Swap in an ML/LLM classifier later by
implementing the `Classifier` protocol and registering it in the pipeline.
"""
from __future__ import annotations

import re
from typing import Protocol

from app.core.config import settings
from app.database.models import Category

# Order matters: earlier rules win on ties. Word-boundary regexes prevent
# 'grant' matching inside 'grantee' incorrectly biasing scores too heavily.
_KEYWORD_MAP: list[tuple[Category, list[str]]] = [
    (Category.RFP, [
        r"\brfp\b", r"request\s+for\s+proposals?", r"\brfq\b", r"request\s+for\s+quotation",
        r"\beoi\b", r"expression\s+of\s+interest", r"\btor\b", r"terms\s+of\s+reference",
        r"empanel(ment)?", r"hiring\s+(of\s+)?(an?\s+)?agenc", r"consultanc(y|ies)",
        # UN procurement notations. UNDP alone publishes IC (417 rows), RFQ
        # (324), ITB (91), LTA (27), RFI and RFEI — every one of which is a
        # solicitation the team can respond to, and none of which the generic
        # words above reliably catch when the title is only a code plus a
        # reference number ("UNDP-IC-2026-184: Financial Reporting Support").
        r"\brfi\b", r"request\s+for\s+information",
        r"\brfei\b", r"request\s+for\s+expressions?\s+of\s+interest",
        r"\bic\b", r"individual\s+contract(or)?",
        r"\blta\b", r"long[\s-]term\s+agreement",
        r"\bsssa\b", r"\bcfa\b", r"call\s+for\s+applications?\s+\(consultan",
        r"request\s+for\s+services?", r"solicitation",
    ]),
    (Category.TENDER, [
        r"\btenders?\b", r"\bbid(s|ding)?\b", r"procurement\s+notice", r"\bnit\b",
        r"invitation\s+to\s+bid", r"supply\s+(and|&)\s+installation",
        # ITB/ICB/NCB are formal tender notations rather than proposal requests.
        r"\bitb\b", r"\bicb\b", r"\bncb\b", r"invitation\s+to\s+tender",
        r"\bshopping\b", r"prequalification",
    ]),
    (Category.FELLOWSHIP, [r"fellowship", r"\bfellows?\b", r"scholarship", r"residency\s+program"]),
    (Category.AWARD, [r"\bawards?\b", r"\bprizes?\b", r"recognition\s+program", r"medal"]),
    (Category.CHALLENGE, [r"challenge", r"hackathon", r"competition", r"innovation\s+contest"]),
    (Category.GRANT, [
        r"\bgrants?\b", r"funding\s+opportunit", r"seed\s+fund", r"\bfund(s)?\b",
        r"financial\s+support", r"call\s+for\s+applications?",
    ]),
    (Category.PROPOSAL, [r"call\s+for\s+proposals?", r"\bcfp\b", r"invit\w+\s+proposals?", r"proposals?\s+invited"]),
]

_COMPILED = [
    (cat, [re.compile(p, re.IGNORECASE) for p in patterns])
    for cat, patterns in _KEYWORD_MAP
]


class Classifier(Protocol):
    """Interface for future ML/LLM classifiers."""

    def classify(self, title: str, description: str, hint: Category | None) -> Category: ...


class KeywordClassifier:
    """Weighted keyword scorer: title hits count 3x, description hits 1x.
    A source-provided hint contributes 2 points (never absolute — a 'grant site'
    can still publish RFPs, per requirements)."""

    TITLE_WEIGHT = 3
    BODY_WEIGHT = 1
    HINT_WEIGHT = 2

    def classify(self, title: str, description: str = "", hint: Category | None = None) -> Category:
        scores: dict[Category, int] = {}
        for cat, patterns in _COMPILED:
            score = 0
            for pat in patterns:
                if pat.search(title):
                    score += self.TITLE_WEIGHT
                if description and pat.search(description):
                    score += self.BODY_WEIGHT
            if score:
                scores[cat] = score
        if hint is not None:
            scores[hint] = scores.get(hint, 0) + self.HINT_WEIGHT
        enabled = set(settings.enabled_categories)
        scores = {c: s for c, s in scores.items() if c.value in enabled}
        if not scores:
            return Category.OTHER
        # max score wins; _KEYWORD_MAP order breaks ties (RFP before Grant, etc.)
        best = max(scores.values())
        for cat, _ in _COMPILED:
            if scores.get(cat) == best:
                return cat
        return Category.OTHER
