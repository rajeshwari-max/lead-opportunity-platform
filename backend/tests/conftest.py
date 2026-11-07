"""Suite-wide test settings.

The semantic classifier (services/semantic_classifier.py) defaults to
"shadow" in production, which would make every test that classifies a lead
try to load — and on a fresh machine download — a 130 MB embedding model.
Tests that exercise it install a small fake embedder and switch the mode on
themselves (tests/test_semantic_classifier.py).

English-only intake (services/english_only.py) is off for the same reason: a
non-English fixture row would otherwise trigger an English-edition lookup over
the network. tests/test_english_only.py switches it on.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _semantic_layer_off_unless_a_test_asks(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "semantic_mode", "off")
    monkeypatch.setattr(settings, "english_only", False)
    yield
