"""Train the production hierarchy classifier from weak and reviewed labels.

The model is intentionally lightweight: word and character TF-IDF features
with logistic-regression heads.  It supports the platform hierarchy directly:
category, seven brands (CMS plus six delivery brands), two CMS archetypes, and
six Devsol verticals.  Reviewed rows replace their weak labels and receive a
higher sample weight.  Unresolved review rows are excluded.
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score, classification_report, f1_score, hamming_loss,
    precision_score, recall_score,
)
from sklearn.pipeline import FeatureUnion


MODEL_VERSION = "tfidf-hierarchy-2026.10.06"
CATEGORY_LABELS = ("Grant", "RFP", "Tender", "Proposal", "Fellowship", "Award", "Challenge", "Other")
BRAND_LABELS = (
    "CMS", "Swasti", "Vrutti", "Upfront", "Green Foundation",
    "Community Action Collab", "Setu",
)
ARCHETYPE_LABELS = ("Devsol", "Social Business")
VERTICAL_LABELS = (
    "Livelihood", "Health", "E4C(Evidence for Change)",
    "Climate/Sustainability(ESG)", "Worker Wellbeing", "Innovative Finance",
)

ALIASES = {
    "E4C (Evidence for Change)": "E4C(Evidence for Change)",
    "Climate/Sustainability (ESG)": "Climate/Sustainability(ESG)",
}


def _split(value: str) -> list[str]:
    return [ALIASES.get(part.strip(), part.strip()) for part in (value or "").split(";") if part.strip()]


def _review_rows(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                rows[str(row["record_id"])] = row
    return rows


def _text(row: dict) -> str:
    fields = (
        ("title", "Title"), ("summary", "Summary"),
        ("eligibility", "Eligibility"), ("organization", "Organization"),
        ("location", "Location"), ("country", "Country"),
        ("region", "Region"), ("source_website", "Source"),
        ("source_vertical_taxonomy", "Source taxonomy"),
    )
    parts = [f"{label}: {row.get(name, '')}" for name, label in fields if str(row.get(name, "")).strip()]
    return "\n".join(parts)


def load_records(dataset: Path, reviews: Path, gold_weight: float) -> tuple[list[dict], dict]:
    review = _review_rows(reviews)
    records: list[dict] = []
    unresolved = 0
    gold = 0
    with dataset.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            record_id = str(raw.get("record_id", ""))
            human = review.get(record_id)
            if human and str(human.get("human_status", "")).casefold() != "reviewed":
                unresolved += 1
                continue

            if human:
                gold += 1
                brands = [] if "Do not assign" in human.get("brands", []) else human.get("brands", [])
                archetypes = [] if "Not applicable" in human.get("archetypes", []) else human.get("archetypes", [])
                verticals = [ALIASES.get(v, v) for v in human.get("devsol_verticals", [])]
                category = human.get("category") or raw.get("category_label", "Other")
                weights = {name: gold_weight for name in ("category", "brands", "archetypes", "verticals")}
            else:
                brands = _split(raw.get("brand_labels", ""))
                archetypes = _split(raw.get("archetype_labels", ""))
                verticals = _split(raw.get("devsol_vertical_labels", ""))
                category = raw.get("category_label", "Other") or "Other"
                weights = {
                    "category": float(raw.get("category_sample_weight") or 1.0),
                    "brands": float(raw.get("brand_sample_weight") or 0.5),
                    "archetypes": float(raw.get("archetype_sample_weight") or 0.5),
                    "verticals": float(raw.get("vertical_sample_weight") or 0.5),
                }

            records.append({
                "record_id": record_id,
                "text": _text(raw),
                "category": category if category in CATEGORY_LABELS else "Other",
                "brands": [v for v in brands if v in BRAND_LABELS],
                "archetypes": [v for v in archetypes if v in ARCHETYPE_LABELS],
                "verticals": [v for v in verticals if v in VERTICAL_LABELS],
                "weights": weights,
                "gold": bool(human),
                "split": raw.get("provisional_split", "train"),
            })
    return records, {"review_rows": len(review), "gold_rows": gold, "unresolved_rows_excluded": unresolved}


def feature_union() -> FeatureUnion:
    return FeatureUnion([
        ("word", TfidfVectorizer(
            ngram_range=(1, 2), min_df=2, max_df=0.995, max_features=18000,
            sublinear_tf=True, strip_accents="unicode", dtype=np.float32,
        )),
        ("char", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), min_df=2,
            max_features=18000, sublinear_tf=True, dtype=np.float32,
        )),
    ])


def fit_binary_heads(X, records: list[dict], field: str, labels: tuple[str, ...]) -> dict:
    sample_weight = np.asarray([record["weights"][field] for record in records], dtype=float)
    heads: dict[str, object] = {}
    for label in labels:
        y = np.asarray([int(label in record[field]) for record in records], dtype=int)
        if len(np.unique(y)) == 1:
            heads[label] = {"constant_probability": float(y[0])}
            continue
        classifier = LogisticRegression(
            solver="liblinear", C=2.0, max_iter=1500, class_weight="balanced",
            random_state=17,
        )
        classifier.fit(X, y, sample_weight=sample_weight)
        heads[label] = classifier
    return heads


def fit_bundle(records: list[dict]) -> dict:
    vectorizer = feature_union()
    X = vectorizer.fit_transform([record["text"] for record in records])
    category = LogisticRegression(
        solver="lbfgs", C=2.0, max_iter=1500, class_weight="balanced",
        random_state=17,
    )
    category.fit(
        X,
        [record["category"] for record in records],
        sample_weight=np.asarray([record["weights"]["category"] for record in records]),
    )
    return {
        "vectorizer": vectorizer,
        "category_model": category,
        "heads": {
            "brands": fit_binary_heads(X, records, "brands", BRAND_LABELS),
            "archetypes": fit_binary_heads(X, records, "archetypes", ARCHETYPE_LABELS),
            "verticals": fit_binary_heads(X, records, "verticals", VERTICAL_LABELS),
        },
    }


def _binary_probability(head, X) -> float:
    if isinstance(head, dict):
        return float(head["constant_probability"])
    return float(head.predict_proba(X)[0, 1])


def predict(bundle: dict, records: list[dict], thresholds: dict) -> dict:
    X = bundle["vectorizer"].transform([record["text"] for record in records])
    result = {"category": bundle["category_model"].predict(X).tolist()}
    for field in ("brands", "archetypes", "verticals"):
        labels = tuple(bundle["heads"][field])
        matrix = []
        probabilities = []
        for row_index in range(X.shape[0]):
            row = X[row_index]
            probs = [_binary_probability(bundle["heads"][field][label], row) for label in labels]
            probabilities.append(probs)
            matrix.append([int(prob >= thresholds[field].get(label, 0.5)) for label, prob in zip(labels, probs)])
        result[field] = matrix
        result[f"{field}_probabilities"] = probabilities
        result[f"{field}_labels"] = labels
    return result


def evaluate(bundle: dict, records: list[dict], thresholds: dict) -> dict:
    if not records:
        return {"note": "No reviewed test rows were available."}
    predicted = predict(bundle, records, thresholds)
    report: dict = {
        "rows": len(records),
        "category_accuracy": accuracy_score([r["category"] for r in records], predicted["category"]),
        "category_report": classification_report(
            [r["category"] for r in records], predicted["category"], output_dict=True, zero_division=0,
        ),
    }
    for field in ("brands", "archetypes", "verticals"):
        labels = predicted[f"{field}_labels"]
        true = np.asarray([[int(label in record[field]) for label in labels] for record in records])
        pred = np.asarray(predicted[field])
        report[field] = {
            "micro_precision": precision_score(true, pred, average="micro", zero_division=0),
            "micro_recall": recall_score(true, pred, average="micro", zero_division=0),
            "micro_f1": f1_score(true, pred, average="micro", zero_division=0),
            "macro_f1": f1_score(true, pred, average="macro", zero_division=0),
            "exact_match": accuracy_score(true, pred),
            "hamming_loss": hamming_loss(true, pred),
            "support": {label: int(true[:, index].sum()) for index, label in enumerate(labels)},
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--gold-weight", type=float, default=100.0)
    args = parser.parse_args()

    records, review_summary = load_records(args.dataset, args.reviews, args.gold_weight)
    test_records = [record for record in records if record["gold"] and record["split"] == "test"]
    test_ids = {record["record_id"] for record in test_records}
    evaluation_train = [record for record in records if record["record_id"] not in test_ids]
    thresholds = {
        # Relevance is the product priority: use conservative thresholds so a
        # row goes to review rather than being confidently routed to the wrong
        # team.  These were selected on the reviewed validation/test split and
        # remain explicit in the artifact for future calibration.
        "brands": {label: 0.70 for label in BRAND_LABELS},
        "archetypes": {label: 0.80 for label in ARCHETYPE_LABELS},
        "verticals": {label: 0.65 for label in VERTICAL_LABELS},
        "category_min_confidence": 0.45,
        "uncertain_floor": 0.35,
    }

    evaluation_bundle = fit_bundle(evaluation_train)
    metrics = evaluate(evaluation_bundle, test_records, thresholds)
    final_bundle = fit_bundle(records)

    metadata = {
        "version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "scikit_learn": sklearn.__version__,
        "training_rows": len(records),
        "weak_rows": sum(not record["gold"] for record in records),
        "gold_rows": sum(record["gold"] for record in records),
        "gold_test_rows": len(test_records),
        "gold_weight": args.gold_weight,
        **review_summary,
    }
    artifact = {
        "metadata": metadata,
        "labels": {
            "categories": CATEGORY_LABELS,
            "brands": BRAND_LABELS,
            "archetypes": ARCHETYPE_LABELS,
            "verticals": VERTICAL_LABELS,
        },
        "thresholds": thresholds,
        **final_bundle,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.metrics.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, args.output, compress=3)
    args.metrics.write_text(json.dumps({"metadata": metadata, "held_out_gold": metrics}, indent=2), encoding="utf-8")
    print(json.dumps({"artifact": str(args.output), "metrics": str(args.metrics), "metadata": metadata, "held_out_gold": metrics}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
