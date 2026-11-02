"""Does the meaning-based layer actually classify better? Measure, don't guess.

    python scripts/semantic_eval.py
    python scripts/semantic_eval.py --dashboard-labels
    python scripts/semantic_eval.py --profiles /tmp/my_profiles.json --floor 0.1

Read-only: nothing is written to the database.

What it compares, on leads a PERSON has labelled
-------------------------------------------------
  ML only      the current production classifier (TF-IDF + logistic regression)
  meaning only the semantic layer by itself, at each similarity threshold
  blend        ML labels plus the meaning labels over the threshold — exactly
               what LOP_SEMANTIC_MODE=blend would put on the dashboard

for the labels the semantic layer scores: the six non-CMS brands, Social
Business, and the six Devsol verticals. CMS and Devsol are derived from
those by the hierarchy, so scoring them again would only repeat the result.

Gold labels come from ml/review_labels.jsonl (the reviewed workbook) and,
with --dashboard-labels, from rows the team labelled by hand on the dashboard.

Read the ML numbers with care: the model was TRAINED on most of the reviewed
rows (98 gold rows at weight 100; 17 held out). On those rows the ML score is
optimistic, while the semantic layer has never seen any of them. If the blend
still wins here, it wins by more on new leads.

Output: the table on screen, a recommended LOP_SEMANTIC_MIN_SIMILARITY, a few
disagreements to read, and data/debug/semantic_eval_<date>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.db import session_scope  # noqa: E402
from app.database.models import Opportunity  # noqa: E402
from app.services import semantic_classifier as sc  # noqa: E402
from app.services.review_labels import ALIASES, load_reviewed  # noqa: E402
from app.services.verticals import HUMAN, VERTICAL_SOCIAL_BUSINESS, str_to_verticals  # noqa: E402

LEAVES = {group: set(labels) for group, labels in sc.LABELS.items()}


def _gold_from_review(review: dict) -> dict[str, set[str]]:
    brands = [] if "Do not assign" in review.get("brands", []) else review.get("brands", [])
    archetypes = [] if "Not applicable" in review.get("archetypes", []) else review.get("archetypes", [])
    verticals = [ALIASES.get(v, v) for v in review.get("devsol_verticals", [])]
    return {"brands": set(brands) & LEAVES["brands"],
            "archetypes": set(archetypes) & LEAVES["archetypes"],
            "verticals": set(verticals) & LEAVES["verticals"]}


def _gold_from_row(row: Opportunity) -> dict[str, set[str]]:
    verticals = set(str_to_verticals(row.verticals or ""))
    archetypes = {"Social Business"} if VERTICAL_SOCIAL_BUSINESS in verticals else set()
    archetypes |= {a.strip() for a in (row.archetypes or "").split(",") if a.strip()}
    brands = {b.strip() for b in (row.brands or "").split(",") if b.strip()}
    return {"brands": brands & LEAVES["brands"],
            "archetypes": archetypes & LEAVES["archetypes"],
            "verticals": verticals & LEAVES["verticals"]}


def _payload(row: Opportunity) -> dict:
    return {"title": row.title or "", "summary": row.summary or "",
            "eligibility": row.eligibility or "", "organization": row.organization or "",
            "location": row.location or "", "country": row.country or "",
            "region": row.region or "", "source": row.source_website or "",
            "category_hint": row.category}


def load_gold(dashboard_labels: bool) -> list[dict]:
    reviewed = {str(r["record_id"]): r for r in load_reviewed()}
    out: list[dict] = []
    with session_scope() as db:
        if reviewed:
            for row in db.execute(select(Opportunity).where(
                    Opportunity.unique_id.in_(list(reviewed)))).scalars():
                out.append({"id": row.unique_id, "origin": "review workbook",
                            "payload": _payload(row),
                            "gold": _gold_from_review(reviewed[row.unique_id])})
        if dashboard_labels:
            seen = {item["id"] for item in out}
            for row in db.execute(select(Opportunity).where(
                    Opportunity.verticals_source == HUMAN)).scalars():
                if row.unique_id not in seen:
                    out.append({"id": row.unique_id, "origin": "dashboard",
                                "payload": _payload(row), "gold": _gold_from_row(row)})
    return out


def _metrics(pairs: list[tuple[set[str], set[str]]]) -> dict:
    tp = sum(len(pred & gold) for pred, gold in pairs)
    fp = sum(len(pred - gold) for pred, gold in pairs)
    fn = sum(len(gold - pred) for pred, gold in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    exact = sum(pred == gold for pred, gold in pairs) / len(pairs) if pairs else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3), "exact": round(exact, 3)}


def _evaluate(items, ml_sets, sem_scores, threshold, floor, ml_probs):
    report = {}
    for group in ("brands", "verticals", "archetypes", "all"):
        groups = ("brands", "archetypes", "verticals") if group == "all" else (group,)
        ml_pairs, sem_pairs, blend_pairs = [], [], []
        for item, ml, scores, probs in zip(items, ml_sets, sem_scores, ml_probs):
            gold = set().union(*(item["gold"][g] for g in groups))
            ml_pred = set().union(*(ml[g] for g in groups))
            sem_pred = {label for g in groups for label, value in scores[g].items()
                        if value >= threshold}
            blend = ml_pred | {label for label in sem_pred
                               if probs.get(label, 0.0) >= floor}
            ml_pairs.append((ml_pred, gold))
            sem_pairs.append((sem_pred, gold))
            blend_pairs.append((blend, gold))
        report[group] = {"ml": _metrics(ml_pairs), "semantic": _metrics(sem_pairs),
                         "blend": _metrics(blend_pairs)}
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dashboard-labels", action="store_true",
                    help="also use rows the team labelled by hand on the dashboard")
    ap.add_argument("--profiles", default="", help="try an edited semantic_profiles.json")
    ap.add_argument("--floor", type=float, default=float(settings.semantic_ml_floor),
                    help="ML probability a meaning label needs to be added (blend)")
    ap.add_argument("--examples", type=int, default=8)
    args = ap.parse_args()

    if args.profiles:
        settings.semantic_profiles_path = args.profiles
    # Production ML labels, with no semantic influence at all.
    settings.semantic_mode = "off"
    from app.services.ml_hierarchy import classify_hierarchy_batch

    items = load_gold(args.dashboard_labels)
    if not items:
        print("No human-labelled leads found in the database. Nothing to measure.")
        return 1
    print(f"{len(items)} human-labelled lead(s): "
          + ", ".join(f"{sum(i['origin'] == o for i in items)} from {o}"
                      for o in sorted({i['origin'] for i in items})))

    predictions = classify_hierarchy_batch([i["payload"] for i in items])
    ml_sets = [{"brands": set(p.brands) & LEAVES["brands"],
                "archetypes": set(p.archetypes) & LEAVES["archetypes"],
                "verticals": set(p.verticals) & LEAVES["verticals"]} for p in predictions]
    ml_probs = [{label: float(v) for g in ("brands", "archetypes", "verticals")
                 for label, v in p.scores.get(g, {}).items()} for p in predictions]

    texts = [sc.lead_text(i["payload"]["title"], i["payload"]["summary"],
                          i["payload"]["eligibility"]) for i in items]
    results = sc.score_texts(texts, threshold=1.1)     # scores only; we choose the cut
    if results is None:
        print("The semantic model could not be loaded:", sc.status()["error"])
        print("Check that `fastembed` is installed and the server can reach huggingface.co.")
        return 2
    sem_scores = [r.scores for r in results]

    thresholds = [round(0.50 + 0.02 * step, 2) for step in range(21)]   # 0.50 … 0.90
    sweep = {t: _evaluate(items, ml_sets, sem_scores, t, args.floor, ml_probs)
             for t in thresholds}
    best = max(thresholds, key=lambda t: (sweep[t]["all"]["blend"]["f1"], t))
    base = sweep[best]

    def line(name, m):
        return (f"  {name:<14} precision {m['precision']:.2f}  recall {m['recall']:.2f}"
                f"  F1 {m['f1']:.2f}  exactly right {m['exact']:.0%}")

    print(f"\nSemantic model: {sc.status()['model']}  ({sc.status()['anchors']} profile anchors)")
    print(f"Best threshold for the blend: {best}  (ML floor {args.floor})\n")
    for group in ("brands", "verticals", "archetypes", "all"):
        print(group.upper())
        print(line("ML only", base[group]["ml"]))
        print(line("meaning only", base[group]["semantic"]))
        print(line("blend", base[group]["blend"]))
    print("\nBlend F1 (all labels) by threshold:")
    print("  " + "  ".join(f"{t:.2f}:{sweep[t]['all']['blend']['f1']:.2f}" for t in thresholds))

    gain = base["all"]["blend"]["f1"] - base["all"]["ml"]["f1"]
    print()
    if gain > 0.02:
        print(f"RECOMMENDATION: blend helps (+{gain:.2f} F1). Set in backend/.env:\n"
              f"  LOP_SEMANTIC_MODE=blend\n  LOP_SEMANTIC_MIN_SIMILARITY={best}")
    else:
        print(f"RECOMMENDATION: keep LOP_SEMANTIC_MODE=shadow (blend changes F1 by "
              f"{gain:+.2f}). Fill in the brand descriptions in "
              "app/ml_models/semantic_profiles.json and measure again.")
    if len(items) < 150:
        print(f"NOTE: {len(items)} labelled leads is a small sample; a few rows can "
              "swing these numbers. More reviewed leads make this more trustworthy.")

    print(f"\nWHERE THEY DISAGREE (first {args.examples})")
    shown = 0
    for item, ml, result in zip(items, ml_sets, results):
        sem = {label for g in sc.GROUPS for label, v in result.scores[g].items() if v >= best}
        mlp = set().union(*ml.values())
        if sem == mlp or shown >= args.examples:
            continue
        shown += 1
        gold = set().union(*item["gold"].values())
        print(f"- {item['payload']['title'][:100]}")
        print(f"    person : {', '.join(sorted(gold)) or '(none)'}")
        print(f"    ML     : {', '.join(sorted(mlp)) or '(none)'}")
        print(f"    meaning: {', '.join(sorted(sem)) or '(none)'}")

    out_dir = settings.log_dir.parent / "data" / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"semantic_eval_{date.today().isoformat()}.json"
    out.write_text(json.dumps({"rows": len(items), "best_threshold": best,
                               "ml_floor": args.floor, "model": sc.status()["model"],
                               "sweep": {str(t): v for t, v in sweep.items()}},
                              indent=2), encoding="utf-8")
    print(f"\nFull results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
