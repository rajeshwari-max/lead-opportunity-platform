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
MODEL_VERSION = "company-fit-rules-2026.10.15-exclusions-big-bets"
TAXONOMY_VERSION = "lop-hierarchy-2026.10"
FEATURE_VERSION = "company-fit-features-v3"
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
# Shown with the eligibility checks for convenience, but it is the
# opportunity's own classification, not evidence about eligibility. It must
# count towards neither the eligibility score nor the confidence.
ROUTING_CRITERION = "Catalyst brand / vertical"
LIST_FIELDS = (
    "countries_of_operation", "industries", "sectors", "focus_areas",
    "organization_types", "capabilities", "services", "project_types",
    "target_beneficiaries", "geographic_focus", "certifications",
    "partnership_types", "funding_types_of_interest",
    "excluded_organizations",
)

PROFILE_KNOWLEDGE_VERSION = 1

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
    # This compatibility field used to contain generic legal entity types.
    # For Catalyst it now represents the teams against which an opportunity is
    # routed. The API name remains unchanged so existing deployments migrate
    # without a destructive database change.
    "organization_types": [*BRANDS, *DEVSOL_VERTICALS],
    "funding_types_of_interest": [
        "Grant", "RFP", "Tender", "Proposal", "Fellowship", "Award", "Challenge",
    ],
}

# Company facts supplied in "Big Bets of the Catalysts".  These are merged
# once into an existing profile, never used as legal-eligibility evidence, and
# remain editable in the admin panel.  The language stays close to the source
# so the explainable exact-phrase matcher can show what actually matched.
CATALYST_PROFILE_KNOWLEDGE = {
    # The document names India as the headquarters and does not enumerate the
    # other countries in which Catalysts has worked, so only India is asserted.
    "countries_of_operation": ["India"],
    "industries": [
        "Social impact and development", "Public health",
        "Smallholder agriculture", "Sustainable finance",
    ],
    "sectors": [
        "Health", "Livelihoods", "Climate change", "Resilience",
        "Sustainable agriculture", "Comprehensive primary health care",
        "Regenerative agriculture",
    ],
    "focus_areas": [
        "Community resilience", "Agency building", "Access to essential services",
        "Surveillance and predictive analytics", "Climate and health risks",
        "Primary health care", "Small and marginal farmers",
        "Farmer producer organisations", "Regenerative landscapes",
        "Blended finance", "Community systems strengthening",
    ],
    "capabilities": [
        "Resilience measurement", "Community systems strengthening",
        "Surveillance and predictive analytics", "Government technical assistance",
        "Virtual PMU", "Comprehensive primary health care",
        "Farmer enterprise development", "Digital public infrastructure",
        "Regenerative agriculture", "Blended finance facility design",
        "Collaborative orchestration", "Action research and implementation",
        "Social business model design",
    ],
    "services": [
        "Consulting", "Programme implementation", "Action research",
        "Collaborative orchestration", "Technical assistance",
        "Monitoring and evaluation", "Capacity building",
        "Digital platform development", "Financing facility design",
    ],
    "project_types": [
        "Development solutions", "Social business", "Action research",
        "Implementation", "Public health surveillance", "Primary health care",
        "Farmer livelihoods", "Regenerative landscapes", "Digital public infrastructure",
    ],
    "target_beneficiaries": [
        "Small and marginal farmers", "Fisherfolk", "Factory workers",
        "Street vendors", "Sex workers", "Migrants", "Tribal populations",
        "Urban poor", "Rural poor", "Gig workers", "Frontline workers",
        "Forest dependent communities",
    ],
    "partnership_types": [
        "Government", "Civil society organisation", "Social enterprise",
        "Academic institution", "Funder", "Investor",
    ],
    "funding_types_of_interest": [
        "Grant", "RFP", "Tender", "Proposal", "Equity", "Debt",
        "Blended finance", "Returnable grant", "Guarantee",
        "Outcome based financing", "Bond",
    ],
}

# The owner clarified that "whitelisted" in the supplied screenshot means the
# opposite of normal procurement terminology: Catalysts does NOT work with
# these organisations.  Preserve that explicit business meaning here.
DEFAULT_EXCLUDED_ORGANIZATIONS = [
    "Pepsi", "Coke", "ITC", "Adani Foundation", "Vedanta Foundation",
    "Coal based power plants - NTPC and others", "Apple", "Akshaya Patra",
    "HPCL", "Oxfam", "Child in Need Institute - Relief Fund", "Lanco",
    "Syngenta", "India HIV/AIDS Alliance", "FHI360 and FHI India",
]

