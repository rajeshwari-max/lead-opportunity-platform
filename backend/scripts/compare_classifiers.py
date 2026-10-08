"""Rule-based (weighted keyword) vs ML (TF-IDF hierarchy) classifier — a fair comparison.

    python scripts/compare_classifiers.py fold  --dataset CSV --out DIR --fold 0
    python scripts/compare_classifiers.py report --out DIR
    python scripts/compare_classifiers.py coverage --dataset CSV --out DIR

Read-only: nothing in the database or the model artifact is changed.

Why cross-validation
--------------------
The shipped model was trained WITH the 98 human-reviewed rows (weight 100), so
scoring it on those rows would be marking its own homework. Instead the
reviewed rows are split into 5 folds, grouped by title so a duplicate of a
test row can never sit in training. For each fold a fresh model is trained on
everything except that fold — the same features, heads, thresholds and
hierarchy post-processing as production — and both methods label the fold.
Every reviewed row is therefore scored exactly once by a model that never saw
it, and the rules see the identical rows.

The rule path is ml_hierarchy._fallback, i.e. exactly what production runs
when the model is missing: classification_model.classify (weighted vertical
keywords, per-label cut-offs) + brands.classify_brands (weighted brand terms).
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import joblib  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.model_selection import GroupKFold  # noqa: E402

import train_hierarchy_model as T  # noqa: E402
from app.services import ml_hierarchy as M  # noqa: E402

FOLDS = 5
GROUPS = {"brands": T.BRAND_LABELS, "archetypes": T.ARCHETYPE_LABELS, "verticals": T.VERTICAL_LABELS}
REVIEWS = Path(__file__).resolve().parents[1] / "ml" / "review_labels.jsonl"


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").casefold()).strip()


def _raw_rows(dataset: Path) -> dict[str, dict]:
    with dataset.open(encoding="utf-8-sig", newline="") as handle:
        return {r["record_id"]: r for r in csv.DictReader(handle)}


def _batch_input(raw: dict) -> dict:
    return {"title": raw.get("title", ""), "summary": raw.get("summary", ""),
            "eligibility": raw.get("eligibility", ""), "organization": raw.get("organization", ""),
            "location": raw.get("location", ""), "country": raw.get("country", ""),
            "region": raw.get("region", ""), "source": raw.get("source_website", ""),
            "category_hint": None}


def _as_dict(p) -> dict:
    cat = p.category.value if hasattr(p.category, "value") else str(p.category)
    return {"category": cat, "brands": list(p.brands), "archetypes": list(p.archetypes),
            "verticals": [T.ALIASES.get(v, v) for v in p.verticals], "status": p.status}


def _rules(raw: dict) -> dict:
    body = " ".join(filter(None, [raw.get("summary", ""), raw.get("eligibility", ""), raw.get("location", "")]))
    return _as_dict(M._fallback(raw.get("title", ""), body, None))


def _ml(artifact: dict, raws: list[dict]) -> list[dict]:
    original = M._load
    M._load = lambda: artifact
    try:
        return [_as_dict(p) for p in M.classify_hierarchy_batch([_batch_input(r) for r in raws])]
    finally:
        M._load = original


def _gold_split(records: list[dict], raws: dict[str, dict]):
    gold = [r for r in records if r["gold"]]
    groups = [_norm(raws[r["record_id"]]["title"]) for r in gold]
    return gold, list(GroupKFold(n_splits=FOLDS).split(gold, groups=groups))


def cmd_fold(args) -> int:
    raws = _raw_rows(args.dataset)
    records, _ = T.load_records(args.dataset, REVIEWS, 100.0)
    gold, splits = _gold_split(records, raws)
    _, test_idx = splits[args.fold]
    test = [gold[i] for i in test_idx]
    test_titles = {_norm(raws[r["record_id"]]["title"]) for r in test}
    train = [r for r in records if _norm(raws[r["record_id"]]["title"]) not in test_titles]
    production = joblib.load(M.MODEL_PATH)
    artifact = {"metadata": {"version": f"cv-fold-{args.fold}"}, "labels": production["labels"],
                "thresholds": production["thresholds"], **T.fit_bundle(train)}
    test_raw = [raws[r["record_id"]] for r in test]
    ml = _ml(artifact, test_raw)
    out = []
    for rec, raw, m in zip(test, test_raw, ml):
        out.append({"record_id": rec["record_id"], "title": raw["title"],
                    "gold": {k: rec[k] for k in ("category", "brands", "archetypes", "verticals")},
                    "rules": _rules(raw), "ml": m})
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"fold{args.fold}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"fold {args.fold}: trained on {len(train)} rows, scored {len(test)} reviewed rows")
    return 0


def _prf(rows: list[dict], method: str, group: str) -> dict:
    labels = GROUPS[group]
    tp = fp = fn = 0
    per = {}
    for label in labels:
        t = p = f = 0
        for r in rows:
            g, h = label in r["gold"][group], label in r[method][group]
            t += g and h; p += (not g) and h; f += g and not h
        per[label] = (t, p, f)
        tp += t; fp += p; fn += f
    def f1(t, p, f):
        return 0.0 if 2 * t + p + f == 0 else 2 * t / (2 * t + p + f)
    supported = [l for l in labels if per[l][0] + per[l][2] > 0]
    return {"precision": tp / (tp + fp) if tp + fp else 0.0, "recall": tp / (tp + fn) if tp + fn else 0.0,
            "micro_f1": f1(tp, fp, fn),
            "macro_f1": float(np.mean([f1(*per[l]) for l in supported])) if supported else 0.0,
            "exact": sum(set(r["gold"][group]) == set(r[method][group]) for r in rows) / len(rows),
            "per_label_f1": {l: round(f1(*per[l]), 3) for l in supported}}


def _misc(pred: dict) -> bool:
    return not (pred["verticals"] or "Social Business" in pred["archetypes"]
                or any(b != "CMS" for b in pred["brands"]))


def cmd_report(args) -> int:
    rows = [r for f in sorted(args.out.glob("fold*.json")) for r in json.loads(f.read_text(encoding="utf-8"))]
    report = {"rows": len(rows), "methods": {}}
    for method in ("rules", "ml"):
        m = {g: _prf(rows, method, g) for g in GROUPS}
        m["category_accuracy"] = sum(r["gold"]["category"] == r[method]["category"] for r in rows) / len(rows)
        m["all_four_exact"] = sum(all(set(r["gold"][g]) == set(r[method][g]) for g in GROUPS)
                                  and r["gold"]["category"] == r[method]["category"] for r in rows) / len(rows)
        m["left_unrouted"] = sum(_misc(r[method]) for r in rows) / len(rows)
        report["methods"][method] = m
    report["gold_unrouted"] = sum(_misc({**r["gold"], "status": ""}) for r in rows) / len(rows)

    # Paired bootstrap on the difference (ML minus rules).
    rng = random.Random(17)
    diffs = {g: [] for g in GROUPS}
    for _ in range(2000):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        for g in GROUPS:
            diffs[g].append(_prf(sample, "ml", g)["micro_f1"] - _prf(sample, "rules", g)["micro_f1"])
    report["ml_minus_rules_micro_f1_95ci"] = {
        g: [round(float(np.percentile(v, 2.5)), 3), round(float(np.percentile(v, 97.5)), 3)] for g, v in diffs.items()}
    report["disagreements"] = [
        {"title": r["title"][:110], "gold": r["gold"]["verticals"] + r["gold"]["brands"],
         "rules": r["rules"]["verticals"] + r["rules"]["brands"], "ml": r["ml"]["verticals"] + r["ml"]["brands"]}
        for r in rows if (set(r["rules"]["verticals"]) | set(r["rules"]["brands"]))
        != (set(r["ml"]["verticals"]) | set(r["ml"]["brands"]))][:40]
    (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "disagreements"}, indent=1))
    return 0


def cmd_coverage(args) -> int:
    raws = list(_raw_rows(args.dataset).values())
    rules = [_rules(r) for r in raws]
    ml = _ml(joblib.load(M.MODEL_PATH), raws)
    n = len(raws)
    out = {"rows": n,
           "rules_unrouted": sum(map(_misc, rules)) / n, "ml_unrouted": sum(map(_misc, ml)) / n,
           "vertical_set_agreement": sum(set(a["verticals"]) == set(b["verticals"]) for a, b in zip(rules, ml)) / n,
           "brand_set_agreement": sum(set(a["brands"]) == set(b["brands"]) for a, b in zip(rules, ml)) / n,
           "note": "In-sample for ML: the shipped model was trained on these rows' rule labels."}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "coverage.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("fold", "report", "coverage"))
    ap.add_argument("--dataset", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fold", type=int, default=0)
    args = ap.parse_args()
    return {"fold": cmd_fold, "report": cmd_report, "coverage": cmd_coverage}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
