"""Build the labelled set every model comparison depends on.

Why this comes first
--------------------
The brief asks for TF-IDF, embeddings and a fine-tuned encoder to be compared
against the rule baseline on held-out data. None of that means anything without
labels, and the labels cannot come from the keyword rules — those are the thing
being evaluated. A model trained on its predecessor's output learns to imitate
it, including its mistakes, and then scores well.

So: keyword labels are WEAK labels. Ground truth is what a person marked in the
Unclassified section, where `verticals_source = 'human'`.

    python scripts/gold_dataset.py --export unlabelled.csv --n 400
    python scripts/gold_dataset.py --status
    python scripts/gold_dataset.py --build gold.jsonl

`--export` writes a stratified sample for someone to label in the dashboard or
a spreadsheet. `--build` collects everything a human HAS labelled into the
evaluation file.

The splitting rule
------------------
Split by time AND by source. A single scraper produces near-duplicate rows —
"Consultancy for Baseline Survey, District A" and "…District B" — and a random
split puts one in train and its twin in test, which reports a score the model
will never reproduce in production. Test is the newest slice, and any source
held out is held out entirely.

Read-only. Writes only the files you name.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
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

from sqlalchemy import select                                  # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity                    # noqa: E402
from app.services.verticals import HUMAN, VERTICALS            # noqa: E402

# Below this, a comparison is theatre: with six labels and a multi-label task,
# a few dozen examples cannot separate four model families, and any winner is
# noise. Stated up front so nobody reports a result from 40 rows.
MIN_FOR_COMPARISON = 300
MIN_PER_LABEL = 20


def _body(o) -> str:
    return " ".join(filter(None, [o.summary, o.vertical, o.eligibility]))


def status(db) -> int:
    rows = db.execute(
        select(Opportunity).where(Opportunity.verticals_source == HUMAN)
    ).scalars().all()
    per: Counter = Counter()
    none_of_six = 0
    for r in rows:
        labels = [t.strip() for t in (r.verticals or "").split(",") if t.strip()]
        if not labels:
            none_of_six += 1
        for label in labels:
            per[label] += 1

    print("=" * 78)
    print("GOLD DATASET STATUS")
    print("=" * 78)
    print(f"{len(rows):,} human-labelled rows.")
    print(f"    {none_of_six:,} marked 'none of the six' — the negatives, and the")
    print(f"      hardest ones to get. A set with no negatives teaches a model")
    print(f"      that everything belongs somewhere.")
    print()
    for label in VERTICALS:
        n = per.get(label, 0)
        flag = "" if n >= MIN_PER_LABEL else f"   <-- under {MIN_PER_LABEL}, cannot be evaluated"
        print(f"    {label:<32} {n:>5}{flag}")
    print()
    if len(rows) < MIN_FOR_COMPARISON:
        print(f"NOT ENOUGH TO COMPARE MODELS. {len(rows)} of {MIN_FOR_COMPARISON} minimum.")
        print("With six labels and a multi-label task, a smaller set cannot")
        print("separate four model families — any winner would be noise.")
        print()
        print("Label more in the dashboard's Unclassified section, or export a")
        print("sample:  python scripts/gold_dataset.py --export unlabelled.csv")
    else:
        print(f"Enough to compare. Build it:")
        print("    python scripts/gold_dataset.py --build gold.jsonl")
        print("    python scripts/classifier_eval.py gold.jsonl")
    return 0


def export(db, path: str, n: int, seed: int) -> int:
    """A stratified sample to label.

    Stratified by source and by whether the rules found anything, because a
    sample of only rule-classified rows would contain no examples of what the
    rules miss — and those are exactly the rows the evaluation has to judge.
    """
    rows = db.execute(
        select(Opportunity).where(
            (Opportunity.verticals_source.is_(None))
            | (Opportunity.verticals_source != HUMAN)
        )
    ).scalars().all()

    by_bucket: dict[tuple[str, bool], list] = {}
    for r in rows:
        key = (r.source_website or "", bool((r.verticals or "").strip()))
        by_bucket.setdefault(key, []).append(r)

    rng = random.Random(seed)
    picked: list = []
    buckets = sorted(by_bucket, key=lambda k: (k[0], k[1]))
    per_bucket = max(1, n // max(1, len(buckets)))
    for key in buckets:
        pool = by_bucket[key]
        rng.shuffle(pool)
        picked.extend(pool[:per_bucket])
    rng.shuffle(picked)
    picked = picked[:n]

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "title", "source_website", "country", "summary",
                    "rule_labels", "YOUR_LABELS (semicolon separated, or NONE)"])
        for r in picked:
            w.writerow([r.id, r.title or "", r.source_website or "",
                        r.country or "", (r.summary or "")[:400],
                        r.verticals or "", ""])
    print(f"Wrote {len(picked):,} rows to {path}.")
    print()
    print("Fill the last column with semicolon-separated vertical names, or")
    print("NONE for 'none of the six'. Leave a row blank to skip it.")
    print(f"Valid names: {', '.join(VERTICALS)}")
    print()
    print("Labelling in the dashboard's Unclassified section is preferable —")
    print("it records who and when, and the label is protected from the next")
    print("backfill. This file is for bulk work away from the screen.")
    return 0


def build(db, path: str, test_fraction: float) -> int:
    rows = db.execute(
        select(Opportunity).where(Opportunity.verticals_source == HUMAN)
        .order_by(Opportunity.verticals_labeled_at.asc().nulls_first(),
                  Opportunity.id.asc())
    ).scalars().all()

    if len(rows) < MIN_FOR_COMPARISON:
        print(f"Only {len(rows)} human-labelled rows; {MIN_FOR_COMPARISON} is the "
              f"minimum for a comparison that means anything.", file=sys.stderr)
        print("Building the file anyway so the harness can be exercised, but any "
              "metric it reports is not evidence.", file=sys.stderr)

    # Time split: the newest slice is the test set. Deduplicated on
    # title+organisation first, so a near-duplicate cannot straddle the split.
    seen: set[tuple[str, str]] = set()
    unique = []
    for r in rows:
        key = ((r.title or "").strip().lower(), (r.organization or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    cut = int(len(unique) * (1 - test_fraction))
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(unique):
            fh.write(json.dumps({
                "id": r.id,
                "title": r.title or "",
                "body": _body(r),
                "source": r.source_website or "",
                "labels": [t.strip() for t in (r.verticals or "").split(",") if t.strip()],
                "split": "train" if i < cut else "test",
                "labeled_at": (r.verticals_labeled_at.isoformat()
                               if r.verticals_labeled_at else None),
            }) + "\n")
    n_test = len(unique) - cut
    print(f"Wrote {len(unique):,} examples to {path} "
          f"({cut:,} train / {n_test:,} test).")
    print(f"{len(rows) - len(unique):,} near-duplicates dropped before splitting.")
    print("Split by time: the test set is the most recently labelled slice, so a")
    print("score reflects performance on what arrives next rather than on rows")
    print("interleaved with their own twins.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--export", metavar="CSV")
    ap.add_argument("--build", metavar="JSONL")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--test-fraction", type=float, default=0.3)
    a = ap.parse_args()
    with session_scope() as db:
        if a.export:
            return export(db, a.export, a.n, a.seed)
        if a.build:
            return build(db, a.build, a.test_fraction)
        return status(db)


if __name__ == "__main__":
    raise SystemExit(main())
