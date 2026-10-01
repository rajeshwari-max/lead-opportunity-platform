"""Auditable multi-label brand classification.

The source workbook contains search phrases, not a promise that every isolated
word is distinctive.  This classifier therefore preserves every supplied term
but applies two safeguards before a brand is asserted:

* matches use word/phrase boundaries (``farm`` never matches ``pharmacy``);
* broad terms such as ``India``, ``research`` and ``worker`` cannot classify a
  row by themselves, while a specific phrase in the title can.

Overlapping variants count once.  For example, "regenerative agriculture
India" is useful evidence, but it must not score twice merely because the
shorter phrase "regenerative agriculture" occurs inside it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from app.services.brand_keywords import BRAND_KEYWORDS, BRANDS

MODEL_VERSION = "brand-rules-2026.10.01"
ASSIGNMENT_THRESHOLD = 3.0

# Valid search aids that are unsafe as standalone brand decisions.  They remain
# in the inventory and can corroborate another hit; they simply cannot turn a
# generic notice into a brand match on their own.
_WEAK_TERMS = {
    "india", "research", "evaluation", "design", "gender", "livelihood",
    "livelihoods", "worker", "community", "urban", "rural", "formal",
    "informal", "farm", "non farm", "commons", "restoration", "esg",
    "sustainability", "supply chain", "textile", "cotton", "spice",
    "women", "children", "elderly", "training", "agriculture", "outreach",
    "social welfare", "service delivery", "policy research", "field research",
    "financial inclusion", "rural development", "skill development",
    "philanthropy", "development finance", "impact investment", "ngos",
    "development sector", "development partners", "social justice",
    "labour welfare", "health and family welfare",
    "health", "resilience", "collaboration", "social protection",
}

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b", re.I)
_COUNTRY_NOISE = re.compile(r"\bindia\b", re.I)
_PARENTHETICAL_ABBREVIATION = re.compile(r"\(([A-Za-z][A-Za-z0-9-]{1,10})\)")
_SEARCH_WRAPPERS = re.compile(
    r"\b(?:call for proposals?|request for applications?|expression of interest|"
    r"open call|funding opportunity|apply now)\b",
    re.I,
)


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("&", " and ").replace("-", " ")
    return " ".join(value.casefold().split())


def _core_variant(term: str) -> str:
    """Remove query furniture while retaining the workbook's original term."""
    value = _SEARCH_WRAPPERS.sub(" ", term)
    value = _YEAR.sub(" ", value)
    value = _COUNTRY_NOISE.sub(" ", value)
    return " ".join(value.split()).strip(" -–—,.;:")


def _phrase_pattern(term: str) -> re.Pattern[str]:
    # Flexible separators make "worker-wellbeing" match "Worker Well Being"
    # and "workers' rights" match "workers rights", without falling back to
    # unsafe substring matching.
    separator = r"[\s\-&/'’]+"
    pieces = [re.escape(part) for part in re.split(separator, term) if part]
    expression = separator.join(pieces)
    return re.compile(rf"(?<!\w){expression}(?!\w)", re.I)


def _search_variants(term: str) -> tuple[str, ...]:
    """Return safe matching aliases while retaining the supplied term."""
    variants = [term, _core_variant(term)]
    for match in _PARENTHETICAL_ABBREVIATION.finditer(term):
        long_form = (term[:match.start()] + term[match.end():]).strip()
        variants.extend((long_form, match.group(1)))
    return tuple(variants)


@dataclass(frozen=True)
class _Term:
    keyword: str
    pattern: re.Pattern[str]
    weak: bool


def _compile() -> dict[str, tuple[_Term, ...]]:
    compiled: dict[str, tuple[_Term, ...]] = {}
    for brand, source_terms in BRAND_KEYWORDS.items():
        terms: list[_Term] = []
        seen: set[str] = set()
        for source_term in source_terms:
            for variant in _search_variants(source_term):
                key = _normalise(variant)
                if not key or key in seen:
                    continue
                seen.add(key)
                terms.append(_Term(
                    keyword=source_term,
                    pattern=_phrase_pattern(variant),
                    weak=key in _WEAK_TERMS,
                ))
        compiled[brand] = tuple(terms)
    return compiled


_COMPILED = _compile()


def _non_overlapping_hits(
    text: str, terms: tuple[_Term, ...], *, title: bool,
) -> tuple[float, list[str], bool]:
    candidates: list[tuple[float, int, int, str, bool]] = []
    for term in terms:
        weight = (1.0 if title else 0.35) if term.weak else (3.0 if title else 1.5)
        # Repetition is not corroboration. A summary that says "community" ten
        # times still contains one broad signal, not ten independent reasons.
        match = term.pattern.search(text or "")
        if match:
            candidates.append(
                (weight, match.start(), match.end(), term.keyword, not term.weak))

    # Prefer stronger and longer matches. Accepted spans cannot overlap, so one
    # phrase described by both a long query and its cleaned core scores once.
    candidates.sort(key=lambda item: (-item[0], -(item[2] - item[1]), item[1]))
    accepted: list[tuple[int, int]] = []
    score = 0.0
    evidence: list[str] = []
    has_specific = False
    for weight, start, end, keyword, specific in candidates:
        if any(start < used_end and end > used_start for used_start, used_end in accepted):
            continue
        accepted.append((start, end))
        score += weight
        has_specific = has_specific or specific
        if keyword not in evidence:
            evidence.append(keyword)
    return score, evidence, has_specific


@dataclass
class BrandClassification:
    labels: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    version: str = MODEL_VERSION

    def scores_json(self) -> str:
        return json.dumps({k: round(v, 2) for k, v in self.scores.items() if v}, sort_keys=True)

    def evidence_json(self) -> str:
        return json.dumps({k: v[:8] for k, v in self.evidence.items() if v}, sort_keys=True)


def classify_brands(title: str, body: str = "") -> BrandClassification:
    result = BrandClassification()
    for brand in BRANDS:
        title_score, title_evidence, title_specific = _non_overlapping_hits(
            title or "", _COMPILED[brand], title=True)
        body_score, body_evidence, body_specific = _non_overlapping_hits(
            body or "", _COMPILED[brand], title=False)
        score = title_score + body_score
        if score:
            result.scores[brand] = score
            result.evidence[brand] = list(dict.fromkeys(title_evidence + body_evidence))
        if score >= ASSIGNMENT_THRESHOLD and (title_specific or body_specific):
            result.labels.append(brand)
    return result


def brands_to_str(brands: list[str]) -> str:
    allowed = set(BRANDS)
    return ", ".join(brand for brand in BRANDS if brand in set(brands) and brand in allowed)


def backfill_brands(active_only: bool = False) -> int:
    """Classify only rows not produced by the current brand ruleset.

    Deployments can finish the user-visible live set synchronously; ordinary
    startup maintenance then completes the historical archive in the
    background without delaying API availability.
    """
    from sqlalchemy import and_, or_

    from app.database.models import Opportunity
    from app.services.actionable import strict_actionable_clause
    from app.services.backfill import run_backfill

    def apply(row) -> bool:
        body = " ".join(filter(None, [row.summary, row.vertical, row.eligibility]))
        result = classify_brands(row.title or "", body)
        row.brands = brands_to_str(result.labels)
        row.brand_scores = result.scores_json()
        row.brand_evidence = result.evidence_json()
        row.brand_classification_version = result.version
        return True

    stale = or_(
        Opportunity.brand_classification_version.is_(None),
        Opportunity.brand_classification_version != MODEL_VERSION,
    )
    where = and_(stale, strict_actionable_clause()) if active_only else stale
    return run_backfill("brand classification", apply, where=where)
