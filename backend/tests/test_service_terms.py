"""A sector classifier must not be fed service-line vocabulary.

Measured on 4,000 recent rows before this change:

    Health    \\bResearch\\b   sole reason for 114 of 738 Health tags
    Health    \\bEvaluation\\b sole reason for  31
    Livelihood \\bEnergy\\b    sole reason for  45

"IEAC Audience Research — Western Balkans 2026" was filed under **Health** on
the word "Research" alone. Each test below names a real row from that audit.
"""
from __future__ import annotations

import pytest

from app.services.keyword_inventory import VERTICAL_KEYWORDS as SHEET
from app.services.service_terms import (
    SERVICE_LINE_TERMS,
    dedupe_patterns,
    is_service_line,
    owned_elsewhere,
    pattern_is_service_line,
)
from app.services.verticals import (
    VERTICAL_E4C,
    VERTICALS,
    _VERTICAL_KEYWORDS,
    classify_verticals,
)


# ------------------------------------------------- the rows the audit named

def test_a_market_research_tender_is_no_longer_health():
    """Filed under Health on the word 'Research' alone."""
    got = classify_verticals("Market Research and Business Development "
                             "Consultancy Services")
    assert "Health" not in got


def test_an_audience_research_tender_is_no_longer_health():
    got = classify_verticals("IEAC Audience Research - Western Balkans 2026")
    assert "Health" not in got
    assert VERTICAL_E4C in got, "it is still research work, and E4C owns that"


def test_a_spectrometer_supply_notice_is_no_longer_livelihood():
    """`\\bEnergy\\b` was the sole reason for 45 Livelihood tags, including
    this. Energy is Climate's concept, and Climate already matches it."""
    got = classify_verticals("Supply of Energy-Dispersive X-ray Fluorescence "
                             "Spectrometer")
    assert "Livelihood" not in got


def test_event_management_services_are_no_longer_livelihood():
    got = classify_verticals("On-Site Event Management Services for "
                             "South-South Triangular Cooperation")
    assert "Livelihood" not in got


# ------------------------------------- and the true positives still classify

@pytest.mark.parametrize("title,expected", [
    ("Assam State Secondary Healthcare Initiative", "Health"),
    ("Grant for Solar Irrigation among Smallholder Farmers", "Livelihood"),
    ("Endline Evaluation of a Maternal Nutrition Programme", "Health"),
    ("Strengthening Social Protection Delivery System in Sindh", "Worker Wellbeing"),
    ("Consultancy for Baseline Survey in Bihar", VERTICAL_E4C),
    ("Climate Resilience and Adaptation Programme", "Climate/Sustainability(ESG)"),
])
def test_the_pruning_does_not_work_by_classifying_less(title, expected):
    """The failure mode of any precision fix: recall collapses and nobody
    notices because the noise went away too."""
    assert expected in classify_verticals(title), title


# ------------------------------------------------------ the E4C exemption

def test_e4c_keeps_its_own_service_line_vocabulary():
    """E4C(Evidence for Change) is 'Research and Community Engagement'. For
    that vertical research IS the sector, and stripping these would gut the one
    vertical they legitimately define."""
    for term in ("research", "evaluation", "consultancy"):
        assert not is_service_line(term, VERTICAL_E4C), term


@pytest.mark.parametrize("vertical", [v for v in VERTICALS if v != VERTICAL_E4C])
def test_every_other_vertical_loses_them(vertical):
    for term in ("research", "evaluation"):
        assert is_service_line(term, vertical), (vertical, term)


def test_a_research_tender_still_reaches_e4c():
    assert VERTICAL_E4C in classify_verticals(
        "Research and Innovation Programme Evaluation")


# ----------------------------------------- a term another vertical owns

def test_the_sheets_livelihood_row_is_deduplicated_against_other_verticals():
    """The comment on the Livelihood keyword block says this was already done.
    It was done to the hand-written list; the merge put every one back."""
    owned = {t: owned_elsewhere(t, "Livelihood", _VERTICAL_KEYWORDS)
             for t in SHEET.get("Livelihood", [])}
    assert owned.get("Energy") == "Climate/Sustainability(ESG)"
    assert owned.get("Education") == VERTICAL_E4C
    assert owned.get("Sanitation & Hygiene") == "Health"
    assert owned.get("HR & Employment") == "Worker Wellbeing"


def test_a_genuinely_livelihood_term_is_not_taken_away():
    """The rule must only drop terms another vertical already covers. If it
    dropped these, the concept would be lost entirely rather than moved."""
    for term in ("Agriculture & Rural Development", "Fisheries & Aquaculture",
                 "Food Systems & Livelihoods"):
        assert owned_elsewhere(term, "Livelihood", _VERTICAL_KEYWORDS) == "", term


def test_dropping_a_term_never_loses_the_concept_entirely():
    """The safety property of the whole change: every term removed from a
    vertical is still matched by the vertical that owns it, so the ROW still
    gets tagged — just correctly."""
    for vertical, terms in SHEET.items():
        if vertical not in VERTICALS:
            continue
        for term in terms:
            owner = owned_elsewhere(term, vertical, _VERTICAL_KEYWORDS)
            if not owner:
                continue
            assert owner in classify_verticals(term), (
                f"{term!r} was dropped from {vertical} as owned by {owner}, "
                f"but classifying it gives {classify_verticals(term)}")


def test_the_comparison_uses_handwritten_patterns_only():
    """Against merged sets the answer would depend on which vertical was built
    first, and a rule whose result changes with dictionary order is not a
    rule."""
    forward = owned_elsewhere("Energy", "Livelihood", _VERTICAL_KEYWORDS)
    reversed_dict = dict(reversed(list(_VERTICAL_KEYWORDS.items())))
    assert owned_elsewhere("Energy", "Livelihood", reversed_dict) == forward