# Controlled aliases only.  A broad fuzzy match would make a word such as
# "apple" in an agriculture title suppress an unrelated opportunity.  These
# aliases are compared against the scraped funding organisation field.
_EXCLUDED_ORGANIZATION_ALIASES = {
    "pepsi": ("pepsi", "pepsico", "pepsico foundation", "pepsi foundation"),
    "coke": ("coke", "coca cola", "coca cola foundation"),
    "coal based power plants ntpc and others": (
        "ntpc", "national thermal power corporation",
    ),
    "akshaya patra": ("akshaya patra", "the akshaya patra foundation"),
    "adani foundation": ("adani foundation", "adani"),
    "vedanta foundation": ("vedanta foundation", "vedanta"),
    "child in need institute relief fund": (
        "child in need institute", "child in need institute relief fund", "cini",
    ),
    "fhi360 and fhi india": ("fhi360", "fhi 360", "fhi india"),
    "india hiv aids alliance": (
        "india hiv aids alliance", "india hiv/aids alliance",
    ),
    "apple": ("apple", "apple inc", "apple foundation"),
}

# Only facts explicitly supplied in the completed brand forms are recorded
# here. Unknowns remain unknown: a historical win demonstrates experience, not
# a legal registration. CAC opportunities can be routed through the ICF/CMS
# entity, but CAC itself is not represented as a separately registered entity.
BRAND_ENTITY_FACTS = {
    "CMS": {"For-profit"},
    "Swasti": {"Non-profit / NGO"},
    "Vrutti": {"Non-profit / NGO"},
    "Green Foundation": {"Non-profit / NGO"},
    "Upfront": set(),
    "Community Action Collab": set(),
    "Setu": set(),
}

