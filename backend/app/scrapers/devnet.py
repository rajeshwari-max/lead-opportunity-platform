"""DevNetJobsIndia RFPs/Tenders scraper (https://www.devnetjobsindia.org).

The site is ASP.NET WebForms: listing rows and pagination use __doPostBack, so
'next page' is a POST carrying __VIEWSTATE. Detail pages, however, are directly
addressable as jobdescription.aspx?job_id=<id>; ids are recovered from logo image
filenames (joblogos/<job_id>.jpg) and postback control references.
"""
from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register
from app.services.amounts import extract_amount
from app.services.organization import extract_organization

log = logging.getLogger("scraper")

_JOB_ID = re.compile(r"joblogos/(\d+)\.\w{3,4}", re.IGNORECASE)
_JOB_ID_HREF = re.compile(r"job_id=(\d+)", re.IGNORECASE)
_APPLY_BY = re.compile(r"apply\s*by\s*:?\s*(.+)", re.IGNORECASE)
_LOCATION = re.compile(r"location\s*:?\s*(.+)", re.IGNORECASE)
_PAGE_POSTBACK = re.compile(r"__doPostBack\('([^']*grdJobs[^']*)','(Page\$\d+)'\)")

_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


@register
class DevNetScraper(BaseScraper):
    name = "devnet"
    display_name = "DevNetJobsIndia"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    # This scraper already had a parse_detail() implementation, but the crawl
    # engine never called it — the hook was dead code. It runs now.
    enrich_details = True
    website = "https://www.devnetjobsindia.org"
    start_url = "https://www.devnetjobsindia.org/rfp_assignments.aspx"

    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        grid = soup.find(id=re.compile(r"grdJobs", re.IGNORECASE)) or soup
        items: list[RawOpportunity] = []

        # Sidebar widgets expose direct jobdescription.aspx?job_id=N links with
        # (truncated) titles — build a title-prefix -> job_id map to recover
        # detail URLs for grid rows whose links are postback-only.
        title_to_id: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            m = _JOB_ID_HREF.search(a["href"])
            label = a.get_text(" ", strip=True).rstrip("….").rstrip(".")
            if m and len(label) > 15:
                title_to_id[label.lower()] = m.group(1)

        for row in grid.find_all("tr"):
            text = row.get_text("\n", strip=True)
            apply_by = _APPLY_BY.search(text)
            if not apply_by:
                continue  # header/pager rows

            title_link = None
            for a in row.find_all("a"):
                label = a.get_text(" ", strip=True)
                if label and "logolink" not in (a.get("href") or ""):
                    title_link = a
                    break
            if title_link is None:
                continue
            title = title_link.get_text(" ", strip=True)

            lines = [ln for ln in text.split("\n") if ln.strip()]
            org = ""
            try:
                idx = next(i for i, ln in enumerate(lines) if ln.startswith(title[:40]))
                if idx + 1 < len(lines) and not _LOCATION.search(lines[idx + 1]):
                    org = lines[idx + 1][:512]
                elif idx + 1 < len(lines):
                    org = ""
            except StopIteration:
                pass
            loc = _LOCATION.search(text)

            items.append(
                RawOpportunity(
                    title=title,
                    organization=org,
                    location=(loc.group(1).strip()[:512] if loc else ""),
                    country="India",
                    region="South Asia",
                    deadline_raw=apply_by.group(1).strip()[:64],
                    opportunity_url=self._detail_url(row, title, title_to_id),
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.RFP,  # hint only — classifier decides
                )
            )
        return items

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        """ASP.NET pager: POST back with __EVENTARGUMENT='Page$<n+1>' if such a
        postback target exists on the current page."""
        soup = BeautifulSoup(html, "lxml")
        target_arg = f"Page${page_number + 1}"
        target_ctrl: str | None = None
        for m in _PAGE_POSTBACK.finditer(html):
            if m.group(2) == target_arg:
                target_ctrl = m.group(1)
                break
        if target_ctrl is None:
            return None

        data: dict[str, str] = {
            "__EVENTTARGET": target_ctrl,
            "__EVENTARGUMENT": target_arg,
        }
        for field in _HIDDEN_FIELDS:
            tag = soup.find("input", {"name": field})
            if tag and tag.get("value") is not None:
                data[field] = tag["value"]
        return PageRequest(page_url, method="POST", data=data)

    async def parse_detail(self, item: RawOpportunity, client: httpx.AsyncClient) -> RawOpportunity:
        if not item.opportunity_url or "job_id=" not in item.opportunity_url:
            return item
        try:
            resp = await client.get(item.opportunity_url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return item
        soup = BeautifulSoup(resp.text, "lxml")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "span", "div"])]
        body = max(paragraphs, key=len, default="")
        if len(body) > 120 and not item.summary:
            item.summary = body[:1000]
        # The detail page is also where the budget/organisation usually appear.
        page_text = soup.get_text(" ", strip=True)
        if not item.funding_amount:
            item.funding_amount = extract_amount(page_text)
        if not item.organization:
            item.organization = extract_organization(page_text, item.title)
        return item

    # ---------------------------------------------------------------- helpers
    def _detail_url(self, row, title: str, title_to_id: dict[str, str]) -> str:
        for a in row.find_all("a", href=True):
            m = _JOB_ID_HREF.search(a["href"])
            if m:
                return f"{self.website}/jobdescription.aspx?job_id={m.group(1)}"
        for img in row.find_all("img", src=True):
            m = _JOB_ID.search(img["src"])
            if m:
                return f"{self.website}/jobdescription.aspx?job_id={m.group(1)}"
        # postback-only row: recover job_id by matching sidebar link titles
        low = title.lower()
        for label, job_id in title_to_id.items():
            if low.startswith(label[:40]) or label.startswith(low[:40]):
                return f"{self.website}/jobdescription.aspx?job_id={job_id}"

        # No id recovered. Returning start_url here is what produced "86
        # different RFPs all pointing at rfp_assignments.aspx" — every one of
        # them opening the index the row was scraped from, and every one
        # sharing a URL, which also defeats deduplication.
        #
        # "" instead: ScraperManager refuses to store a row with no link to the
        # call itself (LOP_REQUIRE_USABLE_LINK), so the row is dropped rather
        # than shipped as a lead that goes nowhere. Losing a row is better than
        # publishing one that wastes the reader's click — and unlike a bad link,
        # a missing one is visible in the counts.
        log.info("[%s] no job_id recoverable for %r — dropping the row rather "
                 "than pointing it at the index", self.name, title[:60])
        return ""
