from __future__ import annotations

from pathlib import Path

from app.database.models import Category
from app.scrapers.phf import FUNDING_URL, PHFScraper
from app.services.source_manifest import MANIFESTS

FIXTURE = Path(__file__).parent / "fixtures" / "phf_funding.html"


def test_phf_uses_the_current_open_applications_url_without_a_browser():
    scraper = PHFScraper()

    assert scraper.start_url == "https://www.phf.org.uk/funding#heading-54836"
    assert scraper.start_url == FUNDING_URL
    assert not scraper.requires_js
    assert not scraper.prefer_js


def test_phf_parser_keeps_only_the_open_section():
    items = PHFScraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"), FUNDING_URL
    )

    assert [item.title for item in items] == [
        "Teacher Development Fund",
        "Migration Fund",
        "India Fund",
    ]
    assert all(item.category_hint is Category.GRANT for item in items)
    assert all(item.record_type == "grant" for item in items)
    assert all(item.source_status == "open" for item in items)
    assert all("/funding/" in item.opportunity_url for item in items)
    assert "Arts Fund" not in {item.title for item in items}
    assert "Awards for Artists" not in {item.title for item in items}


def test_phf_deadline_rolling_amount_and_india_geography_are_explicit():
    teacher, migration, india = PHFScraper().parse_listing(
        FIXTURE.read_text(encoding="utf-8"), FUNDING_URL
    )

    assert teacher.deadline_raw == "11 November 2026"
    assert not teacher.assume_active
    assert teacher.funding_amount == "Up to £165,000"

    assert migration.deadline_raw == ""
    assert migration.assume_active
    assert migration.funding_amount == "Up to £60,000"

    assert india.assume_active
    assert india.country == "India"
    assert india.region == "South Asia"
    assert india.funding_amount == "₹10,00,000 से ₹40,00,000"


def test_awarded_grant_databases_are_disabled_not_scraped_as_opportunities():
    macarthur = MANIFESTS["macarthur_foundation"]
    hewlett = MANIFESTS["hewlett_foundation"]

    assert not macarthur.production_enabled
    assert "historical" in macarthur.known_defect.lower()
    assert "past awards" in macarthur.known_defect.lower()
    assert not hewlett.production_enabled
    assert "grantmaking database" in hewlett.known_defect.lower()
    assert "awarded" in hewlett.known_defect.lower()