_SOUTH_ASIA_COUNTRIES = {
    "afghanistan", "bangladesh", "bhutan", "india", "maldives",
    "nepal", "pakistan", "sri lanka",
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


def _merged_profile_lists(base: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return the current company knowledge without mutating its source maps."""
    merged = {field: list(values) for field, values in base.items()}
    for field, values in CATALYST_PROFILE_KNOWLEDGE.items():
        merged[field] = _clean_list([*merged.get(field, []), *values])
    merged["excluded_organizations"] = _clean_list([
        *merged.get("excluded_organizations", []),
        *DEFAULT_EXCLUDED_ORGANIZATIONS,
    ])
    return merged


def _apply_profile_knowledge(profile: CompanyProfile) -> bool:
    """Merge newly supplied facts once; return whether the row changed.

    The knowledge version is the guard that protects administrator edits.  A
    later profile save may remove an item and it will stay removed until a new,
    explicitly versioned knowledge package is introduced.
    """
    if (profile.knowledge_version or 0) >= PROFILE_KNOWLEDGE_VERSION:
        return False
    additions = _merged_profile_lists({})
    for field, values in additions.items():
        current = _loads(getattr(profile, field, "[]"), [])
        setattr(profile, field, _dumps(_clean_list([*current, *values])))
    profile.knowledge_version = PROFILE_KNOWLEDGE_VERSION
    profile.version = (profile.version or 0) + 1
    profile.updated_at = datetime.now(timezone.utc)
    return True


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9+.-]{1,}", (value or "").casefold())
        if token not in _STOP and not token.isdigit()
    }


def _csv(value: str | None) -> list[str]:
    return _clean_list((value or "").split(","))


def _default_profile(*, enriched: bool = True) -> CompanyProfile:
    profile_lists = (
        _merged_profile_lists(DEFAULT_PROFILE_LISTS)
        if enriched else {field: list(values) for field, values in DEFAULT_PROFILE_LISTS.items()}
    )
    return CompanyProfile(
        id=PROFILE_ID,
        company_name="The Catalysts",
        **{field: _dumps(values) for field, values in profile_lists.items()},
        knowledge_version=PROFILE_KNOWLEDGE_VERSION if enriched else 0,
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
        if create and _apply_profile_knowledge(profile):
            db.commit()
        return profile
    profile = _default_profile(enriched=create)
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
        values = _loads(getattr(profile, field), [])
        if not profile.updated_by and not values and field in DEFAULT_PROFILE_LISTS:
            values = list(DEFAULT_PROFILE_LISTS[field])
        result[field] = values
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
    profile.knowledge_version = PROFILE_KNOWLEDGE_VERSION
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


def _organization_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()
    return re.sub(r"^the\s+", "", key)


def excluded_organization_match(opportunity: Opportunity, profile: dict) -> str:
    """Return the configured exclusion matched by the funding organisation.

    Deliberately do not scan the whole title/summary.  "Apple" may describe a
    farming programme and "Coke" may appear in environmental prose; neither is
    evidence about the donor.  The scraper's organisation field is the source
    evidence this policy decision requires.
    """
    organization = _organization_key(opportunity.organization or "")
    if not organization:
        return ""
    for configured in profile.get("excluded_organizations", []):
        key = _organization_key(configured)
        aliases = _EXCLUDED_ORGANIZATION_ALIASES.get(key, (configured,))
        for alias in aliases:
            candidate = _organization_key(alias)
            if not candidate:
                continue
            exact_only = candidate == "apple"
            if organization == candidate or (
                not exact_only and organization.startswith(candidate + " ")
            ):
                return configured
    return ""


def _stated_applicant_geography(eligibility: str, locations: Iterable[str]) -> list[str]:
    """Find applicant-location rules, not merely the project's delivery place.

    Opportunity location is useful for geographic *interest*, but it cannot
    prove that an applicant is eligible.  Require wording that ties a stated
    place to the applicant or its registration before comparing company facts.
    """
    stated: list[str] = []
    for location in locations:
        place = re.escape(location)
        patterns = (
            rf"\b(?:registered|based|headquartered|incorporated|domiciled|resident|"
            rf"established|located)\s+(?:in|within)\s+(?:the\s+)?{place}(?!\w)",
            rf"(?<!\w){place}\s*[- ]\s*based\b",
            rf"\bapplicants?\s+(?:from|in|of)\s+"
            rf"(?:the\s+)?{place}(?!\w)",
            rf"\b(?:registered|licensed|incorporated|eligible)\s+(?:\w+\s+){{0,5}}?"
            rf"(?:organisations?|organizations?|ngos?|nonprofits?|charities|"
            rf"institutions?|companies|entities)\s+(?:from|in|of)\s+"
            rf"(?:the\s+)?{place}(?!\w)",
            rf"\b(?:only|eligible|open to)\s+(?:\w+\s+){{0,8}}?"
            rf"(?:applicants?|organisations?|organizations?|ngos?|nonprofits?|"
            rf"charities|institutions?|companies|entities)\s+(?:from|in|of)\s+"
            rf"(?:the\s+)?{place}(?!\w)",
        )
        if any(re.search(pattern, eligibility, re.I) for pattern in patterns):
            stated.append(location)
    return stated


def _company_meets_geography(company_location: str, requirement: str) -> bool:
    company = company_location.strip().casefold()
    required = requirement.strip().casefold()
    if company == required:
        return True
    if required == "south asia" and company in _SOUTH_ASIA_COUNTRIES:
        return True
    return False


def eligibility_matches(opportunity: Opportunity, profile: dict,
                        similar_leads: list[dict] | None = None) -> list[dict]:
    original = (opportunity.eligibility or "").strip()
    matches: list[dict] = []

    # Geographic focus expresses interest, not where the company is registered
    # or operates.  It must not establish eligibility on its own.
    company_geo = _clean_list(profile["countries_of_operation"])
    required_geo = _clean_list([opportunity.country, opportunity.region, opportunity.location])
    stated_geo = _stated_applicant_geography(original, required_geo)
    if not stated_geo:
        matches.append(_criterion("Geography", "No explicit geographic eligibility requirement found",
                                  ", ".join(company_geo), "NOT_APPLICABLE", original))
    elif not company_geo:
        matches.append(_criterion("Geography", ", ".join(stated_geo), "",
                                  "UNKNOWN", original))
    else:
        hit = [g for g in company_geo if any(
            _company_meets_geography(g, requirement) for requirement in stated_geo
        )]
        matches.append(_criterion("Geography", ", ".join(stated_geo), ", ".join(company_geo),
                                  "MATCH" if hit else "MISMATCH",
                                  original))

    selected_brands = _csv(opportunity.brands)
    selected_verticals = [value for value in _csv(opportunity.verticals)
                          if value != "Social Business"]
    routed_to = selected_brands + selected_verticals
    matches.append(_criterion(
        ROUTING_CRITERION,
        "Best-fit Catalyst team for this opportunity",
        ", ".join(routed_to),
        "MATCH" if routed_to else "UNKNOWN",
        "Based on the opportunity classification; a reviewer can correct these labels.",
    ))

    org_patterns = {
        "Non-profit / NGO": r"\b(non[- ]?profit|not[- ]for[- ]profit|ngos?|civil society)\b",
        "For-profit": r"\b(for[- ]?profit|private compan(?:y|ies)|commercial entit(?:y|ies))\b",
        "Academic / research": r"\b(universit|academic|research institution)\b",
        "Government": r"\b(government|public sector|municipal|local authorit)\b",
    }
    required_org = [name for name, pattern in org_patterns.items() if re.search(pattern, original, re.I)]
    entity_facts = {
        fact for brand in selected_brands for fact in BRAND_ENTITY_FACTS.get(brand, set())
    }
    if not required_org:
        matches.append(_criterion("Legal entity fit", "No legal entity requirement found",
                                  ", ".join(sorted(entity_facts)), "NOT_APPLICABLE", original))
    elif not entity_facts:
        matches.append(_criterion("Legal entity fit", ", ".join(required_org), "",
                                  "UNKNOWN", original))
    else:
        required_tokens = _tokens(" ".join(required_org))
        company_tokens = _tokens(" ".join(entity_facts))
        status = "MATCH" if required_tokens & company_tokens else "MISMATCH"
        matches.append(_criterion("Legal entity fit", ", ".join(required_org),
                                  ", ".join(sorted(entity_facts)), status, original))

    years = [int(v) for v in re.findall(r"(?:minimum|at least|min\.?)[^\d]{0,15}(\d{1,2})\s+years?", original, re.I)]
    similar_leads = similar_leads or []
    dated_history = sorted(
        (row for row in similar_leads if row.get("event_date")),
        key=lambda row: row["event_date"],
    )
    oldest_year = int(dated_history[0]["event_date"][:4]) if dated_history else None
    evidenced_years = max(0, datetime.now(timezone.utc).year - oldest_year) if oldest_year else None
    history_summary = (
        f"{evidenced_years}+ years evidenced by similar leads (oldest: {oldest_year})"
        if evidenced_years is not None else "No dated similar lead found"
    )
    if not years:
        matches.append(_criterion("Relevant experience history", "No minimum experience period found",
                                  history_summary, "NOT_APPLICABLE",
                                  "Derived from linked historical leads, not assumed company age."))
    elif evidenced_years is None:
        matches.append(_criterion("Relevant experience history", f"At least {max(years)} years", "",
                                  "UNKNOWN", "No dated similar historical lead was found."))
    else:
        required = max(years)
        matches.append(_criterion("Relevant experience history", f"At least {required} years",
                                  history_summary,
                                  "MATCH" if evidenced_years >= required else "MISMATCH",
                                  "Calculated from the oldest dated similar historical lead."))

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
    # Brand/vertical routing is strategic fit, not proof that an applicant is
    # legally eligible. Show it in the same evidence panel for convenience but
    # never let it manufacture an eligibility score.
    material = [m for m in matches if m["status"] != "NOT_APPLICABLE"
                and m["criterion"] != ROUTING_CRITERION]
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
                                   opportunity.eligibility]))
    groups = (
        ("industries", "Industry"), ("sectors", "Sector"),
        ("focus_areas", "Focus area"),
        ("capabilities", "Capability"), ("services", "Service"),
        ("project_types", "Project type"),
        ("target_beneficiaries", "Target beneficiary"),
    )
    configured = 0
    matched = 0
    substantive_match = False
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
            if field in {"capabilities", "services", "project_types", "target_beneficiaries"}:
                substantive_match = True
    # A single broad match is useful but cannot establish complete company fit.
    strategic = round(100 * matched / max(3, configured), 1) if configured else 0.0
    if not substantive_match:
        strategic = min(strategic, 50.0)

    funding_values = profile["funding_types_of_interest"]
    funding_hits = _contains_any(opportunity.funding_type or "", funding_values) if (
        profile["updated_by"] or funding_values != DEFAULT_PROFILE_LISTS["funding_types_of_interest"]
    ) else []
    if funding_hits:
        reasons.append("Funding preference match: " + ", ".join(funding_hits[:3]))

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
    raw = _loads(row.raw_data, {})
    return " ".join(filter(None, [
        row.title, row.focus_area, row.geography, row.brands, row.verticals,
        raw.get("description", ""), raw.get("brief_description", ""),
    ]))


def _opportunity_text(row: Opportunity) -> str:
    return " ".join(filter(None, [
        row.title, row.summary, row.eligibility, row.brands, row.verticals,
        row.country, row.region, row.location,
    ]))


def _historical_period(row: HistoricalLead) -> str:
    raw = _loads(row.raw_data, {})
    start = raw.get("start_date")
    due = raw.get("due_date")
    duration = raw.get("duration")
    pieces = [value for value in (start, due) if value]
    period = " to ".join(pieces)
    if duration:
        period = f"{period} ({duration})" if period else str(duration)
    return period


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
                "opportunity_url": row.opportunity_url,
                "brands": _csv(row.brands),
                "verticals": _csv(row.verticals),
                "geography": row.geography,
                "lead_period": _historical_period(row),
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
    profile = profile_dict(get_or_create_profile(db, create=persist))
    index = index or HistoricalIndex.from_db(db)
    similar = index.similar(opportunity, 5)
    eligibility = eligibility_matches(opportunity, profile, similar)
    excluded_organization = excluded_organization_match(opportunity, profile)
    if excluded_organization:
        eligibility.insert(0, _criterion(
            "Partner or donor policy",
            "The funding organisation must not be on the Catalysts exclusion list",
            f"Excluded organisation: {excluded_organization}",
            "MISMATCH",
            f"The scraped funding organisation is {opportunity.organization}.",
        ))
    eligibility_score, eligibility_level = _eligibility_score(eligibility)
    if excluded_organization:
        eligibility_score, eligibility_level = 0.0, "LOW"
    computed_eligibility_score = eligibility_score
    computed_eligibility_level = eligibility_level
    latest_feedback = db.scalar(select(HumanFeedback).where(
        HumanFeedback.opportunity_id == opportunity.id
    ).order_by(HumanFeedback.created_at.desc()))
    eligibility_feedback = db.scalar(select(HumanFeedback).where(
        HumanFeedback.opportunity_id == opportunity.id,
        HumanFeedback.eligibility_override != "",
    ).order_by(HumanFeedback.created_at.desc()))
    if eligibility_feedback and not excluded_organization:
        eligibility_level = eligibility_feedback.eligibility_override
        eligibility_score = {
            "HIGH": 100.0, "MEDIUM": 60.0, "LOW": 0.0, "UNKNOWN": 0.0,
        }[eligibility_level]
    # _profile_fit also reports how many profile groups matched / were
    # configured; the score already folds those in, so only the first three
    # values are needed here.
    strategic_fit, geographic_fit, profile_reasons, _matched, _configured = _profile_fit(
        opportunity, profile)
    if excluded_organization:
        strategic_fit = 0.0
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
    if excluded_organization:
        score = 0.0
        priority = "LOW"
        reasons.insert(
            0,
            f"Do not pursue: {opportunity.organization} matches the excluded "
            f"organisation {excluded_organization}",
        )
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
    # Only real eligibility checks. The routing criterion is MATCH for every
    # classified opportunity, so counting it gave each one a free evidence
    # point and let a weak match (e.g. 10%) show "medium confidence".
    resolved_requirements = sum(m["status"] in {"MATCH", "MISMATCH"} for m in eligibility
                                if m["criterion"] != ROUTING_CRITERION)
    substantive_matches = sum(
        reason.startswith(("Capability match:", "Service match:",
                           "Project type match:", "Target beneficiary match:"))
        for reason in profile_reasons
    )
    reviewed_eligibility = bool(eligibility_feedback)
    evidence_points = (resolved_requirements + 2 * len(decided)
                       + min(2, substantive_matches) + 2 * reviewed_eligibility)
    if evidence_points >= 6 and decided and (resolved_requirements or reviewed_eligibility):
        confidence = "HIGH"
    elif evidence_points >= 2 and (decided or resolved_requirements or reviewed_eligibility):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    if excluded_organization:
        # This is a direct policy rule matched against the source organisation,
        # not a probabilistic inference from topic keywords.
        confidence = "HIGH"
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
        "excluded_organization": excluded_organization,
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
