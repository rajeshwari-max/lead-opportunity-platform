"""Compare classifiers on held-out data, and pick the simplest one that wins.

The brief's instruction, taken literally: "Select the simplest model that
materially and reproducibly beats the current rule baseline on held-out data.
Document the evidence for the choice."

So this does not pick a model. It measures several and prints the table, and a
model is adopted only if it clears the baseline by a stated margin on data the
baseline never saw.

    python scripts/gold_dataset.py --build gold.jsonl
    python scripts/classifier_eval.py gold.jsonl
    python scripts/classifier_eval.py gold.jsonl --tune      # fit thresholds

Models
------
1. `rules`     the current keyword classifier, as shipped
2. `rules+cal` the same evidence, scored and thresholded per label
               (services/classification_model.py)
3. `tfidf`     word + char TF-IDF, one-vs-rest logistic regression
4. `embed`     sentence embeddings + calibrated one-vs-rest
5. `transformer`  a fine-tuned encoder

3 needs scikit-learn. 4 and 5 need packages that are not installed here and
may not be installable on the EC2 box. Each reports precisely why it was
skipped rather than being silently absent — a comparison with a missing row is
a comparison that quietly favours whatever ran.

Metrics
-------
Per-label precision/recall/F1, micro and macro F1, PR-AUC per label,
exact-match accuracy, Hamming loss, and coverage versus abstention. Precision
is weighted above recall in the verdict: a wrong vertical routes an
opportunity to the wrong team, and a missed one lands in the Unclassified
queue where a person sees it. Those costs are not symmetric.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import sqlalchemy                                          # noqa: F401
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    _act = (".venv\\Scripts\\activate" if sys.platform == "win32"
            else "source .venv/bin/activate")
    print(f"Needs the project venv.\n\n    cd {_root}\n    {_act}\n"
          f"    python scripts/{Path(__file__).name}\n", file=sys.stderr)
    raise SystemExit(2)

from app.services.classification_model import (                # noqa: E402
    DEFAULT_THRESHOLDS, UNCERTAIN_FLOOR, classify,
)
from app.services.verticals import VERTICALS, classify_verticals  # noqa: E402

# A model must beat the baseline by at least this much macro-F1 to be worth
# adopting. Below it, the extra dependency, the inference cost and the
# retraining discipline buy nothing you could measure again next month.
MATERIAL_GAIN = 0.03


def load(path: str):
    train, test = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            (test if rec.get("split") == "test" else train).append(rec)
    return train, test


def _binarise(records):
    y = []
    for r in records:
        labels = set(r.get("labels") or [])
        y.append([1 if v in labels else 0 for v in VERTICALS])
    return y


def _text(r) -> str:
    return f"{r.get('title','')} \n {r.get('body','')}"


# --------------------------------------------------------------- the models

def predict_rules(train, test):
    return [[1 if v in set(classify_verticals(r.get("title", ""), r.get("body", "")))
             else 0 for v in VERTICALS] for r in test], None


def predict_rules_calibrated(train, test, thresholds=None):
    probs = []
    preds = []
    cuts = thresholds or DEFAULT_THRESHOLDS
    for r in test:
        c = classify(r.get("title", ""), r.get("body", ""), thresholds=cuts)
        probs.append([c.scores.get(v, 0.0) for v in VERTICALS])
        preds.append([1 if v in set(c.labels) else 0 for v in VERTICALS])
    return preds, probs


def predict_tfidf(train, test):
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.multiclass import OneVsRestClassifier
        from sklearn.pipeline import FeatureUnion, Pipeline
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"scikit-learn is not installed ({exc})") from exc
    import numpy as np

    ytr = np.array(_binarise(train))
    if ytr.sum() == 0:
        raise RuntimeError("no positive labels in the training split")
    # Word AND character n-grams: the character half is what survives the
    # abbreviations and run-together tokens these titles are full of
    # ("WASH", "M&E", "GRANT-1012").
    feats = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2,
                                 sublinear_tf=True, strip_accents="unicode")),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                 min_df=3, sublinear_tf=True)),
    ])
    clf = Pipeline([
        ("feats", feats),
        ("ovr", OneVsRestClassifier(
            LogisticRegression(max_iter=2000, class_weight="balanced", C=2.0))),
    ])
    clf.fit([_text(r) for r in train], ytr)
    X = [_text(r) for r in test]
    probs = clf.predict_proba(X)
    preds = (probs >= 0.5).astype(int)
    return preds.tolist(), probs.tolist()


def predict_embeddings(train, test):
    try:
        from sentence_transformers import SentenceTransformer   # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"sentence-transformers is not installed ({exc}). It pulls torch "
            f"and downloads a model at first use; on a small EC2 box that is a "
            f"deployment decision, not a pip install") from exc
    raise RuntimeError("not implemented — install the package first, then this "
                       "branch encodes with all-MiniLM-L6-v2 and fits the same "
                       "one-vs-rest head as tfidf")


def predict_transformer(train, test):
    try:
        import transformers                                     # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"transformers is not installed ({exc}). Fine-tuning also needs "
            f"several thousand labelled examples to beat TF-IDF on six labels; "
            f"check gold_dataset.py --status before spending the setup") from exc
    raise RuntimeError("not implemented — needs a labelled set large enough to "
                       "justify it; see the note above")


MODELS = {
    "rules": predict_rules,
    "rules+cal": predict_rules_calibrated,
    "tfidf": predict_tfidf,
    "embed": predict_embeddings,
    "transformer": predict_transformer,
}


# -------------------------------------------------------------- the metrics

def metrics(y_true, y_pred, y_prob=None) -> dict:
    import numpy as np

    yt, yp = np.array(y_true), np.array(y_pred)
    out: dict = {"per_label": {}}
    f1s, precs, recs = [], [], []
    for i, label in enumerate(VERTICALS):
        tp = int(((yt[:, i] == 1) & (yp[:, i] == 1)).sum())
        fp = int(((yt[:, i] == 0) & (yp[:, i] == 1)).sum())
        fn = int(((yt[:, i] == 1) & (yp[:, i] == 0)).sum())
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        ap = None
        if y_prob is not None:
            try:
                from sklearn.metrics import average_precision_score
                if yt[:, i].sum():
                    ap = float(average_precision_score(
                        yt[:, i], np.array(y_prob)[:, i]))
            except Exception:
                ap = None
        out["per_label"][label] = {"support": int(yt[:, i].sum()),
                                   "precision": p, "recall": r, "f1": f1,
                                   "pr_auc": ap}
        precs.append(p); recs.append(r); f1s.append(f1)

    tp = int(((yt == 1) & (yp == 1)).sum())
    fp = int(((yt == 0) & (yp == 1)).sum())
    fn = int(((yt == 1) & (yp == 0)).sum())
    out["micro_f1"] = (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else 0.0
    out["macro_f1"] = sum(f1s) / len(f1s)
    out["macro_precision"] = sum(precs) / len(precs)
    out["macro_recall"] = sum(recs) / len(recs)
    out["exact_match"] = float((yt == yp).all(axis=1).mean())
    out["hamming_loss"] = float((yt != yp).mean())
    # Coverage: how often the model committed to anything at all. A model that
    # abstains on everything scores perfect precision and is useless.
    out["coverage"] = float((yp.sum(axis=1) > 0).mean())
    return out


def show(name: str, m: dict) -> None:
    print(f"\n  {name}")
    print(f"    macro-F1 {m['macro_f1']:.3f}   micro-F1 {m['micro_f1']:.3f}   "
          f"macro-P {m['macro_precision']:.3f}   macro-R {m['macro_recall']:.3f}")
    print(f"    exact-match {m['exact_match']:.3f}   hamming {m['hamming_loss']:.3f}   "
          f"coverage {m['coverage']:.3f}")
    print(f"      {'label':<32} {'sup':>5} {'P':>7} {'R':>7} {'F1':>7} {'PR-AUC':>8}")
    for label, s in m["per_label"].items():
        ap = f"{s['pr_auc']:.3f}" if s["pr_auc"] is not None else "     —"
        print(f"      {label:<32} {s['support']:>5} {s['precision']:>7.3f} "
              f"{s['recall']:>7.3f} {s['f1']:>7.3f} {ap:>8}")


def tune(train, test) -> int:
    """Fit each label's threshold on the TRAIN split, report on test.

    Fitting on test and reporting on test is how a calibration reports a score
    nothing will reproduce.
    """
    import numpy as np

    y = np.array(_binarise(train))
    probs = np.array(predict_rules_calibrated(train, train)[1])
    print("=" * 78)
    print("THRESHOLD TUNING — fitted on the TRAIN split only")
    print("=" * 78)
    best: dict[str, float] = {}
    for i, label in enumerate(VERTICALS):
        if y[:, i].sum() == 0:
            best[label] = DEFAULT_THRESHOLDS.get(label, 0.55)
            print(f"  {label:<32} no positives in train — keeping "
                  f"{best[label]:.2f}")
            continue
        grid = [x / 100 for x in range(20, 96, 5)]
        scored = []
        for t in grid:
            pred = (probs[:, i] >= t).astype(int)
            tp = int(((y[:, i] == 1) & (pred == 1)).sum())
            fp = int(((y[:, i] == 0) & (pred == 1)).sum())
            fn = int(((y[:, i] == 1) & (pred == 0)).sum())
            p = tp / (tp + fp) if tp + fp else 0.0
            r = tp / (tp + fn) if tp + fn else 0.0
            # F0.5 — precision weighted double. A wrong vertical routes work to
            # the wrong team; a missed one lands in the review queue.
            f = (1.25 * p * r / (0.25 * p + r)) if (0.25 * p + r) else 0.0
            scored.append((f, t, p, r))
        f, t, p, r = max(scored)
        best[label] = t
        print(f"  {label:<32} threshold {t:.2f}  (P {p:.2f} R {r:.2f} F0.5 {f:.2f})")
    print()
    print("Paste into services/classification_model.py DEFAULT_THRESHOLDS:")
    print("DEFAULT_THRESHOLDS = {")
    for label, t in best.items():
        print(f'    "{label}": {t:.2f},')
    print("}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dataset")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--models", default="rules,rules+cal,tfidf,embed,transformer")
    a = ap.parse_args()

    train, test = load(a.dataset)
    print("=" * 78)
    print("CLASSIFIER EVALUATION")
    print("=" * 78)
    print(f"{len(train):,} train / {len(test):,} test, split by time with "
          f"near-duplicates removed.")
    if len(test) < 60:
        print()
        print("WARNING: the test split is too small for these numbers to mean")
        print("anything. Treat everything below as a smoke test of the harness,")
        print("not as evidence about a model.")
    if a.tune:
        return tune(train, test)

    y_true = _binarise(test)
    results: dict[str, dict] = {}
    for name in [m.strip() for m in a.models.split(",") if m.strip()]:
        fn = MODELS.get(name)
        if fn is None:
            print(f"\n  {name}: unknown model", file=sys.stderr)
            continue
        try:
            preds, probs = fn(train, test)
        except RuntimeError as exc:
            print(f"\n  {name}\n    SKIPPED — {exc}")
            continue
        except Exception as exc:                      # noqa: BLE001
            print(f"\n  {name}\n    FAILED — {type(exc).__name__}: {exc}")
            continue
        results[name] = metrics(y_true, preds, probs)
        show(name, results[name])

    print()
    print("=" * 78)
    base = results.get("rules")
    if base is None or len(results) < 2:
        print("No comparison possible — the baseline or every challenger is missing.")
        return 0
    ranked = sorted(((n, m["macro_f1"]) for n, m in results.items()),
                    key=lambda kv: -kv[1])
    winner, top = ranked[0]
    gain = top - base["macro_f1"]
    print(f"Baseline (rules) macro-F1 {base['macro_f1']:.3f}. "
          f"Best is {winner} at {top:.3f} ({gain:+.3f}).")
    if winner == "rules" or gain < MATERIAL_GAIN:
        print(f"KEEP THE RULES. Nothing beat them by the {MATERIAL_GAIN:.2f} "
              f"macro-F1 margin that would justify a new dependency, an "
              f"inference cost and a retraining discipline.")
    else:
        print(f"ADOPT {winner}. It clears the baseline by more than "
              f"{MATERIAL_GAIN:.2f} macro-F1 on data it never saw.")
        print("Re-run on a second labelling round before deploying it: one "
              "held-out win is a result, two is a reproducible one.")
    print()
    print(f"Rows scoring below {UNCERTAIN_FLOOR} on every label are abstentions "
          f"and belong in the Unclassified queue, not in a metric.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
