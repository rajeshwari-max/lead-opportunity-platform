"""The meaning-based layer may add judgment; it may never take the ML away.

What these tests pin:
  * shadow mode records the semantic opinion and changes NO label;
  * blend mode keeps every ML label and adds the ones meaning is confident
    about, with the brand > archetype > vertical hierarchy still enforced;
  * the floor and veto settings do what their names say;
  * a missing or broken embedding model degrades to ML-only, silently for the
    scrape and loudly in the log;
  * the meaning profiles really are the brand teams' inventories plus the
    editable descriptions.

A real embedding model is not loaded here. `_ConceptEmbedder` maps words to a
handful of concept axes, so "tea growers" and "smallholder farmers" land on
the same axis without sharing a word — the property the real model provides,
in a form a test can reason about.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.config import settings
from app.services import ml_hierarchy, semantic_classifier as sc

_AXES = {
    "livelihood": ("farmer", "farmers", "smallholder", "growers", "agriculture",
                   "livelihood", "livelihoods", "rural", "incomes", "tea"),
    "health": ("health", "clinic", "maternal", "nutrition", "disease"),
    "climate": ("climate", "renewable", "biodiversity", "adaptation", "carbon"),
    "workers": ("worker", "workers", "factory", "garment", "workplace", "wages"),
    "finance": ("finance", "investment", "impact", "blended", "bond"),
    "protection": ("protection", "welfare", "entitlement", "schemes", "security"),
    "building": ("hospital", "construction", "civil", "works", "infrastructure"),
}


class _ConceptEmbedder:
    name = "test/concepts"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            words = [w.strip(".,:;()-").casefold() for w in text.split()]
            vec = [sum(w in vocab for w in words) for vocab in _AXES.values()]
            vec.append(0.15)          # a small shared component, like real models
            out.append(vec)
        return np.asarray(out, dtype="float32")


class _BrokenEmbedder:
    name = "test/broken"

    def embed(self, texts):
        raise RuntimeError("model file missing")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    data = {
        "brands": {"Vrutti": {"description": "farmer livelihoods and rural incomes",
                              "phrases": []}},
        "archetypes": {},
        "verticals": {
            "Livelihood": {"description": "smallholder agriculture and rural livelihoods"},
            "Health": {"description": "maternal health and nutrition",
                       "exclude": ["hospital construction civil works"]},
            "Climate/Sustainability(ESG)": {"description": "climate adaptation and renewable"},
            "Worker Wellbeing": {"description": "factory workers and workplace wages"},
        },
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(settings, "semantic_profiles_path", str(path))
    return path


@pytest.fixture
def concepts(profiles, monkeypatch):
    embedder = _ConceptEmbedder()
    sc.set_embedder(embedder)
    monkeypatch.setattr(settings, "semantic_min_similarity", 0.8)
    monkeypatch.setattr(settings, "semantic_ml_floor", 0.0)
    monkeypatch.setattr(settings, "semantic_veto_below", 0.0)
    yield embedder
    sc.set_embedder(None)


TEA = ("Call for proposals: raising incomes of tea growers",
       "Support for smallholder tea growers to improve farm incomes in Assam.")


def _ml_only(title, summary):
    return ml_hierarchy.classify_hierarchy(title, summary)


# ----------------------------------------------------------------- profiles

def test_profiles_carry_the_brand_teams_inventories_and_the_descriptions(profiles):
    built = sc.build_profiles()
    setu = built[("brands", "Setu")]
    assert any(anchor.startswith("Setu - Last-Mile Access:") for anchor in setu)
    assert built[("brands", "Vrutti")][0] == "Vrutti: farmer livelihoods and rural incomes"
    assert built[("verticals", "Health")] == ["Health: maternal health and nutrition"]


def test_the_shipped_profiles_file_is_valid_and_covers_every_label():
    data = json.loads(sc.DEFAULT_PROFILES.read_text(encoding="utf-8"))
    for group, labels in sc.LABELS.items():
        assert set(labels) <= set(data[group]), group


# ----------------------------------------------------------------- off/shadow

def test_off_mode_never_touches_the_model(concepts, monkeypatch):
    monkeypatch.setattr(settings, "semantic_mode", "off")
    result = _ml_only(*TEA)
    assert result.semantic == {}
    assert concepts.calls == 0


def test_shadow_mode_records_the_opinion_and_changes_no_label(concepts, monkeypatch):
    baseline = _ml_only(*TEA)
    monkeypatch.setattr(settings, "semantic_mode", "shadow")
    shadow = _ml_only(*TEA)
    assert (shadow.brands, shadow.archetypes, shadow.verticals, shadow.status,
            shadow.version, shadow.source) == (
        baseline.brands, baseline.archetypes, baseline.verticals, baseline.status,
        baseline.version, baseline.source)
    assert "Livelihood" in shadow.semantic["suggested"]["verticals"]
    assert "Vrutti" in shadow.semantic["suggested"]["brands"]
    assert json.loads(shadow.evidence_json())["semantic"]["suggested"]


# ------------------------------------------------------------------- blend

def test_blend_keeps_ml_labels_and_adds_the_meaning(concepts, monkeypatch):
    baseline = _ml_only(*TEA)
    monkeypatch.setattr(settings, "semantic_mode", "blend")
    blended = _ml_only(*TEA)
    assert set(baseline.brands) <= set(blended.brands)
    assert set(baseline.verticals) <= set(blended.verticals)
    assert "Livelihood" in blended.verticals and "Vrutti" in blended.brands
    # hierarchy still holds: a vertical implies Devsol and CMS
    assert "Devsol" in blended.archetypes and "CMS" in blended.brands
    assert blended.status == "classified"
    assert blended.source.endswith("+semantic")
    assert blended.version.startswith(baseline.version + "+semantic-concepts-")


def test_the_ml_floor_stops_meaning_alone_from_adding_a_label(concepts, monkeypatch):
    monkeypatch.setattr(settings, "semantic_mode", "blend")
    monkeypatch.setattr(settings, "semantic_ml_floor", 1.01)   # nothing can clear it
    baseline_mode = _ml_only(*TEA)
    assert baseline_mode.semantic["added"] == []


def test_the_veto_drops_an_ml_label_that_means_nothing_like_the_lead(concepts, monkeypatch):
    lead = ("Grant for factory workers wages and workplace safety",
            "Garment factory workers in Bangladesh.")
    assert "Worker Wellbeing" in _ml_only(*lead).verticals
    monkeypatch.setattr(settings, "semantic_mode", "blend")
    # An absurdly strict veto, so the ML's (correct) label is dropped — the
    # point is only that the setting works, and that it is off by default.
    monkeypatch.setattr(settings, "semantic_veto_below", 1.01)
    result = _ml_only(*lead)
    assert "Worker Wellbeing" in result.semantic["dropped"]
    assert "Worker Wellbeing" not in result.verticals


def test_the_veto_is_off_by_default():
    assert settings.semantic_veto_below == 0.0


def test_batch_and_live_agree_in_blend_mode(concepts, monkeypatch):
    monkeypatch.setattr(settings, "semantic_mode", "blend")
    live = _ml_only(*TEA)
    batch = ml_hierarchy.classify_hierarchy_batch([{
        "title": TEA[0], "summary": TEA[1], "eligibility": "", "organization": "",
        "location": "", "country": "", "region": "", "source": "",
        "category_hint": None}])[0]
    assert (batch.brands, batch.archetypes, batch.verticals, batch.version) == (
        live.brands, live.archetypes, live.verticals, live.version)


def test_backfill_version_includes_the_semantic_profile_only_when_blending(concepts, monkeypatch):
    ml_only = str(ml_hierarchy.model_status()["version"])
    monkeypatch.setattr(settings, "semantic_mode", "shadow")
    assert ml_hierarchy.current_version() == ml_only
    monkeypatch.setattr(settings, "semantic_mode", "blend")
    assert ml_hierarchy.current_version().startswith(ml_only + "+semantic-")


# ------------------------------------------------------------- degradation

def test_a_broken_model_degrades_to_ml_only(profiles, monkeypatch, caplog):
    baseline = _ml_only(*TEA)
    sc.set_embedder(_BrokenEmbedder())
    try:
        monkeypatch.setattr(settings, "semantic_mode", "blend")
        result = _ml_only(*TEA)
        assert (result.brands, result.verticals, result.version) == (
            baseline.brands, baseline.verticals, baseline.version)
        assert result.semantic == {}
        assert "model file missing" in sc.status()["error"]
        assert "ML-only classification continues" in caplog.text
    finally:
        sc.set_embedder(None)


def test_lead_text_reads_what_the_call_is_for():
    text = sc.lead_text("  Grant  for\nfarmers ", "Summary here", "NGOs in India")
    assert text == "Grant for farmers. Summary here. NGOs in India"


# ------------------------------------------------------- evaluation script

def test_eval_metrics_count_precision_recall_and_exact_match():
    from scripts.semantic_eval import _metrics

    m = _metrics([({"Health"}, {"Health"}),
                  ({"Health", "Livelihood"}, {"Livelihood"}),
                  (set(), {"Worker Wellbeing"})])
    assert m == {"precision": 0.667, "recall": 0.667, "f1": 0.667, "exact": 0.333}


def test_eval_blend_is_the_union_the_dashboard_would_show():
    from scripts.semantic_eval import _evaluate

    items = [{"gold": {"brands": {"Vrutti"}, "archetypes": set(),
                       "verticals": {"Livelihood"}}}]
    ml = [{"brands": set(), "archetypes": set(), "verticals": set()}]
    scores = [{"brands": {"Vrutti": 0.8}, "archetypes": {},
               "verticals": {"Livelihood": 0.81, "Health": 0.3}}]
    report = _evaluate(items, ml, scores, 0.75, 0.0, [{}])
    assert report["all"]["ml"]["recall"] == 0.0
    assert report["all"]["blend"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "exact": 1.0}


def test_a_lead_closer_to_an_exclusion_than_to_the_profile_loses_the_label(concepts,
                                                                           monkeypatch):
    monkeypatch.setattr(settings, "semantic_min_similarity", 0.4)
    clinic, works = sc.score_texts([
        "Maternal health and nutrition clinic programme",
        "Hospital construction civil works tender for a maternal clinic",
    ])
    assert "Health" in clinic.selected["verticals"]
    assert "Health" not in works.selected["verticals"]
    assert works.excluded["Health"] == "hospital construction civil works"
    assert "excluded_by" in works.as_evidence()


def test_the_shipped_brand_profiles_come_from_the_brand_forms():
    data = json.loads(sc.DEFAULT_PROFILES.read_text(encoding="utf-8"))
    for brand in ("Swasti", "Vrutti", "Upfront", "Green Foundation"):
        entry = data["brands"][brand]
        assert entry["description"] and entry["phrases"] and entry["exclude"], brand
