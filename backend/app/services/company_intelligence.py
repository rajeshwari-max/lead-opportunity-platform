"""Explainable company fit, eligibility and historical-behaviour scoring.

This module intentionally does not classify opportunities.  The hierarchy
classifier answers what an opportunity is about.  This service consumes those
labels together with the shared company profile and verified historical
outcomes to answer whether the opportunity is worth the company's attention.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    CompanyProfile, ExperienceEvent, HistoricalLead, HumanFeedback,
    Opportunity, OpportunityIntelligence,
)

PROFILE_ID = 1
MODEL_VERSION = "company-fit-rules-2026.10.08"
TAXONOMY_VERSION = "lop-hierarchy-2026.10"
FEATURE_VERSION = "company-fit-features-v2"
THRESHOLD_VERSION = "company-fit-thresholds-v1"

DEFAULT_WEIGHTS = {
    "eligibility_fit": 30.0,
    "company_strategic_fit": 25.0,
    "historical_similarity": 20.0,
    "past_success_pattern": 15.0,
    "geographic_fit": 5.0,
    "opportunity_quality": 5.0,
}
DEFAULT_THRESHOLDS = {"high": 70.0, "medium": 45.0}
LIST_FIELDS = (
    "countries_of_operation", "industries", "sectors", "focus_areas",
    "organization_types", "capabilities", "services", "project_types",
    "target_beneficiaries", "geographic_focus", "certifications",
    "partnership_types", "funding_types_of_interest",
)

BRANDS = (
    "CMS", "Swasti", "Vrutti", "Upfront", "Green Foundation",
    "Community Action Collab", "Setu",
)
ARCHETYPES = ("Devsol", "Social Business")
DEVSOL_VERTICALS = (
    "Livelihood", "Health", "E4C(Evidence for Change)",
    "Climate/Sustainability(ESG)", "Worker Wellbeing", "Innovative Finance",
)

# Editable starting point drawn from the taxonomy and geographic preference the
# product owner supplied. These are not model truth: an administrator can
# replace every value from the dashboard, and a profile that has been edited is
# never re-seeded on restart.
DEFAULT_PROFILE_LISTS = {
    "geographic_focus": ["India", "South Asia"],
    "funding_types_of_interest": [
        "Grant", "RFP", "Tender", "Proposal", "Fellowship", "Award", "Challenge",
    ],
}

_STOP = {
    "the", "and", "for", "from", "with", "that", "this", "into", "are",
    "was", "will", "its", "their", "our", "your", "about", "apply", "call",
    "request", "proposal", "opportunity", "grant", "rfp", "tender", "fund",
    "programme", "program", "project", "support", "deadline", "open", "2023", "2024",
    "2025", "2026", "2027",
}


def _loads(value: str | None, fallback):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clean_list(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = text.casefold()
        if text and key not in seen:
            out.append(text)
            seen.add(key)
    return out


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9+.-]{1,}", (value or "").casefold())
        if token not in _STOP and not token.isdigit()
    }


def _csv(value: str | None) -> list[str]:
    return _clean_list((value or "").split(","))


def _default_profile() -> CompanyProfile:
    return CompanyProfile(
        id=PROFILE_ID,
        company_name="The Catalysts",
        **{field: _dumps(values) for field, values in DEFAULT_PROFILE_LISTS.items()},
        recommendation_weights=_dumps(DEFAULT_WEIGHTS),
        recommendation_thresholds=_dumps(DEFAULT_THRESHOLDS),
        version=1,
        updated_by="",
        updated_at=datetime.now(timezone.utc),
    )


def get_or_create_profile(db: Session, *, create: bool = True) -> CompanyProfile:
    profile = db.get(CompanyProfile, PROFILE_ID)
    if profile:
        untouched_blank = (
            profile.version == 1 and not profile.updated_by and not profile.company_name
            and all(not _loads(getattr(profile, field), []) for field in LIST_FIELDS)
        )
        if untouched_blank:
            if not create:
                return CompanyProfile(
                    id=profile.id, company_name="The Catalysts",
                    company_size=profile.company_size,
                    years_of_operation=profile.years_of_operation,
                    recommendation_weights=profile.recommendation_weights,
                    recommendation_thresholds=profile.recommendation_thresholds,
                    version=profile.version, updated_by=profile.updated_by,
                    updated_at=profile.updated_at,
                    **{field: _dumps(DEFAULT_PROFILE_LISTS[field])
                       if field in DEFAULT_PROFILE_LISTS else getattr(profile, field)
                       for field in LIST_FIELDS},
                )
            profile.company_name = "The Catalysts"
            for field, values in DEFAULT_PROFILE_LISTS.items():
                setattr(profile, field, _dumps(values))
            db.commit()
        return profile
    profile = _default_profile()
    if not create:
        return profile
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def profile_dict(profile: CompanyProfile) -> dict:
    result = {
        "id": profile.id,
        "company_name": profile.company_name,
        "company_size": profile.company_size,
        "years_of_operation": profile.years_of_operation,
        "version": profile.version,
        "updated_by": profile.updated_by,
        "updated_at": profile.updated_at,
    }
    for field in LIST_FIELDS:
        result[field] = _loads(getattr(profile, field), [])
    result["recommendation_weights"] = {
        **DEFAULT_WEIGHTS, **_loads(profile.recommendation_weights, {})
    }
    result["recommendation_thresholds"] = {
        **DEFAULT_THRESHOLDS, **_loads(profile.recommendation_thresholds, {})
    }
    return result


def update_profile(db: Session, data: dict, actor: str) -> CompanyProfile:
    profile = get_or_create_profile(db)
    for field in LIST_FIELDS:
        setattr(profile, field, _dumps(_clean_list(data.get(field, []))))
    for field in ("company_name", "company_size", "years_of_operation"):
        setattr(profile, field, data.get(field))
    weights = {key: float(data.get("recommendation_weights", {}).get(key, value))
               for key, value in DEFAULT_WEIGHTS.items()}
    if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("Recommendation weights must be non-negative and total more than zero")
    thresholds = {key: float(data.get("recommendation_thresholds", {}).get(key, value))
                  for key, value in DEFAULT_THRESHOLDS.items()}
    if not 0 <= thresholds["medium"] < thresholds["high"] <= 100:
        raise ValueError("Thresholds must satisfy 0 <= medium < high <= 100")
    profile.recommendation_weights = _dumps(weights)
    profile.recommendation_thresholds = _dumps(thresholds)
    profile.version = (profile.version or 0) + 1
    profile.updated_by = actor
    profile.updated_at = datetime.now(timezone.utc)
    db.commit()
    return profile


def _criterion(name: str, requirement: str, company: str, status: str,
               evidence: str = "") -> dict:
    return {
        "criterion": name, "requirement": requirement or "Not stated",
        "company_information": company or "Not configured", "status": status,
        "evidence": evidence or requirement or "No source evidence available",
    }


def _contains_any(text: str, values: Iterable[str]) -> list[str]:
    return [value for value in values if re.search(
        rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I
    )]


def eligibility_matches(opportunity: Opportunity, profile: dict) -> list[dict]:
    original = (opportunity.eligibility or "").strip()
    matches: list[dict] = []

    company_geo = _clean_list(profile["countries_of_operation"] + profile["geographic_focus"])
    required_geo = _clean_list([opportunity.country, opportunity.region, opportunity.location])
    stated_geo = _contains_any(original, required_geo)
    if not stated_geo:
        matches.append(_criterion("Geography", "No explicit geographic eligibility requirement found",
                                  ", ".join(company_geo), "UNKNOWN", original))
    elif not company_geo:
        matches.append(_criterion("Geography", ", ".join(stated_geo), "",
                                  "UNKNOWN", original))
    else:
        required_text = " ".join(stated_geo).casefold()
        hit = [g for g in company_geo if g.casefold() in required_text or required_text in g.casefold()]
        matches.append(_criterion("Geography", ", ".join(stated_geo), ", ".join(company_geo),
                                  "MATCH" if hit else "MISMATCH",
                                  original))

    org_patterns = {
        "Non-profit / NGO": r"\b(non[- ]?profit|not[- ]for[- ]profit|ngos?|civil society)\b",
        "For-profit": r"\b(for[- ]?profit|private compan(?:y|ies)|commercial entit(?:y|ies))\b",
        "Academic / research": r"\b(universit|academic|research institution)\b",
        "Government": r"\b(government|public sector|municipal|local authorit)\b",
    }
    required_org = [name for name, pattern in org_patterns.items() if re.search(pattern, original, re.I)]
    company_org = profile["organization_types"]
    if not required_org:
        matches.append(_criterion("Organization type", "No organization type requirement found",
                                  ", ".join(company_org), "NOT_APPLICABLE", original))
    elif not company_org:
        matches.append(_criterion("Organization type", ", ".join(required_org), "",
                                  "UNKNOWN", original))
    else:
        required_tokens = _tokens(" ".join(required_org))
        company_tokens = _tokens(" ".join(company_org))
        status = "MATCH" if required_tokens & company_tokens else "MISMATCH"
        matches.append(_criterion("Organization type", ", ".join(required_org),
                                  ", ".join(company_org), status, original))

    years = [int(v) for v in re.findall(r"(?:minimum|at least|min\.?)[^\d]{0,15}(\d{1,2})\s+years?", original, re.I)]
    if not years:
        matches.append(_criterion("Years of operation", "No minimum years found",
                                  str(profile["years_of_operation"] or ""), "NOT_APPLICABLE", original))
    elif profile["years_of_operation"] is None:
        matches.append(_criterion("Years of operation", f"At least {max(years)} years", "",
                                  "UNKNOWN", original))
    else:
        required = max(years)
        matches.append(_criterion("Years of operation", f"At least {required} years",
                                  f"{profile['years_of_operation']} years",
                                  "MATCH" if profile["years_of_operation"] >= required else "MISMATCH",
                                  original))

    cert_required = bool(re.search(r"\b(certif|accredit|registration|registered under|licen[cs])", original, re.I))
    certifications = profile["certifications"]
    if not cert_required:
        matches.append(_criterion("Certification or registration", "No explicit requirement found",
                                  ", ".join(certifications), "NOT_APPLICABLE", original))
    elif not certifications:
        matches.append(_criterion("Certification or registration", "A certification or registration is required",
                                  "", "UNKNOWN", original))
    else:
        named_hits = _contains_any(original, certifications)
        matches.append(_criterion("Certification or registration", "Source requirement",
                                  ", ".join(certifications), "MATCH" if named_hits else "UNKNOWN", original))
    return matches


def _eligibility_score(matches: list[dict]) -> tuple[float, str]:
    material = [m for m in matches if m["status"] != "NOT_APPLICABLE"]
    if not material:
        return 0.0, "UNKNOWN"
    counts = Counter(m["status"] for m in material)
    score = 100.0 * counts["MATCH"] / len(material)
    if counts["MISMATCH"]:
        level = "LOW"
    elif counts["UNKNOWN"]:
        level = "MEDIUM" if counts["MATCH"] else "UNKNOWN"
    else:
        level = "HIGH"
    return round(score, 1), level


def _profile_fit(opportunity: Opportunity, profile: dict) -> tuple[float, float, list[str], int, int]:
    text = " ".join(filter(None, [opportunity.title, opportunity.summary,
                                   opportunity.eligibility, opportunity.funding_type]))
    groups = (
        ("industries", "Industry"), ("sectors", "Sector"),
        ("focus_areas", "Focus area"),
        ("capabilities", "Capability"), ("services", "Service"),
        ("project_types", "Project type"),
        ("target_beneficiaries", "Target beneficiary"),
        ("funding_types_of_interest", "Funding type"),
    )
    configured = 0
    matched = 0
    reasons: list[str] = []
    for field, label in groups:
        values = profile[field]
        # Older installs were seeded with every vertical and funding type.
        # Those taxonomy defaults are not evidence of company experience.
        if not profile["updated_by"] and (
            (field == "sectors" and values == [*DEVSOL_VERTICALS, "Social Business"])
            or (field == "funding_types_of_interest"
                and values == DEFAULT_PROFILE_LISTS["funding_types_of_interest"])
        ):
            continue
        if not values:
            continue
        configured += 1
        hits = _contains_any(text, values)
        if hits:
            matched += 1
            reasons.append(f"{label} match: {', '.join(hits[:3])}")
    # A single broad match is useful but cannot establish complete company fit.
    strategic = round(100 * matched / max(3, configured), 1) if configured else 0.0

    geography = 0.0
    geo_values = _clean_list(profile["countries_of_operation"] + profile["geographic_focus"])
    opp_geo = " ".join(filter(None, [opportunity.country, opportunity.region,
                                      opportunity.location])).casefold()
    if geo_values and opp_geo:
        geo_hits = _contains_any(opp_geo, geo_values)
        geography = 100.0 if geo_hits else 0.0
        if geo_hits:
            reasons.append("Geography match: " + ", ".join(geo_hits[:3]))
    return strategic, geography, reasons, matched, configured


def _historical_text(row: HistoricalLead) -> str:
    # Imported taxonomy labels are machine generated. Matching on them alone
    # makes unrelated leads appear similar, so titles establish the overlap.
    return row.title or ""


def _opportunity_text(row: Opportunity) -> str:
    return row.title or ""


@dataclass
class HistoricalIndex:
    rows: list[HistoricalLead]
    tokens: list[set[str]]
    inverted: dict[str, set[int]]

    @classmethod
    def from_db(cls, db: Session, limit: int = 10000) -> "HistoricalIndex":
        rows = list(db.scalars(select(HistoricalLead).order_by(HistoricalLead.id.desc()).limit(limit)))
        token_rows: list[set[str]] = []
        inverted: dict[str, set[int]] = defaultdict(set)
        for index, row in enumerate(rows):
            terms = _tokens(_historical_text(row))
            token_rows.append(terms)
            for term in terms:
                inverted[term].add(index)
        return cls(rows, token_rows, inverted)

    def similar(self, opportunity: Opportunity, limit: int = 5) -> list[dict]:
        query = _tokens(_opportunity_text(opportunity))
        candidates: set[int] = set()
        for term in query:
            candidates.update(self.inverted.get(term, ()))
        scored: list[tuple[float, HistoricalLead]] = []
        for index in candidates:
            terms = self.tokens[index]
            union = len(query | terms)
            if not union:
                continue
            overlap = query & terms
            score = len(overlap) / union
            if len(overlap) >= 2 and score >= 0.2:
                scored.append((score, self.rows[index]))
        scored.sort(key=lambda item: (item[0], item[1].event_date or datetime.min.date()), reverse=True)
        return [
            {
                "id": row.id, "title": row.title, "similarity": round(score * 100, 1),
                "status": row.status, "outcome": row.outcome, "won_lost": row.won_lost,
                "reason": row.reason,
                "matched_terms": sorted(query & _tokens(_historical_text(row))),
                "event_date": row.event_date.isoformat() if row.event_date else None,
            }
            for score, row in scored[:limit]
        ]


def _quality(opportunity: Opportunity) -> float:
    checks = (
        bool(opportunity.title), bool(opportunity.summary), bool(opportunity.eligibility),
        bool(opportunity.opportunity_url), bool(opportunity.deadline or opportunity.deadline_state == "rolling"),
        bool(opportunity.funding_amount), bool(opportunity.organization),
        bool(opportunity.country or opportunity.location),
    )
    return round(100 * sum(checks) / len(checks), 1)


def _weighted_score(parts: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(max(0.0, float(weights.get(key, 0))) for key in parts)
    if not total:
        return 0.0
    return round(sum(parts[key] * max(0.0, float(weights.get(key, 0))) for key in parts) / total, 1)


def analyze_opportunity(db: Session, opportunity: Opportunity, *, persist: bool = True,
                        index: HistoricalIndex | None = None,
                        commit: bool = True) -> dict:
    profile = profile_dict(get_or_create_profile(db))
    eligibility = eligibility_matches(opportunity, profile)
    eligibility_score, eligibility_level = _eligibility_score(eligibility)
    computed_eligibility_score = eligibility_score
    computed_eligibility_level = eligibility_level
    latest_feedback = db.scalar(select(HumanFeedback).where(
        HumanFeedback.opportunity_id == opportunity.id
    ).order_by(HumanFeedback.created_at.desc()))
    eligibility_feedback = db.scalar(select(HumanFeedback).where(
        HumanFeedback.opportunity_id == opportunity.id,
        HumanFeedback.eligibility_override != "",
    ).order_by(HumanFeedback.created_at.desc()))
    if eligibility_feedback:
        eligibility_level = eligibility_feedback.eligibility_override
        eligibility_score = {
            "HIGH": 100.0, "MEDIUM": 60.0, "LOW": 0.0, "UNKNOWN": 0.0,
        }[eligibility_level]
    # _profile_fit also reports how many profile groups matched / were
    # configured; the score already folds those in, so only the first three
    # values are needed here.
    strategic_fit, geographic_fit, profile_reasons, _matched, _configured = _profile_fit(
        opportunity, profile)
    index = index or HistoricalIndex.from_db(db)
    similar = index.similar(opportunity, 5)
    historical_similarity = similar[0]["similarity"] if similar else 0.0
    decided = [row for row in similar if row["won_lost"] in {"won", "lost"}]
    success_score = round(100 * sum(row["won_lost"] == "won" for row in decided) / len(decided), 1) if decided else 0.0
    quality = _quality(opportunity)
    parts = {
        "eligibility_fit": eligibility_score,
        "company_strategic_fit": strategic_fit,
        "historical_similarity": historical_similarity,
        "past_success_pattern": success_score,
        "geographic_fit": geographic_fit,
        "opportunity_quality": quality,
    }
    score = _weighted_score(parts, profile["recommendation_weights"])
    thresholds = profile["recommendation_thresholds"]
    priority = "HIGH" if score >= thresholds["high"] else "MEDIUM" if score >= thresholds["medium"] else "LOW"
    risks = [
        f"{m['criterion']}: {m['status'].replace('_', ' ').title()} - {m['requirement']}"
        for m in eligibility if m["status"] in {"MISMATCH", "UNKNOWN"}
    ]
    reasons = list(profile_reasons)
    if decided:
        wins = sum(row["won_lost"] == "won" for row in decided)
        reasons.append(f"{wins} of {len(decided)} closest decided historical leads were won")
    elif similar:
        reasons.append("Similar historical leads exist, but their final won/lost outcome is not verified")
    if not reasons:
        reasons.append("Company profile and verified outcome history do not yet provide a positive match")
    if eligibility_feedback:
        reasons.append(
            f"A reviewer set eligibility to {eligibility_level}"
            + (f": {eligibility_feedback.reason}" if eligibility_feedback.reason else "")
        )
    configured_profile_fields = sum(bool(profile[field]) for field in LIST_FIELDS)
    evidence_points = configured_profile_fields + len(similar) + sum(m["status"] != "UNKNOWN" for m in eligibility)
    confidence = "HIGH" if evidence_points >= 12 else "MEDIUM" if evidence_points >= 6 else "LOW"
    result = {
        "opportunity_id": opportunity.id,
        "classification": {
            "brands": _csv(opportunity.brands),
            "archetypes": _csv(opportunity.archetypes),
            "verticals": [
                value for value in _csv(opportunity.verticals)
                if value != "Social Business"
            ],
            "scores": {
                "brands": _loads(opportunity.brand_scores, {}),
                "archetypes": _loads(opportunity.classification_evidence, {}).get(
                    "archetype_scores", {}
                ),
                "verticals": _loads(opportunity.vertical_scores, {}),
            },
            "evidence": _loads(opportunity.classification_evidence, {}),
            "source": opportunity.classification_source or opportunity.verticals_source or "",
            "status": opportunity.classification_status or "",
            "version": opportunity.classification_version or "",
        },
        "components": parts,
        "computed_eligibility_score": computed_eligibility_score,
        "computed_eligibility_level": computed_eligibility_level,
        "eligibility_score": eligibility_score,
        "eligibility_level": eligibility_level,
        "company_fit_score": strategic_fit,
        "historical_similarity": historical_similarity,
        "success_pattern_score": success_score,
        "opportunity_quality_score": quality,
        "recommendation_score": score,
        "priority": priority,
        "confidence": confidence,
        "eligibility_matches": eligibility,
        "similar_leads": similar,
        "reasons": reasons,
        "risks": risks,
        "human_decision": latest_feedback.decision if latest_feedback else "",
        "human_reason": latest_feedback.reason if latest_feedback else "",
        "eligibility_override": eligibility_feedback.eligibility_override if eligibility_feedback else "",
        "versions": {
            "model": MODEL_VERSION, "taxonomy": TAXONOMY_VERSION,
            "features": FEATURE_VERSION, "thresholds": THRESHOLD_VERSION,
            "company_profile": profile["version"],
        },
    }
    if persist:
        snapshot = db.scalar(select(OpportunityIntelligence).where(
            OpportunityIntelligence.opportunity_id == opportunity.id
        )) or OpportunityIntelligence(opportunity_id=opportunity.id)
        changed = snapshot.id is None or abs((snapshot.recommendation_score or 0) - score) >= 0.1
        for field in (
            "eligibility_score", "eligibility_level", "company_fit_score",
            "historical_similarity", "success_pattern_score", "opportunity_quality_score",
            "recommendation_score", "priority", "confidence",
        ):
            setattr(snapshot, field, result[field])
        snapshot.eligibility_matches = _dumps(eligibility)
        snapshot.similar_leads = _dumps(similar)
        snapshot.reasons = _dumps(reasons)
        snapshot.risks = _dumps(risks)
        snapshot.model_version = MODEL_VERSION
        snapshot.taxonomy_version = TAXONOMY_VERSION
        snapshot.feature_version = FEATURE_VERSION
        snapshot.threshold_version = THRESHOLD_VERSION + f"/profile-{profile['version']}"
        snapshot.computed_at = datetime.now(timezone.utc)
        db.add(snapshot)
        if changed:
            db.add(ExperienceEvent(
                opportunity_id=opportunity.id, event_type="prediction",
                actor="system", model_version=MODEL_VERSION,
                prediction_confidence={"LOW": 0.33, "MEDIUM": 0.66, "HIGH": 1.0}[confidence],
                payload=_dumps({"score": score, "priority": priority, "components": parts}),
            ))
        if commit:
            db.commit()
    return result


def snapshot_dict(row: OpportunityIntelligence) -> dict:
    return {
        "opportunity_id": row.opportunity_id,
        "eligibility_score": row.eligibility_score,
        "eligibility_level": row.eligibility_level,
        "company_fit_score": row.company_fit_score,
        "historical_similarity": row.historical_similarity,
        "success_pattern_score": row.success_pattern_score,
        "opportunity_quality_score": row.opportunity_quality_score,
        "recommendation_score": row.recommendation_score,
        "priority": row.priority,
        "confidence": row.confidence,
        "eligibility_matches": _loads(row.eligibility_matches, []),
        "similar_leads": _loads(row.similar_leads, []),
        "reasons": _loads(row.reasons, []),
        "risks": _loads(row.risks, []),
        "versions": {
            "model": row.model_version, "taxonomy": row.taxonomy_version,
            "features": row.feature_version, "thresholds": row.threshold_version,
        },
        "computed_at": row.computed_at,
    }


def learning_summary(db: Session) -> dict:
    rows = list(db.scalars(select(HistoricalLead)))
    feedback = list(db.scalars(select(HumanFeedback)))
    experience = list(db.scalars(select(ExperienceEvent)))
    decisions = [row for row in rows if row.won_lost in {"won", "lost"}]
    won = [row for row in decisions if row.won_lost == "won"]
    by_status = Counter(row.status or "Unknown" for row in rows)
    by_type = Counter(row.opportunity_type or "Unknown" for row in rows)
    by_geo = Counter(row.geography or "Unknown" for row in won)
    by_vertical = Counter(v for row in won for v in _csv(row.verticals))
    reasons_won = Counter(row.reason for row in won if row.reason)
    reasons_lost = Counter(row.reason for row in decisions if row.won_lost == "lost" and row.reason)
    reasons_rejected = Counter(
        row.reason for row in rows if row.action_taken == "Rejected" and row.reason
    )
    actions = Counter(row.user_action for row in experience if row.user_action)
    live_outcomes = Counter(row.outcome for row in experience if row.outcome)
    return {
        "historical": {
            "total": len(rows), "decided": len(decisions), "won": len(won),
            "lost": len(decisions) - len(won),
            "positive_unverified": sum(
                row.action_taken == "Accepted" and not row.won_lost for row in rows
            ),
            "applied": sum(row.action_taken == "Applied" for row in rows),
            "shortlisted": sum(row.action_taken == "Shortlisted" for row in rows),
            "rejected": sum(row.action_taken == "Rejected" for row in rows),
            "win_rate": round(100 * len(won) / len(decisions), 1) if decisions else 0,
        },
        "feedback": {
            "total": len(feedback),
            "accepted": sum(row.decision == "accept" for row in feedback),
            "rejected": sum(row.decision == "reject" for row in feedback),
            "corrected": sum(row.decision == "correct" for row in feedback),
        },
        "live_actions": {
            "saved": actions["saved"],
            "preparing": actions["preparing"],
            "applied": actions["applied"],
            "shortlisted": actions["shortlisted"],
            "accepted": actions["accepted"],
            "unsuccessful": actions["unsuccessful"],
            "withdrawn": actions["withdrawn"],
            "won": live_outcomes["won"],
            "lost": live_outcomes["lost"],
        },
        "top_statuses": by_status.most_common(10),
        "top_opportunity_types": by_type.most_common(10),
        "successful_geographies": by_geo.most_common(10),
        "successful_verticals": by_vertical.most_common(10),
        "top_win_reasons": reasons_won.most_common(10),
        "top_loss_reasons": reasons_lost.most_common(10),
        "top_rejection_reasons": reasons_rejected.most_common(10),
    }
