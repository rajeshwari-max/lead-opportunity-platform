"""Precision checks for the dashboard's opportunity-only contract."""
from __future__ import annotations

from app.database.models import Category
from app.services.classification import (
    KeywordClassifier,
    category_hint_for_record_type,
)
from app.services.opportunity_gate import is_opportunity


def test_page_level_grant_hint_does_not_turn_a_random_link_into_an_opportunity():
    keep, why = is_opportunity(
        "Our approach to stronger communities",
        "Learn about our programmes and partners.",
        "https://example.org/what-we-do/communities",
        category="Grant",
        curated=False,
    )
    assert not keep
    assert why == "no opportunity signal"


def test_news_path_is_rejected_even_when_the_slug_mentions_a_grant():
    keep, why = is_opportunity(
        "Foundation announces a new education grant",
        "The programme will support schools over five years.",
        "https://example.org/news/new-education-grant",
        category="Grant",
        curated=False,
    )
    assert not keep
    assert why == "page type is never an opportunity"


def test_direct_rfp_title_survives_an_informational_looking_source_path():
    keep, why = is_opportunity(
        "RFPs: RSV and Functional Decline in Hospitalized Adults",
        "Deadline: 19-Oct-2026",
        "https://example.org/research-2/rfps-rsv-functional-decline/",
        curated=False,
    )
    assert keep and why == ""


def test_bare_funding_language_is_not_enough_to_admit_a_programme_page():
    keep, why = is_opportunity(
        "Climate funding programme",
        "Information about our long-term funding strategy and portfolio.",
        "https://example.org/programmes/climate-finance",
        curated=False,
    )
    assert not keep
    assert why == "no opportunity signal"


def test_open_call_is_a_first_class_proposal_category():
    got = KeywordClassifier().classify(
        "Open Call: Community Evidence Partners", "", None)
    assert got is Category.PROPOSAL


def test_plural_call_acronyms_are_recognised():
    classifier = KeywordClassifier()
    assert classifier.classify("CFPs: Agriculture Research 2026") is Category.PROPOSAL
    assert classifier.classify("CFAs: Community Health Partners") is Category.RFP


def test_open_call_for_an_award_is_not_mistaken_for_a_past_winner_story():
    keep, why = is_opportunity(
        "Open Call for Emerging Sustainability Leader Award",
        "Previous winners came from twelve countries. Applications are open.",
        "https://example.org/opportunities/sustainability-leader-award",
        curated=False,
    )
    assert keep and why == ""


def test_structured_source_types_map_to_dashboard_categories():
    assert category_hint_for_record_type("grant") is Category.GRANT
    assert category_hint_for_record_type("call_for_proposals") is Category.PROPOSAL
    assert category_hint_for_record_type("eoi") is Category.RFP
    assert category_hint_for_record_type("rfq") is Category.RFP
    assert category_hint_for_record_type("itb") is Category.TENDER
    assert category_hint_for_record_type("contract_award") is None


def test_curated_board_can_keep_a_real_call_with_a_plain_title():
    keep, why = is_opportunity(
        "Disability Inclusion Assessment",
        "UN Agency: UNICEF",
        "https://www.unpartnerportal.org/landing/opportunities/123/",
        curated=True,
    )
    assert keep and why == ""


def test_dashboard_scope_hides_legacy_other_and_linkless_rows():
    from datetime import date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database.models import Base, Opportunity, Status
    from app.schemas.opportunity import OpportunityFilters
    from app.services.filter_service import FilterService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        common = dict(
            organization="Org", source_website="Source", status=Status.ACTIVE,
            deadline=date(2099, 1, 1), deadline_state="dated",
            verticals="Health",
        )
        db.add_all([
            Opportunity(unique_id="good", title="Health Grant", category=Category.GRANT,
                        opportunity_url="https://example.org/grants/health", **common),
            Opportunity(unique_id="other", title="Random programme", category=Category.OTHER,
                        opportunity_url="https://example.org/programme", **common),
            Opportunity(unique_id="nolink", title="Linkless Grant", category=Category.GRANT,
                        opportunity_url="", **common),
        ])
        db.flush()

        result = FilterService(db).query(OpportunityFilters(
            opportunity_types_only=True))
        assert [row.unique_id for row in result.items] == ["good"]
