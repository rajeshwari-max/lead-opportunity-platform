"""One configurable scraper covering many funder listing pages.

The source list (sources.json) holds ~74 foundation and funder sites. Writing a
bespoke scraper for each would be weeks of work and weeks of maintenance, and
most of them are the same shape: a page of links to individual grant or RFP
pages, frequently WordPress. So this module implements that shape once and is
instantiated per entry in the config.

Extraction is heuristic by design: find the repeated block of links that look
like opportunity titles, then reuse the platform's existing services to fill in
the rest (category classification, verticals, amount, organisation, geography).
A site that needs special handling can either add CSS selectors to its config
entry, or graduate to its own module — the plugin registry treats both the same.

Sites that yield nothing are expected and are reported rather than hidden; see
`validate_sources()`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

CONFIG_PATH = Path(__file__).with_name("sources.json")

# A link is a candidate opportunity when its text reads like a title rather than
# navigation. Tuned to be permissive — the classifier downstream discards noise.
_MIN_TITLE = 18
_MAX_TITLE = 300

_NAV_WORDS = {
    "home", "about", "about us", "contact", "contact us", "privacy", "privacy policy",
    "terms", "terms of use", "cookies", "cookie policy", "search", "menu", "login",
    "log in", "sign in", "sign up", "register", "subscribe", "newsletter", "donate",
    "careers", "jobs", "press", "media", "news", "blog", "events", "team", "our team",
    "read more", "learn more", "find out more", "view all", "see all", "next",
    "previous", "back", "share", "download", "apply", "apply now", "faq", "faqs",
    "sitemap", "accessibility", "français", "español", "english",
}
_NAV_HREF = re.compile(
    r"/(about|contact|privacy|terms|cookie|login|signin|register|subscribe|careers?|"
    r"jobs?|press|media|team|faq|sitemap|accessibility|donate|newsletter|tag|category|"
    r"author|feed|wp-|#)", re.IGNORECASE)

# /page/2/, ?page=2, ?paged=2, ?p=2 — the number is group(2) so it can be bumped.
_PAGE_IN_URL = re.compile(r"(/page/|[?&](?:page|paged|pg|p)=)(\d+)", re.IGNORECASE)

_DEADLINE = re.compile(
    r"(?:deadline|closing date|closes?|apply by|due|submission[s]? (?:by|due)|"
    r"expires?)\s*[:\-–]?\s*"
    r"(\d{1,2}\s+\w{3,9}\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE)


def _looks_like_opportunity(text: str, href: str) -> bool:
    t = text.strip()
    if not (_MIN_TITLE <= len(t) <= _MAX_TITLE):
        return False
    if t.lower() in _NAV_WORDS:
        return False
    if _NAV_HREF.search(href or ""):
        return False
    # Mostly-uppercase short strings are usually section headers or buttons.
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        return False
    return True


class GenericListingScraper(BaseScraper):
    """Base for config-driven sources. Subclasses are generated at import time."""

    config: dict = {}
    # Whether the page just parsed yielded anything — guards URL-bumping so a
    # non-paginated site isn't walked into invented page numbers.
    _page_had_items: bool = False

    # Per-source CSS overrides (optional, from sources.json):
    #   item_selector  — container for each listing
    #   title_selector — title/link within the container
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        items: list[RawOpportunity] = []
        seen: set[str] = set()
        item_sel = self.config.get("item_selector")
        title_sel = self.config.get("title_selector")

        containers = soup.select(item_sel) if item_sel else [soup]
        for container in containers:
            anchors = (container.select(title_sel) if title_sel
                       else container.find_all("a", href=True))
            for a in anchors:
                href = a.get("href") or ""
                if not href:
                    continue
                title = a.get_text(" ", strip=True)
                if not _looks_like_opportunity(title, href):
                    # Very common card layout: the title lives in a heading and
                    # the link is a separate "Learn More" / image anchor, so the
                    # anchor's own text is useless. Fall back to the nearest
                    # heading in the same block. (Gates Grand Challenges lists
                    # its open calls exactly this way, and we saw none of them.)
                    title = self._heading_title(a)
                    if not title or _NAV_HREF.search(href):
                        continue
                url = urljoin(page_url, href)
                if urlparse(url).netloc.replace("www.", "") not in \
                        urlparse(self.website).netloc.replace("www.", ""):
                    continue          # off-site link (social, partner, funder logo)
                if url in seen:
                    continue
                seen.add(url)

                # Deadline, if the surrounding block mentions one.
                block = a.find_parent(["article", "li", "div", "tr"]) or a
                text = block.get_text(" ", strip=True)[:1200]
                m = _DEADLINE.search(text)

                items.append(RawOpportunity(
                    title=title[:500],
                    organization=self.display_name,   # the funder IS the source here
                    summary=text[:1000] if len(text) > len(title) + 40 else "",
                    deadline_raw=(m.group(1) if m else "")[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,     # hint only; classifier decides
                    # These pages rarely state a deadline. Treating them as closed
                    # would discard the entire source, so they are kept as ongoing
                    # and the deadline is filled in by detail enrichment when found.
                    assume_active=not bool(m),
                ))
        self._page_had_items = bool(items)
        return items

    @staticmethod
    def _heading_title(anchor) -> str:
        """Nearest heading text around a link, for card layouts where the
        anchor itself only says 'Learn More' or wraps an image."""
        block = anchor
        for _ in range(4):
            block = block.parent
            if block is None:
                return ""
            h = block.find(["h1", "h2", "h3", "h4", "h5"])
            if h:
                text = h.get_text(" ", strip=True)
                if _MIN_TITLE <= len(text) <= _MAX_TITLE and text.lower() not in _NAV_WORDS:
                    return text
        return ""

    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        """Follow pagination across the common patterns these sites use.

        The inherited rel=next / "next"-anchor detection found nothing on
        virtually all of them — every source stopped at page 1 — because most
        use numbered links, an arrow glyph, or a WordPress /page/N/ URL rather
        than a literal "Next" anchor.
        """
        soup = BeautifulSoup(html, "lxml")

        # 1. rel="next" — the unambiguous signal when it exists.
        link = soup.find("a", rel="next") or soup.find("link", rel="next")
        href = link.get("href") if link else None
        if href and not href.startswith("#"):
            return PageRequest(urljoin(page_url, href))

        # 2. A link whose text is the next page number, inside a pagination block.
        want = str(page_number + 1)
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True) != want:
                continue
            cls = " ".join(a.get("class") or []) + " " + " ".join(
                (a.parent.get("class") or []) if a.parent else [])
            if re.search(r"pag|page", cls, re.IGNORECASE) or _PAGE_IN_URL.search(a["href"]):
                return PageRequest(urljoin(page_url, a["href"]))

        # 3. An arrow / "older posts" style control.
        for a in soup.find_all("a", href=True):
            label = a.get_text(" ", strip=True).lower()
            if label in {"next", "next page", "next »", "»", "›", ">", "older",
                         "older posts", "load more", "show more", "view more"}:
                if not a["href"].startswith("#"):
                    return PageRequest(urljoin(page_url, a["href"]))

        # 4. The URL itself is paginated — bump the number. Only attempted when
        #    the current page actually produced results, so we don't invent
        #    pages for sites that never paginate.
        m = _PAGE_IN_URL.search(page_url)
        if m and self._page_had_items:
            nxt = page_url[:m.start(2)] + str(page_number + 1) + page_url[m.end(2):]
            if nxt != page_url:
                return PageRequest(nxt)
        return None


def _load_config() -> list[dict]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Could not read %s — no generic sources registered", CONFIG_PATH)
        return []


def _build() -> list[type[GenericListingScraper]]:
    """Create and register one scraper class per configured source."""
    built = []
    for cfg in _load_config():
        name = cfg.get("name")
        url = cfg.get("url")
        if not name or not url:
            continue
        cls = type(
            f"Generic_{name}",
            (GenericListingScraper,),
            {
                "name": name,
                "display_name": cfg.get("display_name", name),
                "website": cfg.get("website") or f"https://{urlparse(url).netloc}",
                "start_url": url,
                "config": cfg,
                # Many foundation sites are JS-rendered; use a browser when one is
                # available but fall back to plain HTTP rather than failing.
                "prefer_js": bool(cfg.get("requires_js", True)),
                "enrich_details": bool(cfg.get("enrich_details", True)),
            },
        )
        built.append(register(cls))
    log.info("Registered %s configured funder sources", len(built))
    return built


GENERATED = _build()
