"""Apply versioned human hierarchy decisions before automatic backfills."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database.db import session_scope
from app.database.models import Category, Opportunity
from app.services.brands import brands_to_str
from app.services.verticals import HUMAN, VERTICAL_SOCIAL_BUSINESS, verticals_to_str

VERSION = "human-review-2026.10.06"
DEFAULT_LABELS = Path(__file__).resolve().parents[2] / "ml" / "review_labels.jsonl"
ALIASES = {
    "E4C (Evidence for Change)": "E4C(Evidence for Change)",
    "Climate/Sustainability (ESG)": "Climate/Sustainability(ESG)",
}


def load_reviewed(path: Path = DEFAULT_LABELS) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [row for row in rows if str(row.get("human_status", "")).casefold() == "reviewed"]


def preview_hierarchy_reviews(path: Path = DEFAULT_LABELS) -> dict:
    labels = load_reviewed(path)
    ids = [str(row["record_id"]) for row in labels]
    with session_scope() as db:
        matched = set(db.execute(
            select(Opportunity.unique_id).where(Opportunity.unique_id.in_(ids))
        ).scalars())
    return {
        "reviewed": len(labels),
        "matched": len(matched),
        "missing": len(set(ids) - matched),
        "missing_ids": sorted(set(ids) - matched)[:10],
    }


def apply_hierarchy_reviews(path: Path = DEFAULT_LABELS,
                            reviewer: str = "completed-review-workbook") -> int:
    """Apply changed reviewed decisions only; unresolved rows never train or write."""
    labels = load_reviewed(path)
    if not labels:
        return 0
    by_id = {str(row["record_id"]): row for row in labels}
    now = datetime.now(timezone.utc)
    updated = 0
    with session_scope() as db:
        opportunities = db.execute(
            select(Opportunity).where(Opportunity.unique_id.in_(list(by_id)))
        ).scalars().all()
        for opportunity in opportunities:
            review = by_id[opportunity.unique_id]
            brands = [] if "Do not assign" in review.get("brands", []) else review.get("brands", [])
            archetypes = [] if "Not applicable" in review.get("archetypes", []) else review.get("archetypes", [])
            verticals = [ALIASES.get(value, value) for value in review.get("devsol_verticals", [])]
            if "Social Business" in archetypes:
                verticals.append(VERTICAL_SOCIAL_BUSINESS)
            category = Category(review["category"])
            brand_value = brands_to_str([brand for brand in brands if brand != "CMS"])
            archetype_value = ", ".join(archetypes)
            vertical_value = verticals_to_str(verticals)
            intended_status = "classified" if (brands or verticals) else "unclassified"
            current = (
                opportunity.category, opportunity.brands, opportunity.archetypes,
                opportunity.verticals, opportunity.verticals_source,
                opportunity.classification_status, opportunity.classification_source,
                opportunity.classification_version,
            )
            intended = (
                category, brand_value, archetype_value, vertical_value, HUMAN,
                intended_status, HUMAN, VERSION,
            )
            if current == intended:
                continue
            opportunity.category = category
            opportunity.brands = brand_value
            opportunity.archetypes = archetype_value
            opportunity.verticals = vertical_value
            opportunity.verticals_source = HUMAN
            opportunity.verticals_labeled_by = reviewer
            opportunity.verticals_labeled_at = now
            opportunity.classification_status = intended_status
            opportunity.classification_source = HUMAN
            opportunity.classification_version = VERSION
            opportunity.brand_classification_version = VERSION
            opportunity.classified_at = now
            updated += 1
        db.commit()
    return updated
