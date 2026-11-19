"""UNDP Procurement Notices — source-specific row and process parser.

The public board currently renders every open notice on one long HTML page.
Each notice is an ``a.vacanciesTable__row`` card with labelled cells for Title,
Ref No, UNDP Office/Country, Process, Deadline and Posted.  Treating that as a
generic funding page captured the links and dates, but assigned a Grant hint to
every notice.  A row titled simply "Knowledge Management Events" was therefore
at risk of becoming a Grant even though UNDP explicitly called it an RFP.

This scraper reads the source fields verbatim.  Generic pagination detection is
kept as a fallback so a real Next/numbered link will be followed if UNDP adds
one later; invented ``?page=N`` URLs are deliberately not used because the live
site ignores them and repeats page one.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.generic_listing import GenericListingScraper, rows_signature
from app.scrapers.registry import register


_PROCESS = re.compile(r"^\s*([A-Z][A-Z0-9_-]*)\b", re.IGNORECASE)
_RECORD_TYPES = {
    "RFP": ("rfp", Category.RFP),
    "RFQ": ("rfq", Category.RFP),
    "ITB": ("itb", Category.TENDER),
    "EOI": ("eoi", Category.RFP),
    "REOI": ("eoi", Category.RFP),
    "RFI": ("rfp", Category.RFP),
    "IC": ("consultancy", Category.RFP),
    "CFP": ("call_for_proposals", Category.PROPOSAL),
    "LTA": ("tender", Category.TENDER),
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").casefold())


def _country(office: str) -> str:
    """UNDP-IND/INDIA -> India; preserve multi-country text."""
    value = (office or "").strip()
    if "/" not in value:
        return ""
    country = value.rsplit("/", 1)[-1].strip()
    return country.title() if country else ""


@register
class UNDPProcurementScraper(GenericListingScraper):
    name = "undp_procurement"
    display_name = "UNDP Procurement"
    website = "https://procurement-notices.undp.org"
    start_url = f"{website}/?lang=en"
    prefer_js = False
    enrich_details = False
    curated = True
    stale_page_streak_override = 0
    config = {
        "require_funding_signal": False,
        "refresh_on_reseen": True,
    }

    _CARD_SELECTOR = (
        'a.vacanciesTable__row[href*="view_negotiation.cfm"], '
        'a.vacanciesTableLink[href*="view_negotiation.cfm"]'
    )

    @staticmethod
    def _fields(card) -> dict[str, str]:
        fields: dict[str, str] = {}
        for cell in card.select(".vacanciesTable__cell"):
            label = cell.select_one(".vacanciesTable__cell__label")
            if label is None:
                continue
            label_text = label.get_text(" ", strip=True)
            label.extract()
            fields[_key(label_text)] = cell.get_text(" ", strip=True)
        return fields

    @staticmethod
    def _process_type(process: str, title: str = "") -> tuple[str, Category]:
        match = _PROCESS.match(process or "")
        code = match.group(1).upper() if match else ""
        if code in _RECORD_TYPES:
            return _RECORD_TYPES[code]
        if not code:
            # A small number of live UNDP cards publish a blank Process cell.
            # Keep record_type blank (the source did not state one), while
            # supplying only a display hint from unambiguous title wording.
            if re.search(r"\b(compra\s+menor|purchase|supply|procurement)\b",
                         title or "", re.IGNORECASE):
                return "", Category.TENDER
            return "", Category.RFP
        # An unknown source code remains visible and auditable. The source
        # contract is intentionally not an exhaustive allow-list, so a new
        # UNDP process cannot silently disappear.
        return code.casefold(), Category.RFP

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(self._CARD_SELECTOR)
        if not cards:
            # Supports the older table layout captured in the repository and
            # makes a structural site change visible through the generic
            # parser's existing probe evidence instead of returning nothing.
            return super().parse_listing(html, page_url)

        items: list[RawOpportunity] = []
        seen: set[str] = set()
        malformed: list[str] = []
        for card in cards:
            href = card.get("href") or ""
            url = urljoin(page_url, href)
            fields = self._fields(card)
            title = fields.get("title", "").strip()
            deadline = fields.get("deadline", "").strip()
            # Deadline cells also carry the New York time. The date is the
            # first DD-Mon-YY token and is what DeadlineParser expects.
            date_match = re.search(r"\b\d{1,2}-[A-Za-z]{3}-\d{2,4}\b", deadline)
            process = fields.get("process", "").strip()
            if not title or not href or date_match is None:
                malformed.append(href or title or "unidentified row")
                continue
            if url in seen:
                continue
            seen.add(url)

            record_type, category = self._process_type(process, title)
            office = fields.get("undpofficecountry", "").strip()
            ref_no = fields.get("refno", "").strip()
            posted = fields.get("posted", "").strip()
            summary = " | ".join(part for part in (
                f"Title: {title}",
                f"Reference: {ref_no}" if ref_no else "",
                f"UNDP office/country: {office}" if office else "",
                f"Process: {process}" if process else "Process: not stated by UNDP",
                f"Posted: {posted}" if posted else "",
            ) if part)
            country = _country(office)
            items.append(RawOpportunity(
                title=title[:500],
                organization=self.display_name,
                country=country,
                location=country,
                summary=summary[:1000],
                deadline_raw=date_match.group(0),
                opportunity_url=url,
                website=self.website,
                source_website=self.display_name,
                category_hint=category,
                record_type=record_type,
                source_status="open",
                dayfirst=True,
            ))

        if malformed:
            raise RuntimeError(
                f"UNDP changed or omitted required fields on {len(malformed)} "
                f"of {len(cards)} notice row(s); refusing partial coverage. "
                f"Examples: {', '.join(malformed[:3])}"
            )

        self._page_had_items = bool(items)
        self._prev_signature, self._page_signature = (
            self._page_signature, rows_signature(items)
        )
        self.last_probe["expected_container_present"] = True
        return items