# ------------------------------------------------------------- housekeeping

def test_case_only_duplicate_patterns_collapse():
    r"""`\bEnergy\b` from the sheet and `\benergy\b` hand-written are one rule
    evaluated twice under IGNORECASE. Harmless, and it made the audit print the
    same rule as two rows with identical counts."""
    assert dedupe_patterns([r"\bEnergy\b", r"\benergy\b", r"climate"]) == [
        r"\bEnergy\b", r"climate"]


def test_the_audit_no_longer_reports_a_rule_twice():
    from app.services.verticals import _COMPILED

    for vertical, patterns in _COMPILED.items():
        sources = [p.pattern.casefold() for p in patterns]
        assert len(sources) == len(set(sources)), vertical


def test_the_handwritten_guard_is_documented_as_currently_inert():
    """SERVICE_LINE_PATTERNS matches nothing today — the service-line terms all
    arrive through the sheet merge. It stays as a guard against someone adding
    them back by hand, and this test records that it is currently a no-op so
    nobody mistakes it for the thing doing the work."""
    hits = [
        (v, p) for v, pats in _VERTICAL_KEYWORDS.items()
        for p in pats if pattern_is_service_line(v, p)
    ]
    assert hits == [], (
        "a hand-written service-line pattern now exists; that is fine, but the "
        f"docstring saying this guard is inert is out of date: {hits}")


def test_every_service_line_term_is_lowercase():
    """Comparison is casefolded on one side only, so an upper-case entry here
    would silently never match."""
    for term in SERVICE_LINE_TERMS:
        assert term == term.lower(), term


def test_the_local_e4c_constant_matches_the_real_one():
    """service_terms cannot import from verticals — verticals imports IT while
    building its pattern table — so the name is spelled out there. This is what
    stops the two copies drifting apart silently."""
    from app.services import service_terms, verticals

    assert service_terms.VERTICAL_E4C == verticals.VERTICAL_E4C


# ------------------------------- de-duplication was not the cosmetic change
# ------------------------------- I first described it as

def test_a_single_body_mention_no_longer_scores_twice():
    r"""`\bEnergy\b` from the sheet and `\benergy\b` hand-written both matched
    the SAME word, so one mention of "energy" in a summary scored 2 and cleared
    the two-point threshold on its own. The rule says "a title hit, or 2+ body
    hits" — two hits means two signals, not one signal counted twice.

    On the live database this moved 120 rows, which is why calling the dedupe
    harmless was wrong.
    """
    from app.services.verticals import classify_verticals

    got = classify_verticals("Procurement of Office Furniture",
                             "The project supports the national energy sector.")
    assert "Climate/Sustainability(ESG)" not in got


@pytest.mark.parametrize("title", [
    "Liberia Electricity Sector Strengthening and Access Project",
    "Mozambique Energy Sector Programmatic Preparation: Hydropower",
    "Rural Electrification Programme, Phase II",
])
def test_real_energy_projects_are_still_climate(title):
    """The rows the dedupe dropped were genuine energy-sector projects that
    this vertical had never matched on their own words — it claimed "energy"
    and could not recognise "electricity". The gap was exposed, not created."""
    assert "Climate/Sustainability(ESG)" in classify_verticals(title), title


@pytest.mark.parametrize("title", [
    "Study on Purchasing Power Parity in South Asia",
    "Grid Computing Infrastructure Procurement",
])
def test_the_energy_terms_stay_narrow(title):
    """Bare "power" and "grid" are deliberately excluded. Purchasing power and
    grid computing are not energy projects."""
    assert "Climate/Sustainability(ESG)" not in classify_verticals(title), title


def test_a_noncommunicable_disease_project_is_health_not_climate():
    """One of the 120. Losing Climate here is the fix working."""
    got = classify_verticals("Serbia Noncommunicable Diseases Prevention "
                             "and Control Project")
    assert "Health" in got
    assert "Climate/Sustainability(ESG)" not in got


# --------------------------------------------- span scoring (off by default)

def test_span_scoring_is_off_until_someone_measures_it():
    """It re-tags a large share of the database. The flag exists so the
    measurement happens before the change, not after."""
    from app.core.config import Settings, settings

    assert settings.vertical_span_scoring is False
    assert Settings.model_fields["vertical_span_scoring"].default is False


def test_one_phrase_matched_by_a_general_and_a_specific_pattern_scores_once():
    r"""'health system strengthening' matches \bhealth AND health\s+system.
    Under pattern counting that is 2 — enough to tag from the body alone,
    which the documented rule says should not happen."""
    from app.services.verticals import classify_verticals

    body = "Support for health system strengthening."
    assert "Health" in classify_verticals("Office Furniture", body)
    assert "Health" not in classify_verticals("Office Furniture", body,
                                              span_scoring=True)


def test_two_genuinely_different_phrases_still_score_two():
    """The fix must not simply make everything score 1. Two distinct concepts
    in two distinct places is what the threshold was always meant to mean."""
    from app.services.verticals import classify_verticals

    body = "Biodiversity work and separate reforestation activities."
    assert "Climate/Sustainability(ESG)" in classify_verticals(
        "Office Furniture", body, span_scoring=True)


def test_a_title_hit_alone_still_qualifies_under_span_scoring():
    from app.services.verticals import classify_verticals

    assert "Health" in classify_verticals("Maternal Health Programme", "",
                                          span_scoring=True)


def test_overlapping_spans_merge_rather_than_accumulate():
    from app.services.verticals import _covered

    spans = _covered("health system strengthening", "Health")
    assert len(spans) == 1, spans
