# Classification evaluation

**Status: the harness is built and verified; the comparison has not been run on
real data, because there is no labelled data yet.**

That is the finding, not an excuse. This document says exactly what exists,
what a run would produce, and what has to happen first — so nobody reads a
number here and believes a model was chosen on evidence.

---

## Why there is no result yet

The brief asks for four model families compared against the rule baseline on
held-out data. Every one of them needs labels, and the labels cannot come from
the keyword rules — those are the thing being evaluated. A model trained on its
predecessor's output learns to imitate it, including its mistakes, and then
scores well against it.

Ground truth is what a person marked in the Unclassified section
(`verticals_source = 'human'`). At the time of writing that set is empty.

```
python scripts/gold_dataset.py --status
```

The threshold for a comparison that means anything is **300 examples with at
least 20 per label**. With six labels and a multi-label task, fewer cannot
separate four model families — any winner would be noise, and reporting one
would be worse than reporting nothing.

---

## What is built

| Piece | File | State |
|---|---|---|
| Scored classifier with abstention | `app/services/classification_model.py` | working |
| Per-label thresholds | same | defaults set from base rates, **not fitted** |
| Gold-set export / build / status | `scripts/gold_dataset.py` | working |
| Model comparison + metrics | `scripts/classifier_eval.py` | working |
| Threshold tuning | `scripts/classifier_eval.py --tune` | working |
| Labelling UI | Unclassified section | working |

### The baseline can now abstain

The shipped classifier returns a list of labels and nothing else. It has no
confidence, so "uncertain" cannot be expressed and nothing can be routed to
review. `classification_model.py` scores the same evidence into 0..1 and
produces three states:

```
classified     at least one label at or above its threshold
uncertain      best label in the review band (>= 0.30) — signal, not enough
unclassified   nothing came close
```

This matters for the comparison itself: beating a baseline that cannot abstain
proves nothing, because the baseline is forced to guess on every row.

### Thresholds are per label, and that is deliberate

Worker Wellbeing appears on 2% of rows with specific vocabulary. E4C appears on
34% and shares "research" and "evaluation" with every consultancy RFP on the
platform. One global cut-off would either flood E4C or starve Worker Wellbeing.

Current values are **starting points from the measured base rates, not a
calibration**. `--tune` fits them on the train split and prints replacements.

### The split rule

Time **and** source. One scraper produces near-duplicate rows — "Consultancy
for Baseline Survey, District A" and "…District B" — and a random split puts
one in train and its twin in test, reporting a score production will never
reproduce. The test set is the newest labelled slice, and near-duplicates are
removed on title+organisation before splitting.

---

## Metrics the harness reports

Per-label precision / recall / F1 / PR-AUC / support; micro and macro F1;
exact-match accuracy; Hamming loss; coverage versus abstention.

**Precision is weighted above recall** in the verdict and in `--tune` (F0.5). A
wrong vertical routes an opportunity to the wrong team; a missed one lands in
the Unclassified queue where a person sees it. Those costs are not symmetric.

**Adoption rule:** a model is adopted only if it clears the rule baseline by
**0.03 macro-F1** on data it never saw. Below that, the extra dependency, the
inference cost and the retraining discipline buy nothing measurable again next
month. The script prints `KEEP THE RULES` or `ADOPT <model>` and applies that
rule itself.

---

## Harness verification

Run against a synthetic 450-example set to prove the harness works end to end:

```
315 train / 135 test

rules       macro-F1 0.949   micro-F1 0.934   coverage 0.785
rules+cal   macro-F1 0.949   micro-F1 0.934   PR-AUC per label reported
tfidf       macro-F1 1.000   micro-F1 1.000
embed       SKIPPED — sentence-transformers not installed
transformer SKIPPED — transformers not installed
```

**These numbers are not evidence about any model.** The synthetic set is
generated from templates and is trivially separable — TF-IDF scoring 1.000 is a
property of the fixture, not of the method. The run proves the harness loads a
dataset, fits, predicts, computes every metric and applies the adoption rule.
Nothing more.

The two skipped models report *why* they were skipped rather than being
silently absent. A comparison with a missing row quietly favours whatever ran.

`embed` and `transformer` need packages that pull torch and download weights.
On a small EC2 box that is a deployment decision rather than a `pip install`,
and a fine-tuned encoder needs several thousand labelled examples to beat
TF-IDF on six labels — check `gold_dataset.py --status` before spending the
setup.

---

## What was measured, and did change the classifier

The rule baseline was audited on real data even without labels, and two faults
were found and fixed:

1. **Service-line terms were feeding a sector classifier.** The BD spreadsheet
   lists "Research", "Evaluation" and "Training & Capacity Building" under
   Health, because those are what the Health team searches for. Fed to a
   classifier answering "which sector is this", they tag everything.
   `\bResearch\b` alone was the sole reason for **114 of 738 Health tags**,
   including *"Market Research and Business Development Consultancy Services"*.

2. **Duplicate patterns were double-counting.** `\bEnergy\b` and `\benergy\b`
   both matched the same word, so one mention scored 2 and cleared a threshold
   of 2 on its own — the documented rule says a single body hit is too weak.
   Fifteen of eighteen probed sector phrases scored more than once inside a
   single vertical, which means the threshold has effectively been 1.

Neither needed a model. Both are the kind of thing a comparison would have
hidden: a learned model trained on those labels would have reproduced them.

---

## To produce a real result

```
1.  Label in the dashboard's Unclassified section — search, select, assign.
    Or bulk-export a stratified sample:
        python scripts/gold_dataset.py --export unlabelled.csv --n 400

2.  python scripts/gold_dataset.py --status        # 300+, 20+ per label?

3.  python scripts/gold_dataset.py --build gold.jsonl
4.  python scripts/classifier_eval.py gold.jsonl
5.  python scripts/classifier_eval.py gold.jsonl --tune

6.  Re-run on a second labelling round before deploying anything.
    One held-out win is a result; two is a reproducible one.
```

Include the hard cases deliberately: rows marked "none of the six" are the
negatives, and a set without them teaches a model that everything belongs
somewhere.
