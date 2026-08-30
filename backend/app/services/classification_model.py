"""Vertical classification that can say "I am not sure".

What was wrong with the old one
-------------------------------
`classify_verticals` returns a list of labels and nothing else. It scores on a
fixed threshold of 2 and has no notion of confidence, so:

* a label backed by one weak body hit is indistinguishable from one backed by
  a title match and three corroborating terms;
* "uncertain" cannot be expressed, so nothing can be routed to review;
* a threshold cannot be re-tuned without re-running the classifier over every
  row, because no probability was ever stored.

That is why 34% of rows carry no vertical and nobody can tell which of them are
genuinely outside the six and which are just thin.

What this adds
--------------
The same evidence, turned into a per-label score in 0..1, plus a status:

    classified     at least one label at or above its threshold
    uncertain      the best label is in the review band — real signal, not
                   enough of it
    unclassified   nothing came close

Thresholds are PER LABEL and configurable, because the labels are not equally
separable. Worker Wellbeing appears on 2% of rows and its vocabulary is
specific; E4C appears on 34% and shares "research" and "evaluation" with every
consultancy RFP on the platform. One global cut-off would either flood E4C or
starve Worker Wellbeing.

Honest about what this is
-------------------------
This is a calibrated *rule* model, not a learned one. The score is a bounded
transform of the same keyword evidence, so it inherits that evidence's blind
spots — it will be confident about a document that uses the right words for the
wrong reason. It exists to make abstention and threshold-tuning possible, and
to be the baseline every learned model in `scripts/classifier_eval.py` has to
beat on held-out data. Beating a baseline that cannot abstain proves nothing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.services.verticals import (
    VERTICALS,
    _BODY_WEIGHT,
    _TITLE_WEIGHT,
    explain_verticals,
)

# Bump when the rules or the scoring change. Stored on every row so a later
# audit can tell which version produced a label, and so a re-classification
# can target only the rows a stale version touched.
MODEL_VERSION = "rules-2026.08.30"

# Score at or above which a label is asserted. Per label, because the labels
# are not equally separable — see the module docstring.
#
# These are STARTING points chosen from the measured base rates, not fitted
# values. `scripts/classifier_eval.py --tune` fits them against labelled data
# and prints the replacements; until someone runs it on a real gold set, they
# are a defensible default rather than a calibration.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "Livelihood": 0.55,
    "Health": 0.55,
    "E4C(Evidence for Change)": 0.70,     # 34% base rate; needs more evidence
    "Climate/Sustainability(ESG)": 0.55,
    "Worker Wellbeing": 0.45,             # 2% base rate; specific vocabulary
    "Innovative Finance": 0.50,
}

# Below the threshold but above this, the row is UNCERTAIN rather than
# unclassified: there is real signal and not enough of it. That band is what
# the review queue is for, and it is the difference between "we looked and
# found nothing" and "we looked and could not decide".
UNCERTAIN_FLOOR = 0.30

# A title hit plus two corroborating body hits. Scores saturate here so a
# document that repeats the right words twenty times does not outrank one that
# names the sector in its title.
_SATURATION = float(_TITLE_WEIGHT + 2 * _BODY_WEIGHT)


@dataclass
class Classification:
    labels: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    status: str = "unclassified"          # classified | uncertain | unclassified
    version: str = MODEL_VERSION

    @property
    def best(self) -> tuple[str, float]:
        if not self.scores:
            return ("", 0.0)
        label = max(self.scores, key=lambda k: self.scores[k])
        return (label, self.scores[label])

    def scores_json(self) -> str:
        return json.dumps({k: round(v, 3) for k, v in self.scores.items()
                           if v > 0.0}, sort_keys=True)

    def evidence_json(self) -> str:
        """The patterns that fired, for the labels that scored. Truncated: this
        is shown to a reviewer deciding one row, not archived for analysis."""
        trimmed = {k: v[:6] for k, v in self.evidence.items() if v}
        return json.dumps(trimmed, sort_keys=True)


def _raw_score(vertical: str, title: str, body: str) -> tuple[float, list[str]]:
    """Weighted evidence for one label, and the patterns that produced it."""
    why = explain_verticals(title, body).get(vertical)
    if why is None:
        # Below the assignment threshold in the rules. Re-score the raw hits so
        # a near-miss still gets a number — that is the whole point of the
        # uncertain band, and a label that never scores below its cut-off can
        # never be reviewed.
        from app.services.verticals import _COMPILED

        hits, patterns = 0.0, []
        for pat in _COMPILED[vertical]:
            fired = False
            if title and pat.search(title):
                hits += _TITLE_WEIGHT
                fired = True
            if body and pat.search(body):
                hits += _BODY_WEIGHT
                fired = True
            if fired:
                patterns.append(pat.pattern)
        return hits, patterns
    # Assigned by the rules: recompute the same way for a comparable number.
    from app.services.verticals import _COMPILED

    hits = 0.0
    for pat in _COMPILED[vertical]:
        if title and pat.search(title):
            hits += _TITLE_WEIGHT
        if body and pat.search(body):
            hits += _BODY_WEIGHT
    return hits, why


def classify(title: str, body: str = "",
             thresholds: dict[str, float] | None = None) -> Classification:
    """Score every label, then decide."""
    cuts = thresholds or DEFAULT_THRESHOLDS
    out = Classification()

    for vertical in VERTICALS:
        raw, patterns = _raw_score(vertical, title or "", body or "")
        if raw <= 0:
            continue
        # Bounded, monotonic, and saturating. Not a probability — it is not
        # fitted to anything — so it is called a score everywhere it appears.
        score = min(raw / _SATURATION, 1.0)
        out.scores[vertical] = score
        if patterns:
            out.evidence[vertical] = patterns

    out.labels = [v for v, s in out.scores.items()
                  if s >= cuts.get(v, 0.55)]
    if out.labels:
        # Canonical ordering, so a stored value is comparable between runs.
        out.labels = [v for v in VERTICALS if v in set(out.labels)]
        out.status = "classified"
    elif out.best[1] >= UNCERTAIN_FLOOR:
        out.status = "uncertain"
    else:
        out.status = "unclassified"
    return out


def status_for_stored(verticals: str | None, stored_status: str | None) -> str:
    """What a row's classification status is, including rows written before
    the column existed.

    A row with labels is classified; one without is unclassified. Inferring
    that is honest — it is what the labels already say — whereas inventing
    "uncertain" for a legacy row would claim a measurement nobody took.
    """
    if stored_status:
        return stored_status
    return "classified" if (verticals or "").strip() else "unclassified"
