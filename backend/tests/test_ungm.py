"""UNGM public notice list (scrapers/ungm.py)."""
from __future__ import annotations

from pathlib import Path

import app.scrapers  # noqa: F401
from app.scrapers.registry import SCRAPER_REGISTRY

FIXTURE = Path(__file__).parent / "fixtures" / "ungm_notices.html"


def _items():
    s = SCRAPER_REGISTRY["ungm"]()
    return s.parse_listing(FIXTURE.read_text(encoding="utf-8"), s.start_url)


def test_every_notice_row_is_read_and_the_header_is_not():
    items = _items()
    assert len(items) == 4
    assert items[0].title == ("RFP/CRVS/2610/64 Design,Configuration,Deployment, "
                              "& Rollout of OneRegistry CRVS")


def test_fields_come_from_their_own_cells():
    rfp = next(i for i in _items() if "Parametric Insurance" in i.title)
    assert rfp.deadline_raw == "30-Nov-2026"
    assert rfp.organization == "UNDP" and rfp.country == "India"
    assert rfp.opportunity_url == "https://www.ungm.org/Public/Notice/322569"
    assert rfp.record_type == "rfp" and rfp.category_hint.value == "RFP"
    assert "UNDP-IND-00861" in rfp.summary and "Published 13-Nov-2026" in rfp.summary


def test_notice_types_map_to_record_types():
    by_title = {i.title[:20]: i.record_type for i in _items()}
    assert by_title["381-Senior Utility S"] == "consultancy"
    assert by_title["Provision of TOGAF E"] == "eoi"


def test_ungm_is_registered_with_a_contract_and_is_curated():
    from app.services.source_manifest import contract_for
    cls = SCRAPER_REGISTRY["ungm"]
    assert cls.curated and not cls.enrich_details
    c = contract_for("ungm")
    assert c.production_enabled and c.scope_status.value == "confirmed"
