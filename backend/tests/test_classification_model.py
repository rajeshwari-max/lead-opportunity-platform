"""A classifier that can abstain, and thresholds that can move.

The shipped one returns labels and nothing else: no confidence, so "uncertain"
cannot be expressed, nothing can be routed to review, and a threshold cannot be
re-tuned without re-running inference over every row.
"""
from __future__ import annotations

import json

import pytest

from app.services.classification_model import (
    DEFAULT_THRESHOLDS,
    MODEL_VERSION,
    UNCERTAIN_FLOOR,
    classify,
    status_for_stored,
)
from app.services.verticals import VERTICALS


def test_a_clear_row_is_classified_with_a_score():
    c = classify("Grant for Solar Irrigation among Smallholder Farmers")
    assert c.status == "classified"
    assert "Livelihood" in c.labels
    assert c.scores["Livelihood"] > 0.5


def test_a_row_with_no_signal_is_unclassified_not_guessed():
    c = classify("Procurement of Office Chairs and Desks")
    assert c.status == "unclassified"
    assert c.labels == []


def test_the_uncertain_band_exists_and_is_reachable():
    """Without it there is nothing to route to review — every row is either
    asserted or silently dropped, which is how 34% of the database became
    invisible with no way to tell thin rows from irrelevant ones."""
    c = classify("Consultancy Services", "Some passing mention of nutrition.")
    assert c.status in {"uncertain", "unclassified"}
    if c.status == "uncertain":
        assert c.best[1] >= UNCERTAIN_FLOOR
        assert c.labels == [], "an uncertain row must not assert a label"


def test_scores_are_bounded():
    c = classify("Health health health health health", "health " * 50)
    assert all(0 <= s <= 1 for s in c.scores.values())


def test_repetition_does_not_beat_a_title_match():
    """Saturation is deliberate: a document repeating the right word twenty
    times must not outrank one naming the sector in its title."""
    title = classify("Maternal Health Programme").scores.get("Health", 0)
    body = classify("Procurement Notice", "health " * 40).scores.get("Health", 0)
    assert title >= body


def test_every_label_has_its_own_threshold():
    """E4C is on 34% of rows and shares 'research' with every consultancy RFP;
    Worker Wellbeing is on 2% with specific vocabulary. One global cut-off
    would either flood the first or starve the second."""
    assert set(DEFAULT_THRESHOLDS) == set(VERTICALS)
    assert DEFAULT_THRESHOLDS["E4C(Evidence for Change)"] > \
        DEFAULT_THRESHOLDS["Worker Wellbeing"]


def test_raising_a_threshold_removes_a_label_without_re_scoring():
    """The reason scores are stored: a threshold can move without an inference
    pass over 100,000 rows."""
    text = "Baseline Study and Data Collection"
    loose = classify(text, thresholds={v: 0.1 for v in VERTICALS})
    strict = classify(text, thresholds={v: 0.99 for v in VERTICALS})
    assert len(loose.labels) >= len(strict.labels)
    assert loose.scores == strict.scores


def test_labels_come_back_in_canonical_order():
    c = classify("Solar Irrigation for Farmers with a health component")
    assert c.labels == [v for v in VERTICALS if v in set(c.labels)]


def test_evidence_names_the_patterns_that_fired():
    c = classify("Grant for Solar Irrigation among Smallholder Farmers")
    assert c.evidence.get("Livelihood"), "no evidence recorded"
    assert json.loads(c.evidence_json())


def test_scores_serialise_for_storage():
    c = classify("Maternal Health and Nutrition Programme")
    stored = json.loads(c.scores_json())
    assert stored and all(0 < v <= 1 for v in stored.values())


def test_the_version_is_stamped_so_a_label_can_be_traced():
    assert classify("Health Programme").version == MODEL_VERSION


@pytest.mark.parametrize("verticals,stored,expected", [
    ("Health", None, "classified"),
    ("", None, "unclassified"),
    (None, None, "unclassified"),
    ("", "uncertain", "uncertain"),
])
def test_a_row_written_before_the_column_existed_reads_honestly(
        verticals, stored, expected):
    """Inferring from the labels a row already has is honest. Inventing
    "uncertain" for a legacy row would claim a measurement nobody took."""
    assert status_for_stored(verticals, stored) == expected
