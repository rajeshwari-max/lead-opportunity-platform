"""The digest and reminder must carry the same decision-ready opportunity brief."""
from __future__ import annotations

from datetime import date

from app.database.models import Category, Opportunity, TeamMember
from app.services.email_service import _digest_html, _reminder_html


def _member() -> TeamMember:
    return TeamMember(
        name="Asha", email="asha@example.org", keywords="health",
        categories="", verticals="Health", active=True, auto_send=False,
    )


def _opportunity(**changes) -> Opportunity:
    fields = dict(
        id=17,
        unique_id="mail-fixture",
        title="Community Health Fund",
        organization="Example Foundation",
        country="India",
        region="South Asia",
        location="India and Nepal",
        category=Category.GRANT,
        work_type="Implementation",
        study_type="",
        deadline=date(2026, 10, 8),
        opportunity_url="https://example.org/fund",
        website="https://example.org",
        source_website="Example",
        summary="Supports community-led primary health programmes.",
        eligibility="Registered nonprofits with three years of experience.",
        funding_amount="USD 250,000",
        brands="Swasti, Setu",
        verticals="Health, Livelihood",
        approved=True,
    )
    fields.update(changes)
    return Opportunity(**fields)


def test_digest_contains_the_complete_opportunity_brief():
    html = _digest_html(_member(), [_opportunity()])

    for expected in (
        "New funding opportunities",
        "Summary:",
        "Supports community-led primary health programmes.",
        "Opportunity size:",
        "USD 250,000",
        "Source:",
        "Example",
        "Eligibility:",
        "Registered nonprofits with three years of experience.",
        "Brand:",
        "Swasti, Setu",
        "Vertical:",
        "Health, Livelihood",
        "Archetype:",
        "Grant",
        "Implementation",
        "08 Oct 2026",
    ):
        assert expected in html


def test_reminder_reuses_the_digest_ui_and_details():
    digest = _digest_html(_member(), [_opportunity()])
    reminder = _reminder_html(_member(), [_opportunity()], 7)

    assert "New funding opportunities" in digest
    assert "Deadline reminder" in reminder
    assert "in 7 days" in reminder
    for shared in (
        "Open the dashboard", "Opportunity size:", "Eligibility:",
        "Brand:", "Vertical:", "Archetype:",
    ):
        assert shared in digest
        assert shared in reminder


def test_digest_and_reminder_have_no_approval_action():
    for approved in (False, True):
        opportunities = [_opportunity(approved=approved)]
        for html in (_digest_html(_member(), opportunities),
                     _reminder_html(_member(), opportunities, 7)):
            assert "Approve" not in html
            assert "/approve/" not in html
            # The Action column came back on 2026-10-06 — for "Add to Wrike"
            # only, and only where Wrike is enabled (test_email_wrike_button).
            if ">Action</th>" in html:
                assert "Add to Wrike" in html
            assert "Open the dashboard" in html


def test_compact_email_keeps_required_classification_and_eligibility():
    opportunities = [
        _opportunity(id=i, unique_id=f"mail-{i}", title=f"Health Fund {i}")
        for i in range(45)
    ]
    html = _reminder_html(_member(), opportunities, 3)

    assert "Eligibility:" in html
    assert "Brand:" in html
    assert "Vertical:" in html
    assert "Archetype:" in html


def test_scraped_text_is_escaped_in_html_mail():
    html = _digest_html(
        _member(),
        [_opportunity(title='<script>alert("x")</script>', eligibility="A & B")],
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "A &amp; B" in html
