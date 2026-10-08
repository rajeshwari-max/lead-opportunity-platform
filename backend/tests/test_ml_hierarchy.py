from app.services import ml_hierarchy


def test_versioned_model_loads_with_review_provenance():
    ml_hierarchy.reset_model_cache()
    status = ml_hierarchy.model_status()
    assert status["mode"] == "ml"
    assert status["loaded"] is True
    assert status["training_rows"] == 5360
    assert status["gold_rows"] == 98
    assert status["unresolved_rows_excluded"] == 2


def test_batch_and_live_inference_preserve_hierarchy():
    sample = {
        "title": "Grant for rural livelihoods and farmer producer organisations in India",
        "summary": "Funding for market access, smallholder agriculture and women farmers.",
        "eligibility": "Indian nonprofit organisations",
        "organization": "Example Foundation",
        "location": "India",
        "country": "India",
        "region": "South Asia",
        "source": "Test",
        "category_hint": None,
    }
    live = ml_hierarchy.classify_hierarchy(
        sample["title"], sample["summary"], sample["eligibility"],
        sample["organization"], sample["location"], sample["country"],
        sample["region"], sample["source"], sample["category_hint"],
    )
    batch = ml_hierarchy.classify_hierarchy_batch([sample])[0]
    assert batch.category == live.category
    assert batch.brands == live.brands
    assert batch.archetypes == live.archetypes
    assert batch.verticals == live.verticals
    if batch.verticals:
        assert "Devsol" in batch.archetypes
        assert "CMS" in batch.brands
    if batch.archetypes:
        assert "CMS" in batch.brands


def test_disabled_model_falls_back_without_interrupting_classification(monkeypatch):
    monkeypatch.setattr(ml_hierarchy.settings, "ml_classifier_enabled", False)
    ml_hierarchy.reset_model_cache()
    result = ml_hierarchy.classify_hierarchy(
        "Request for proposals for public health research",
        "Community health evidence and evaluation in India",
    )
    assert result.source == "rule"
    assert result.category.value == "RFP"
    assert "Health" in result.verticals
    ml_hierarchy.reset_model_cache()
