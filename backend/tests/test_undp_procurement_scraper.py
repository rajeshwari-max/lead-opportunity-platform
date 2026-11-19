from __future__ import annotations

import pytest

from app.database.models import Category
from app.scrapers.undp import UNDPProcurementScraper


def _card(*, notice_id="51842", title="Knowledge Management Events",
          process="RFP - Request for proposal", deadline="29-Nov-26",
          office="UNDP-AFG/AFGHANISTAN") -> str:
    return f"""
    <a class="vacanciesTableLink vacanciesTable__row region_RAS"
       href="view_negotiation.cfm?nego_id={notice_id}">
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Title</div><span>{title}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Ref No</div><span>UNDP-AFG-00930</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">UNDP Office/Country</div><span>{office}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Process</div><span>{process}</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Deadline</div><span>{deadline}<br/>05:02 AM (New York time)</span></div>
      <div class="vacanciesTable__cell"><div class="vacanciesTable__cell__label">Posted</div><span>18-Nov-26</span></div>
    </a>"""


@pytest.mark.parametrize(("process", "record_type", "category"), [
    ("RFP - Request for proposal", "rfp", Category.RFP),
    ("RFQ - Request for quotation", "rfq", Category.RFP),
    ("ITB - Invitation to bid", "itb", Category.TENDER),
    ("EOI - Expression of interest", "eoi", Category.RFP),
    ("IC - Individual contractor", "consultancy", Category.RFP),
])
def test_source_process_controls_the_notice_type(process, record_type, category):
    rows = UNDPProcurementScraper().parse_listing(
        _card(process=process), UNDPProcurementScraper.start_url,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.record_type == record_type
    assert row.category_hint is category
    assert row.deadline_raw == "29-Nov-26"
    assert row.country == "Afghanistan"
    assert row.opportunity_url.endswith("view_negotiation.cfm?nego_id=51842")
    assert "Reference: UNDP-AFG-00930" in row.summary


def test_every_labelled_notice_card_is_returned_once():
    html = _card(notice_id="1") + _card(notice_id="2", process="RFQ - Request for quotation")
    rows = UNDPProcurementScraper().parse_listing(html, UNDPProcurementScraper.start_url)
    assert [row.opportunity_url.rsplit("=", 1)[-1] for row in rows] == ["1", "2"]


def test_real_next_link_will_be_followed_if_undp_adds_pagination():
    html = _card() + '<div class="pagination"><a href="?lang=en&page=2">Next</a></div>'
    scraper = UNDPProcurementScraper()
    scraper.parse_listing(html, scraper.start_url)
    nxt = scraper.next_page(html, scraper.start_url, 1)
    assert nxt is not None and "page=2" in str(nxt.url)


def test_missing_required_source_field_fails_instead_of_partial_coverage():
    with pytest.raises(RuntimeError, match="refusing partial coverage"):
        UNDPProcurementScraper().parse_listing(
            _card(deadline=""), UNDPProcurementScraper.start_url,
        )


def test_blank_process_is_kept_without_inventing_a_source_type():
    rows = UNDPProcurementScraper().parse_listing(
        _card(title="PAZ - Compra Menor equipos de tecnología", process=""),
        UNDPProcurementScraper.start_url,
    )
    assert len(rows) == 1
    assert rows[0].record_type == ""
    assert rows[0].category_hint is Category.TENDER
    assert "Process: not stated by UNDP" in rows[0].summary


def test_registry_uses_the_dedicated_scraper_not_the_generic_generated_class():
    import app.scrapers  # noqa: F401
    from app.scrapers.registry import SCRAPER_REGISTRY

    assert SCRAPER_REGISTRY["undp_procurement"] is UNDPProcurementScraper
