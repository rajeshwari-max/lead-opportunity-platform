"""English-only intake: switch to the source's English edition, never translate."""
from __future__ import annotations

import pytest

from app.schemas.opportunity import RawOpportunity
from app.services import english_only as eo


@pytest.fixture(autouse=True)
def _english_only_on(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "english_only", True)


def _raw(title, url="https://example.org/fr/appels/123", summary=""):
    return RawOpportunity(title=title, summary=summary, opportunity_url=url,
                          source_website="Example")


@pytest.mark.parametrize("title", [
    "2027 Prince Albert Fund",
    "Consultancy RFP",
    "Digitization And Geo Referencing Of Existing Land Use Maps",
    "Short Term Conference and Travel Scholarships",
    "Call for proposals: climate resilience grants for NGOs in India",
    "Fondation Mérieux small grants for laboratories",
])
def test_english_titles_stay_english(title):
    assert eo.detect(title)[0]


@pytest.mark.parametrize("title,summary", [
    ("Appel à projets pour les associations locales", "Date limite de dépôt des candidatures"),
    ("Convocatoria de subvenciones para organizaciones de la sociedad civil", ""),
    ("Конкурс малых грантов для НКО", ""),
])
def test_other_languages_are_detected(title, summary):
    assert not eo.detect(title, summary)[0]


def test_english_candidates_switch_the_language_selection():
    assert "https://example.org/en/appels/123" in eo.english_candidates(
        "https://example.org/fr/appels/123")
    assert "https://example.org/call?id=4&lang=en" in eo.english_candidates(
        "https://example.org/call?id=4&lang=fr")
    assert "https://www.example.org/x" in eo.english_candidates("https://fr.example.org/x")
    assert eo.english_candidates("https://example.org/calls/123") == []


def test_hreflang_and_heading_are_read():
    html = ('<link rel="alternate" hreflang="en" href="/en/call/9">'
            '<meta property="og:title" content="Call for local associations">'
            '<meta name="description" content="Grants for community groups">')
    assert eo.hreflang_english(html, "https://x.org/fr/call/9") == "https://x.org/en/call/9"
    assert eo.read_heading(html) == ("Call for local associations",
                                     "Grants for community groups")


def test_english_rows_need_no_lookup(monkeypatch):
    monkeypatch.setattr(eo, "_fetch_english", lambda *a: pytest.fail("network used"))
    monkeypatch.setattr(eo, "_stored_english", lambda *a: pytest.fail("db used"))
    v = eo.ensure_english(_raw("Request for proposals: baseline survey in Odisha"))
    assert v.keep and not v.switched


def test_non_english_row_is_swapped_for_its_english_edition(monkeypatch):
    eo._cache.clear(); eo.reset_run_budget()
    monkeypatch.setattr(eo, "_stored_english", lambda *a: None)
    monkeypatch.setattr(eo, "_fetch_english", lambda url, t: (
        "Call for projects for local associations", "Grants for associations",
        "https://example.org/en/appels/123"))
    v = eo.ensure_english(_raw("Appel à projets pour les associations locales",
                               summary="Date limite de dépôt des candidatures"))
    assert v.keep and v.switched
    assert v.raw.title == "Call for projects for local associations"
    assert v.raw.opportunity_url == "https://example.org/en/appels/123"
    assert v.raw.summary == "Grants for associations"


def test_stored_english_edition_is_reused_without_network(monkeypatch):
    monkeypatch.setattr(eo, "_stored_english", lambda urls, src: (
        "Call for projects", "Stored summary", "https://example.org/en/appels/123"))
    monkeypatch.setattr(eo, "_fetch_english", lambda *a: pytest.fail("network used"))
    v = eo.ensure_english(_raw("Appel à projets pour les associations locales"))
    assert v.keep and v.raw.title == "Call for projects"


def test_only_non_english_leads_are_not_stored_or_translated(monkeypatch):
    eo._cache.clear(); eo.reset_run_budget()
    monkeypatch.setattr(eo, "_stored_english", lambda *a: None)
    monkeypatch.setattr(eo, "_fetch_english", lambda *a: None)
    v = eo.ensure_english(_raw("Convocatoria de subvenciones para organizaciones de la sociedad civil"))
    assert not v.keep and "no English version" in v.reason


def test_closed_non_english_rows_are_never_looked_up(monkeypatch):
    monkeypatch.setattr(eo, "_fetch_english", lambda *a: pytest.fail("network used"))
    monkeypatch.setattr(eo, "_stored_english", lambda *a: pytest.fail("db used"))
    v = eo.ensure_english(_raw("Appel à projets pour les associations locales"), expired=True)
    assert not v.keep


def test_lookups_are_capped_per_run(monkeypatch):
    from app.core.config import settings
    eo._cache.clear(); eo.reset_run_budget()
    calls = []
    monkeypatch.setattr(settings, "english_lookup_max_per_run", 2)
    monkeypatch.setattr(eo, "_stored_english", lambda *a: None)
    monkeypatch.setattr(eo, "_fetch_english", lambda url, t: calls.append(url))
    for i in range(5):
        eo.ensure_english(_raw("Appel à projets pour les associations locales",
                               url=f"https://example.org/fr/appels/{i}"))
    assert len(calls) == 2


def test_switch_can_be_turned_off(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "english_only", False)
    assert eo.ensure_english(_raw("Appel à projets pour les associations locales")).keep
