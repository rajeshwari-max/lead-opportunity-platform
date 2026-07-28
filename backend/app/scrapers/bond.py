"""Bond funding opportunities scraper (https://www.bond.org.uk/funding-opportunities/).

Bond renders 9 cards server-side; the remaining ~490 load through a FacetWP
"Load more" button. Their REST refresh endpoint rejects non-browser calls
("unable to auto-detect the post listing"), so with Playwright installed this
scraper renders the page and clicks Load More until every card is present,
then parses them all in one pass. Without Playwright it falls back to plain
HTTP and yields just the server-rendered first batch.

Closing date may be a real date or "Ongoing" (kept active, no deadline).
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from app.core.config import settings
from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register

log = logging.getLogger("scraper")

_CLOSING = re.compile(r"closing\s+date\s*:?\s*(.+)", re.IGNORECASE)
_LOCATION = re.compile(r"^location\s*:?\s*(.+)", re.IGNORECASE)
_SKIP_HEADINGS = {"filter by", "region", "eligibility", "grant size", "closing date",
                  "contact", "collaborate", "connect", "subscribe", "funding opportunities"}
_LOAD_MORE_SELECTOR = ".facetwp-load-more, .fwp-load-more, button.facetwp-load-more"


@register
class BondScraper(BaseScraper):
    name = "bond"
    display_name = "Bond UK"
    website = "https://www.bond.org.uk"
    start_url = "https://www.bond.org.uk/funding-opportunities/"
    prefer_js = True   # browser-render + Load More expansion when Playwright exists

    # Single (expanded) page — everything is parsed in one pass.
    def next_page(self, html: str, page_url: str, page_number: int) -> None:
        return None

    def _fetch_rendered_sync(self, url: str) -> str:
        """Render the page, then click Load More until all results are loaded."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=settings.user_agent)
                page.goto(url, timeout=int(settings.request_timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

                count_js = ("document.querySelectorAll('.facetwp-template h3,"
                            " .facetwp-template h4').length")
                total = page.evaluate(
                    "window.FWP?.settings?.pager?.total_rows || 0"
                )
                clicks = 0
                prev_count = page.evaluate(count_js)
                for _ in range(150):
                    # JS click: immune to overlay/visibility quirks. False when
                    # the button is gone or hidden (= last page reached).
                    clicked = page.evaluate(
                        """(sel) => {
                            const b = document.querySelector(sel);
                            if (!b || b.offsetParent === null) return false;
                            b.click();
                            return true;
                        }""",
                        _LOAD_MORE_SELECTOR.split(",")[0].strip(),
                    )
                    if not clicked:
                        break
                    clicks += 1
                    try:  # wait until the batch actually renders (not a fixed nap)
                        page.wait_for_function(
                            f"{count_js} > {prev_count}", timeout=10_000
                        )
                    except Exception:
                        break  # 10s with no growth — genuinely stuck/finished
                    prev_count = page.evaluate(count_js)
                    if total and prev_count >= total:
                        break  # everything the site reports is now on the page
                log.info("[bond] expanded via %s Load More clicks — %s/%s items rendered",
                         clicks, prev_count, total or "?")
                return page.content()
            finally:
                browser.close()

    # ------------------------------------------------------------------ parse
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        articles = soup.select("article.opportunity-item")
        if articles:
            return [item for art in articles if (item := self._parse_article(art)) is not None]
        return self._parse_headings_fallback(soup)   # non-rendered fallback markup

    def _parse_article(self, art: Tag) -> RawOpportunity | None:
        """One card = one <article class='opportunity-item' id='post-NNN'>."""
        title_el = art.select_one(".opportunity-item__title") or art.find(["h3", "h4"])
        if title_el is None:
            return None
        funder = title_el.get_text(" ", strip=True)
        if not funder or "cookie" in funder.lower():
            return None

        # <dl><dt>Location</dt><dd>Worldwide</dd><dt>Closing date</dt><dd>…</dd></dl>
        meta: dict[str, str] = {}
        for dl in art.select("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                meta[dt.get_text(" ", strip=True).lower()] = dd.get_text(" ", strip=True)

        closing = meta.get("closing date", "")
        location = meta.get("location", "")
        grant_size = meta.get("grant size", "")

        paragraphs = [p.get_text(" ", strip=True) for p in art.select(".opportunity-item__content > p")]
        summary = next((p for p in paragraphs if len(p) > 60), "")[:1000]
        elig_el = art.select_one(".js-eligibility-content")
        eligibility = elig_el.get_text(" ", strip=True)[:600] if elig_el else ""

        apply_el = art.select_one(".opportunity-item__link a[href]")
        post_id = art.get("id", "")
        url = apply_el["href"] if apply_el else f"{self.start_url}#{post_id}"

        return RawOpportunity(
            title=f"{funder} — Funding Opportunity" if len(funder) < 40 else funder,
            organization=funder,
            deadline_raw=closing[:64],
            summary=summary,
            eligibility=eligibility,
            location=location,
            funding_amount=(f"£{grant_size}k+" if grant_size.isdigit() else grant_size)[:256],
            country="Global" if location.lower().startswith("worldwide") else "",
            region="Global" if location.lower().startswith("worldwide") else "",
            opportunity_url=url,
            website=self.website,
            source_website=self.display_name,
            category_hint=Category.GRANT,
        )

    def _parse_headings_fallback(self, soup: BeautifulSoup) -> list[RawOpportunity]:
        """Heading-walk heuristic for the plain-HTTP (non-rendered) page."""
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        for h in soup.find_all(["h3", "h4"]):
            funder = h.get_text(" ", strip=True)
            if not funder or funder.lower() in _SKIP_HEADINGS or len(funder) < 4 \
                    or "cookie" in funder.lower():
                continue
            card_text, apply_url = self._collect_card(h)
            closing = _CLOSING.search(card_text)
            if not closing:
                continue
            key = f"{funder}|{closing.group(1).strip()[:24]}"
            if key in seen:
                continue
            seen.add(key)
            location = ""
            for line in card_text.split("\n"):
                m = _LOCATION.match(line.strip())
                if m:
                    location = m.group(1).strip()[:512]
                    break
            paragraphs = [ln.strip() for ln in card_text.split("\n")
                          if len(ln.strip()) > 80 and "closing date" not in ln.lower()]
            items.append(RawOpportunity(
                title=f"{funder} — Funding Opportunity" if len(funder) < 40 else funder,
                organization=funder,
                deadline_raw=closing.group(1).strip()[:64],
                summary=(paragraphs[0][:1000] if paragraphs else ""),
                eligibility=(paragraphs[1][:600] if len(paragraphs) > 1 else ""),
                location=location,
                country="Global" if location.lower().startswith("worldwide") else "",
                region="Global" if location.lower().startswith("worldwide") else "",
                opportunity_url=apply_url or self.start_url,
                website=self.website,
                source_website=self.display_name,
                category_hint=Category.GRANT,
            ))
        return items

    @staticmethod
    def _collect_card(heading: Tag) -> tuple[str, str]:
        """Text + first external Apply link between this heading and the next."""
        lines: list[str] = []
        apply_url = ""
        for sib in heading.find_all_next(limit=25):
            if sib.name in {"h3", "h4"} and sib is not heading:
                break
            if sib.name == "a" and sib.get("href", "").startswith("http") \
                    and "bond.org.uk" not in sib["href"] and not apply_url:
                apply_url = sib["href"]
            if sib.name in {"p", "div", "span", "dt", "dd", "li"}:
                text = sib.get_text("\n", strip=True)
                if text:
                    lines.append(text)
        return "\n".join(lines), apply_url
