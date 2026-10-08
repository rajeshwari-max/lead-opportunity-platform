"""Production hierarchy classification with a safe rule-based fallback."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.database.models import Category
from app.services.brands import classify_brands
from app.services.classification import KeywordClassifier
from app.services.classification_model import classify as classify_vertical_rules
from app.services.verticals import VERTICAL_SOCIAL_BUSINESS

log = logging.getLogger("scraper")

MODEL_PATH = Path(__file__).resolve().parents[1] / "ml_models" / "hierarchy_model.joblib"
_lock = threading.Lock()
_artifact = None
_load_error = ""


@dataclass
class HierarchyPrediction:
    category: Category
    brands: list[str] = field(default_factory=list)
    archetypes: list[str] = field(default_factory=list)
    verticals: list[str] = field(default_factory=list)
    scores: dict[str, dict[str, float]] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    status: str = "unclassified"
    source: str = "rule"
    version: str = ""

    def scores_json(self, group: str) -> str:
        return json.dumps({key: round(value, 4) for key, value in self.scores.get(group, {}).items()}, sort_keys=True)

    def evidence_json(self) -> str:
        return json.dumps({
            "model_source": self.source,
            "model_version": self.version,
            "top_terms": self.evidence,
            "archetype_scores": self.scores.get("archetypes", {}),
        }, sort_keys=True)


def _configured_path() -> Path:
    configured = str(getattr(settings, "ml_model_path", "") or "").strip()
    return Path(configured).expanduser() if configured else MODEL_PATH


def _load():
    global _artifact, _load_error
    if _artifact is not None:
        return _artifact
    if not getattr(settings, "ml_classifier_enabled", True):
        _load_error = "disabled by LOP_ML_CLASSIFIER_ENABLED"
        return None
    with _lock:
        if _artifact is not None:
            return _artifact
        try:
            import joblib

            _artifact = joblib.load(_configured_path())
            _load_error = ""
            log.info("ML hierarchy classifier loaded: %s", _artifact["metadata"]["version"])
        except Exception as exc:  # noqa: BLE001 - fallback must survive any artifact problem
            _load_error = f"{type(exc).__name__}: {exc}"
            log.warning("ML hierarchy classifier unavailable; using rules: %s", _load_error)
            return None
    return _artifact


def reset_model_cache() -> None:
    """Test/reload helper; the next prediction reloads the configured file."""
    global _artifact, _load_error
    with _lock:
        _artifact = None
        _load_error = ""


def model_status() -> dict:
    artifact = _load()
    metadata = dict(artifact.get("metadata", {})) if artifact else {}
    return {
        "mode": "ml" if artifact else "rule_fallback",
        "enabled": bool(getattr(settings, "ml_classifier_enabled", True)),
        "loaded": artifact is not None,
        "version": metadata.get("version", "rules"),
        "trained_at": metadata.get("trained_at"),
        "training_rows": metadata.get("training_rows", 0),
        "gold_rows": metadata.get("gold_rows", 0),
        "unresolved_rows_excluded": metadata.get("unresolved_rows_excluded", 0),
        "path": str(_configured_path()),
        "error": _load_error,
    }


def _input_text(title: str, summary: str, eligibility: str, organization: str,
                location: str, country: str, region: str, source: str,
                category_hint: Category | None) -> str:
    values = (
        ("Title", title), ("Summary", summary), ("Eligibility", eligibility),
        ("Organization", organization), ("Location", location),
        ("Country", country), ("Region", region), ("Source", source),
        ("Source category hint", category_hint.value if category_hint else ""),
    )
    return "\n".join(f"{label}: {value}" for label, value in values if str(value or "").strip())


def _binary_probability(head, X) -> float:
    if isinstance(head, dict):
        return float(head.get("constant_probability", 0.0))
    return float(head.predict_proba(X)[0, 1])


def _top_terms(artifact, X, classifier, class_index: int | None = None, limit: int = 5) -> list[str]:
    if isinstance(classifier, dict) or not hasattr(classifier, "coef_"):
        return []
    try:
        coefficient = classifier.coef_[class_index or 0]
        contribution = X.multiply(coefficient).toarray()[0]
        names = artifact["vectorizer"].get_feature_names_out()
        indices = contribution.argsort()[::-1]
        return [str(names[index]).split("__", 1)[-1] for index in indices if contribution[index] > 0][:limit]
    except Exception:  # noqa: BLE001 - evidence is optional, prediction is not
        return []


def _fallback(title: str, body: str, category_hint: Category | None) -> HierarchyPrediction:
    category = KeywordClassifier().classify(title, body, category_hint)
    vertical_result = classify_vertical_rules(title, body)
    brand_result = classify_brands(title, body)
    social = VERTICAL_SOCIAL_BUSINESS in vertical_result.labels
    devsol_verticals = [label for label in vertical_result.labels if label != VERTICAL_SOCIAL_BUSINESS]
    brands = (["CMS"] if vertical_result.labels else []) + brand_result.labels
    archetypes = (["Devsol"] if devsol_verticals else []) + (["Social Business"] if social else [])
    scores = {
        "verticals": {key: float(value) for key, value in vertical_result.scores.items() if key != VERTICAL_SOCIAL_BUSINESS},
        "brands": {key: float(value) for key, value in brand_result.scores.items()},
        "archetypes": {"Social Business": float(vertical_result.scores.get(VERTICAL_SOCIAL_BUSINESS, 0.0))},
    }
    return HierarchyPrediction(
        category=category,
        brands=brands,
        archetypes=archetypes,
        verticals=devsol_verticals,
        scores=scores,
        evidence={**vertical_result.evidence, **brand_result.evidence},
        status=vertical_result.status if not brand_result.labels else "classified",
        source="rule",
        version=f"{vertical_result.version}+{brand_result.version}",
    )


def classify_hierarchy(title: str, summary: str = "", eligibility: str = "",
                       organization: str = "", location: str = "", country: str = "",
                       region: str = "", source: str = "",
                       category_hint: Category | None = None) -> HierarchyPrediction:
    body = " ".join(filter(None, [summary, eligibility, location]))
    artifact = _load()
    if artifact is None:
        return _fallback(title, body, category_hint)

    text = _input_text(title, summary, eligibility, organization, location,
                       country, region, source, category_hint)
    X = artifact["vectorizer"].transform([text])
    category_model = artifact["category_model"]
    category_probs = category_model.predict_proba(X)[0]
    best_index = int(category_probs.argmax())
    best_category = str(category_model.classes_[best_index])
    minimum = float(artifact["thresholds"].get("category_min_confidence", 0.45))
    if float(category_probs[best_index]) < minimum:
        category = KeywordClassifier().classify(title, body, category_hint)
    else:
        try:
            category = Category(best_category)
        except ValueError:
            category = KeywordClassifier().classify(title, body, category_hint)

    scores: dict[str, dict[str, float]] = {"category": {
        str(label): float(probability) for label, probability in zip(category_model.classes_, category_probs)
    }}
    evidence: dict[str, list[str]] = {
        f"category:{best_category}": _top_terms(artifact, X, category_model, best_index),
    }
    selected: dict[str, list[str]] = {}
    for group in ("brands", "archetypes", "verticals"):
        group_scores: dict[str, float] = {}
        group_selected: list[str] = []
        thresholds = artifact["thresholds"][group]
        for label, head in artifact["heads"][group].items():
            probability = _binary_probability(head, X)
            group_scores[label] = probability
            if probability >= float(thresholds.get(label, 0.5)):
                group_selected.append(label)
            terms = _top_terms(artifact, X, head)
            if terms and (label in group_selected or probability >= 0.35):
                evidence[f"{group}:{label}"] = terms
        scores[group] = group_scores
        selected[group] = group_selected

    # Enforce the declared hierarchy upward. A confident Devsol vertical is
    # also evidence for Devsol and CMS even if those broader heads sit just
    # below their thresholds.
    if selected["verticals"] and "Devsol" not in selected["archetypes"]:
        selected["archetypes"].append("Devsol")
    if selected["archetypes"] and "CMS" not in selected["brands"]:
        selected["brands"].insert(0, "CMS")
    if "CMS" not in selected["brands"]:
        selected["archetypes"] = []
        selected["verticals"] = []
    elif "Devsol" not in selected["archetypes"]:
        selected["verticals"] = []

    routed = bool(selected["verticals"] or "Social Business" in selected["archetypes"]
                  or any(label != "CMS" for label in selected["brands"]))
    all_scores = [value for group in ("brands", "archetypes", "verticals") for value in scores[group].values()]
    uncertain_floor = float(artifact["thresholds"].get("uncertain_floor", 0.35))
    status = "classified" if routed else ("uncertain" if max(all_scores, default=0.0) >= uncertain_floor else "unclassified")

    return HierarchyPrediction(
        category=category,
        brands=selected["brands"],
        archetypes=selected["archetypes"],
        verticals=selected["verticals"],
        scores=scores,
        evidence=evidence,
        status=status,
        source="model",
        version=str(artifact["metadata"]["version"]),
    )


def classify_hierarchy_batch(records: list[dict]) -> list[HierarchyPrediction]:
    """Vectorised inference for maintenance jobs; single-row scraping keeps richer evidence."""
    if not records:
        return []
    artifact = _load()
    if artifact is None:
        return [
            _fallback(
                str(row.get("title", "")),
                " ".join(filter(None, [row.get("summary", ""), row.get("eligibility", ""), row.get("location", "")])),
                row.get("category_hint"),
            )
            for row in records
        ]

    texts = [
        _input_text(
            str(row.get("title", "")), str(row.get("summary", "")),
            str(row.get("eligibility", "")), str(row.get("organization", "")),
            str(row.get("location", "")), str(row.get("country", "")),
            str(row.get("region", "")), str(row.get("source", "")),
            row.get("category_hint"),
        )
        for row in records
    ]
    X = artifact["vectorizer"].transform(texts)
    category_model = artifact["category_model"]
    category_probabilities = category_model.predict_proba(X)
    group_probabilities: dict[str, dict[str, list[float]]] = {}
    for group in ("brands", "archetypes", "verticals"):
        group_probabilities[group] = {}
        for label, head in artifact["heads"][group].items():
            if isinstance(head, dict):
                values = [float(head.get("constant_probability", 0.0))] * len(records)
            else:
                values = [float(value) for value in head.predict_proba(X)[:, 1]]
            group_probabilities[group][label] = values

    predictions: list[HierarchyPrediction] = []
    minimum = float(artifact["thresholds"].get("category_min_confidence", 0.45))
    uncertain_floor = float(artifact["thresholds"].get("uncertain_floor", 0.35))
    for index, row in enumerate(records):
        category_probs = category_probabilities[index]
        best_index = int(category_probs.argmax())
        best_label = str(category_model.classes_[best_index])
        if float(category_probs[best_index]) >= minimum:
            try:
                category = Category(best_label)
            except ValueError:
                category = KeywordClassifier().classify(
                    str(row.get("title", "")), str(row.get("summary", "")), row.get("category_hint"))
        else:
            category = KeywordClassifier().classify(
                str(row.get("title", "")), str(row.get("summary", "")), row.get("category_hint"))

        scores: dict[str, dict[str, float]] = {
            "category": {
                str(label): float(value)
                for label, value in zip(category_model.classes_, category_probs)
            }
        }
        selected: dict[str, list[str]] = {}
        for group in ("brands", "archetypes", "verticals"):
            scores[group] = {
                label: values[index]
                for label, values in group_probabilities[group].items()
            }
            selected[group] = [
                label for label, probability in scores[group].items()
                if probability >= float(artifact["thresholds"][group].get(label, 0.5))
            ]
        if selected["verticals"] and "Devsol" not in selected["archetypes"]:
            selected["archetypes"].append("Devsol")
        if selected["archetypes"] and "CMS" not in selected["brands"]:
            selected["brands"].insert(0, "CMS")
        if "CMS" not in selected["brands"]:
            selected["archetypes"] = []
            selected["verticals"] = []
        elif "Devsol" not in selected["archetypes"]:
            selected["verticals"] = []
        routed = bool(
            selected["verticals"] or "Social Business" in selected["archetypes"]
            or any(label != "CMS" for label in selected["brands"])
        )
        all_scores = [value for group in ("brands", "archetypes", "verticals") for value in scores[group].values()]
        status = "classified" if routed else (
            "uncertain" if max(all_scores, default=0.0) >= uncertain_floor else "unclassified")
        predictions.append(HierarchyPrediction(
            category=category,
            brands=selected["brands"],
            archetypes=selected["archetypes"],
            verticals=selected["verticals"],
            scores=scores,
            evidence={"batch": ["vectorised model inference"]},
            status=status,
            source="model",
            version=str(artifact["metadata"]["version"]),
        ))
    return predictions


def backfill_hierarchy(active_only: bool = True) -> int:
    """Refresh stale automatic labels while preserving every human decision."""
    from sqlalchemy import and_, or_, select

    from app.database.db import session_scope
    from app.database.models import Opportunity
    from app.services.actionable import strict_actionable_clause
    from app.services.brands import brands_to_str
    from app.services.verticals import HUMAN, verticals_to_str

    current_version = model_status()["version"]

    stale = and_(
        or_(Opportunity.verticals_source.is_(None), Opportunity.verticals_source != HUMAN),
        or_(Opportunity.classification_version.is_(None), Opportunity.classification_version != current_version),
    )
    where = and_(stale, strict_actionable_clause()) if active_only else stale
    updated = 0
    last_id = 0
    with session_scope() as db:
        while True:
            rows = db.execute(
                select(Opportunity)
                .where(Opportunity.id > last_id, where)
                .order_by(Opportunity.id)
                .limit(1000)
            ).scalars().all()
            if not rows:
                break
            last_id = rows[-1].id
            payloads = [{
                "title": row.title or "", "summary": row.summary or "",
                "eligibility": row.eligibility or "", "organization": row.organization or "",
                "location": row.location or "", "country": row.country or "",
                "region": row.region or "", "source": row.source_website or "",
                "category_hint": row.category,
            } for row in rows]
            for row, prediction in zip(rows, classify_hierarchy_batch(payloads)):
                ui_verticals = list(prediction.verticals)
                if "Social Business" in prediction.archetypes:
                    ui_verticals.append(VERTICAL_SOCIAL_BUSINESS)
                row.category = prediction.category
                row.verticals = verticals_to_str(ui_verticals)
                row.brands = brands_to_str([brand for brand in prediction.brands if brand != "CMS"])
                row.archetypes = ", ".join(prediction.archetypes)
                row.brand_scores = prediction.scores_json("brands")
                row.brand_evidence = prediction.evidence_json()
                row.brand_classification_version = prediction.version
                row.classification_status = prediction.status
                row.classification_source = prediction.source
                row.classification_version = prediction.version
                row.vertical_scores = prediction.scores_json("verticals")
                row.classification_evidence = prediction.evidence_json()
                row.classified_at = datetime.now(timezone.utc)
                row.verticals_source = "auto"
                updated += 1
            db.commit()
            for row in rows:
                db.expunge(row)
    if updated:
        log.info("hierarchy classification: updated %s row(s)", updated)
    return updated
