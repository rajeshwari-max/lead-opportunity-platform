from __future__ import annotations

import json

import pytest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database.models import Base, Category, Opportunity, Status
from app.schemas.opportunity import OpportunityFilters
from app.services.brand_keywords import (
    BRAND_KEYWORDS,
    BRAND_KEYWORD_SECTIONS,
    SOURCE_KEYWORD_COUNTS,
)
from app.services.brands import classify_brands
from app.services.filter_service import FilterService


@pytest.mark.parametrize(
    ("brand", "title"),
    [
        ("Green Foundation", "Open call for climate-smart agriculture innovation"),
        ("Vrutti", "FPO development and women entrepreneurship grant"),
        ("Swasti", "Digital health and primary health systems programme"),
        ("Setu", "Universal social protection systems programme"),
        ("Upfront", "Improving garment worker safety and workplace wellbeing"),
        ("Community Action Collab", "Systems change through collaborative action"),
    ],
)
def test_specific_workbook_phrases_assign_the_expected_brand(brand, title):
    result = classify_brands(title)
    assert brand in result.labels
    assert result.evidence[brand]


def test_all_source_keywords_are_preserved_without_case_insensitive_duplicates():
    assert {name: len(terms) for name, terms in BRAND_KEYWORDS.items()} == SOURCE_KEYWORD_COUNTS
    for terms in BRAND_KEYWORDS.values():
        folded = [term.casefold() for term in terms]
        assert len(folded) == len(set(folded))


def test_setu_subsections_are_preserved_separately_and_flattened_once():
    sections = BRAND_KEYWORD_SECTIONS["Setu"]
    assert tuple(sections) == (
        "Core Social Protection", "Government & Policy", "Last-Mile Access",
        "Vulnerable Communities", "Livelihoods & Workers", "Health & Nutrition",
        "Financial Inclusion", "Digital & Technology", "Climate & Shocks",
        "Research & Evidence", "Capacity Building", "CSR & Philanthropy",
        "Development Sector", "Partnerships", "District-Level Development",
        "Key Government Functions",
    )
    assert sum(map(len, sections.values())) == 139
    assert len(BRAND_KEYWORDS["Setu"]) == 134
    assert "Informal Workers" in sections["Vulnerable Communities"]
    assert "Informal Workers" in sections["Livelihoods & Workers"]


def test_collab_subsections_are_preserved_separately_and_flattened_once():
    sections = BRAND_KEYWORD_SECTIONS["Community Action Collab"]
    assert tuple(sections) == ("Primary Themes", "Core Sectors")
    assert sum(map(len, sections.values())) == 15
    assert len(BRAND_KEYWORDS["Community Action Collab"]) == 13
    assert "Livelihoods" in sections["Primary Themes"]
    assert "Livelihoods" in sections["Core Sectors"]


@pytest.mark.parametrize("title", [
    "Disaster risk reduction funding opportunity",
    "DRR innovation partnership",
    "Workers Rights programme",
])
def test_collab_parenthetical_and_punctuation_variants_match(title):
    assert "Community Action Collab" in classify_brands(title).labels


@pytest.mark.parametrize("title", [
    "Research grant in India",
    "Community design evaluation",
    "Rural worker support",
    "ESG fund announcement",
    "Training grant for women",
    "Agriculture and rural development programme",
    "Health, resilience and collaboration grant",
])
def test_broad_workbook_terms_do_not_classify_by_themselves(title):
    assert classify_brands(title).labels == []


def test_repeated_or_combined_broad_terms_are_not_specific_evidence():
    result = classify_brands(
        "India research evaluation design",
        "Community community community worker rural urban ESG",
    )
    assert result.labels == []


def test_matching_uses_boundaries_not_substrings():
    result = classify_brands("Pharmacy formality research grant")
    assert "Vrutti" not in result.labels  # farm/formal must not match
    assert "Upfront" not in result.labels


def test_multiple_distinct_body_phrases_can_corroborate_a_brand():
    result = classify_brands("Funding opportunity", "Public health delivery with digital health tools")
    assert "Swasti" in result.labels


def test_cross_brand_opportunity_can_be_multi_labelled():
    result = classify_brands("Blended finance for regenerative agriculture")
    assert {"Green Foundation", "Vrutti"}.issubset(result.labels)


def test_scores_and_evidence_are_auditable_json():
    result = classify_brands("Workplace wellbeing programme for garment workers")
    assert json.loads(result.scores_json())["Upfront"] >= 3
    assert json.loads(result.evidence_json())["Upfront"]


def test_brand_filter_reaches_a_brand_match_without_a_cms_vertical():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Opportunity(
            unique_id="vrutti-only", title="FPO grant", source_website="Test",
            category=Category.GRANT, status=Status.ACTIVE,
            deadline=date(2099, 1, 1), deadline_state="dated",
            opportunity_url="https://example.org/fpo", verticals="",
            brands="Vrutti",
        ))
        db.flush()
        result = FilterService(db).query(OpportunityFilters(brands=["Vrutti"]))
        assert [item.unique_id for item in result.items] == ["vrutti-only"]


def test_cms_and_other_brand_selections_are_a_union():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        common = dict(
            source_website="Test", category=Category.GRANT,
            status=Status.ACTIVE, deadline=date(2099, 1, 1),
            deadline_state="dated", opportunity_url="https://example.org/call",
        )
        db.add_all([
            Opportunity(unique_id="health", title="Health grant",
                        verticals="Health", brands="", **common),
            Opportunity(unique_id="vrutti", title="FPO grant",
                        verticals="", brands="Vrutti", **common),
            Opportunity(unique_id="neither", title="Arts grant",
                        verticals="E4C(Evidence for Change)", brands="", **common),
        ])
        db.flush()
        result = FilterService(db).query(OpportunityFilters(
            verticals=["Health"], brands=["Vrutti"]))
        assert {item.unique_id for item in result.items} == {"health", "vrutti"}


def test_brand_stats_include_all_brands_and_count_assignments_once():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Opportunity(unique_id="multi", title="Health grant", source_website="Test",
            category=Category.GRANT, status=Status.ACTIVE, deadline=date(2099, 1, 1),
            deadline_state="dated", opportunity_url="https://example.org/multi",
            verticals="Health, Livelihood", brands="Vrutti, Swasti"))
        db.flush()
        stats = FilterService(db).stats(OpportunityFilters(has_vertical=False))
        assert stats.by_brand == {"CMS":1, "Green Foundation":0, "Vrutti":1,
            "Swasti":1, "Setu":0, "Upfront":0, "Community Action Collab":0}
