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
from app.scrapers.registry import SCRAPER_REGISTRY, register
from app.services.links import is_furniture

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
    # Institutional site chrome seen on the World Bank / ADB / UN boards. These
    # were being stored as opportunities: "Skip to main content" and
    # "Procurement Policy" are both plain <a> tags of a plausible length inside
    # the listing region, so nothing else rejected them.
    "skip to main content", "skip to content", "projects & operations",
    "projects and operations", "procurement notices", "procurement policy",
    "procurement", "tenders", "opportunities", "notices", "results",
    "operational procurement", "business opportunities", "how to apply",
    "guidance", "documents", "publications", "data", "reports", "help",
    "site map", "disclaimer", "fraud & corruption", "integrity",
}
# Titles that are a bare section heading rather than a call. Anchored so a real
# call that merely mentions the word ("Procurement of Assistive Technology…")
# survives.
_SECTION_TITLE = re.compile(
    r"^(skip\s|projects?\b.{0,3}operations?$|procurement\s*(notices?|policy|"
    r"guidance|documents?)?$|tenders?$|opportunit(y|ies)$|notices?$|"
    r"grants?$|about\b|browse\b|filter\b|sort\b|all\s+\w+$)",
    re.IGNORECASE)
# Navigation paths. Each alternative must fill a WHOLE path segment — the
# trailing (?=[/?#.]|$) is what makes that true, and it is not optional.
#
# Without it these matched as substrings, and the damage was invisible: "jobs?"
# matched /jobdescription.aspx, so every DevNetJobsIndia per-item link
# (/jobdescription.aspx?job_id=300671) was classified as site navigation and
# dropped. What survived were the rows whose link was the listing page itself —
# 86 different RFPs all pointing at rfp_assignments.aspx. The bug did not empty
# the source, it inverted it: real links discarded, useless ones kept.
# The same trap was waiting for /tagline, /teamwork, /mediation, /pressure and
# /categories on any other site.
_NAV_HREF = re.compile(
    r"/(about|contact|privacy|terms|cookie|login|signin|register|subscribe|careers?|"
    r"jobs?|press|media|team|faq|sitemap|accessibility|donate|newsletter|tag|category|"
    r"author|feed)(?=[-/?#.]|$)|/wp-|#$", re.IGNORECASE)

# /page/2/, ?page=2, ?paged=2, ?p=2 — the number is group(2) so it can be bumped.
_PAGE_IN_URL = re.compile(r"(/page/|[?&](?:page|paged|pg|p)=)(\d+)", re.IGNORECASE)

# A bare date, for cells that hold nothing else. Group 1 matches the same shapes
# _DEADLINE captures, so both feed deadline_raw identically.
_DATE_ONLY = re.compile(
    r"(\d{1,2}\s+\w{3,9}\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
)


def _same_page(url: str, page_url: str) -> bool:
    """True when `url` is just the listing page again.

    Compared on scheme+host+path only. Query and fragment are deliberately
    ignored: ?page=2, ?sort=date and #results all still resolve to the index,
    never to one opportunity.
    """
    a, b = urlparse(url), urlparse(page_url)
    return (a.netloc.replace("www.", "") == b.netloc.replace("www.", "")
            and a.path.rstrip("/") == b.path.rstrip("/"))


# Field labels that get glued onto the value when a listing is a table and the
# anchor text swallows the header cell. UNDP Procurement produced 1,274 rows
# where the title read "Title RFP-074-IND-2026-Selection of a Service Provider…"
# — and because the same notice also appeared without the prefix, one URL ended
# up with three near-identical rows (1,274 rows over just 858 URLs).
_FIELD_LABEL = re.compile(
    r"^(title|subject|name|description|reference|ref\.?\s*no\.?|notice|"
    r"tender|opportunity)\s*[:\-–]?\s+(?=\S)",
    re.IGNORECASE,
)


def strip_field_label(title: str) -> str:
    """Drop a leading table-header label from a scraped title.

    Only strips when real text follows, so a listing genuinely called "Tender"
    or "Notice" is left alone rather than being emptied.
    """
    out = _FIELD_LABEL.sub("", (title or "").strip(), count=1).strip()
    return out or (title or "").strip()


_DEADLINE = re.compile(
    r"(?:deadline|closing date|closes?|apply by|due|submission[s]? (?:by|due)|"
    r"expires?)\s*[:\-–]?\s*"
    r"(\d{1,2}\s+\w{3,9}\s+\d{4}|\w{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
    re.IGNORECASE)


def _says_rolling(block_text: str) -> bool:
    """True only when the page itself states there is no closing date.

    Delegates to the same DeadlineParser.is_ongoing() the ingest pipeline uses,
    so "rolling", "open-ended", "no fixed deadline" and "until filled" mean the
    same thing at scrape time and at save time. Two independent notions of
    "ongoing" is how a row ends up live in one layer and closed in the other.
    """
    from app.services.deadline_parser import DeadlineParser

    return DeadlineParser.is_ongoing(block_text or "")


# ---------------------------------------------------------------- positive test
# The checks above are a BLOCKLIST: they reject navigation we have seen before.
# That can only ever catch known junk, so every new funder site contributes its
# own vocabulary of chrome — Clean Air Fund's "Our work" / "Insights" / "Funded
# projects" sailed through and were stored as three opportunities on a page that
# has no open calls at all.
#
# So a link must now also show POSITIVE evidence of being a funding call. Any
# one of these is enough, which keeps it forgiving:
#   * funding vocabulary in the title
#   * a deadline in the surrounding block
#   * a currency amount in the surrounding block
#   * a URL path that names a funding record
#
# A page of pure navigation supplies none of them.
_FUNDING_WORDS = re.compile(
    r"\b(grant|grants|funding|fund|fellowship|scholarship|bursary|prize|award|"
    r"call\s+for|request\s+for|rfp|rfq|rfa|eoi|expression\s+of\s+interest|"
    r"tender|bid|proposal|solicitation|programme|program|scheme|"
    r"applications?\s+(open|invited|are)|apply\s+(now|for|by)|"
    r"invit\w+|open\s+call|competition|challenge|accelerator|incubator)\b",
    re.IGNORECASE,
)
_FUNDING_HREF = re.compile(
    r"/(grant|grants|funding|fund|opportunit|call|calls|tender|rfp|rfq|award|"
    r"fellowship|scholarship|programme|program|apply|competition|challenge)s?[/\-_]",
    re.IGNORECASE,
)
_AMOUNT_NEAR = re.compile(
    r"(?:[$£€₹]|\bUSD\b|\bEUR\b|\bGBP\b|\bINR\b|\bAUD\b|\bCAD\b)\s?\d",
    re.IGNORECASE,
)


def looks_like_funding(title: str, href: str, block_text: str) -> bool:
    """Positive evidence that this link is a funding call, not site furniture."""
    if _FUNDING_WORDS.search(title or ""):
        return True
    if _FUNDING_HREF.search(href or ""):
        return True
    if block_text:
        if _DEADLINE.search(block_text):
            return True
        if _AMOUNT_NEAR.search(block_text):
            return True
        # The title may be bland ("Youth Resilience in Coastal Kenya") while the
        # block around it is clearly a funding card.
        if _FUNDING_WORDS.search(block_text):
            return True
    return False


def clean_title(text: str) -> str:
    """Strip the field label some boards render inside the link text.

    UNDP's procurement board marks up each row as "Title <the actual title>",
    so every one of its 588 rows was stored with a leading "Title ". Also drops
    "Deadline:"-style trailing labels left behind by the same markup.
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = re.sub(r"^(title|name|subject|notice)\s*[:\-–]?\s+", "", t, flags=re.IGNORECASE)
    return t.strip(" :-–|")


def _looks_like_opportunity(text: str, href: str) -> bool:
    t = clean_title(text)
    if not (_MIN_TITLE <= len(t) <= _MAX_TITLE):
        return False
    if t.lower() in _NAV_WORDS or _SECTION_TITLE.match(t):
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
        rejected = 0          # links that looked like site furniture, not funding
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
                title = strip_field_label(clean_title(a.get_text(" ", strip=True)))
                if not _looks_like_opportunity(title, href):
                    # Very common card layout: the title lives in a heading and
                    # the link is a separate "Learn More" / image anchor, so the
                    # anchor's own text is useless. Fall back to the nearest
                    # heading in the same block. (Gates Grand Challenges lists
                    # its open calls exactly this way, and we saw none of them.)
                    title = strip_field_label(clean_title(self._heading_title(a)))
                    if not title or _NAV_HREF.search(href):
                        continue
                url = urljoin(page_url, href)
                if urlparse(url).netloc.replace("www.", "") not in \
                        urlparse(self.website).netloc.replace("www.", ""):
                    continue          # off-site link (social, partner, funder logo)
                # A link back to the page we are parsing is not a link to one
                # opportunity — it is the "back to listings" / pagination /
                # filter link. DevNetJobsIndia stored 86 different RFPs all
                # pointing at rfp_assignments.aspx, the very page they were
                # scraped from, so every one of those rows opened the index
                # instead of the call.
                if _same_page(url, page_url):
                    rejected += 1
                    continue
                if url in seen:
                    continue
                seen.add(url)

                # Deadline, if the surrounding block mentions one.
                block = a.find_parent(["article", "li", "div", "tr"]) or a
                text = block.get_text(" ", strip=True)[:1200]
                m = _DEADLINE.search(text)

                # Boards that put the closing date in its own cell rather than
                # in prose ("Deadline: 12 Sep 2026") are invisible to the regex
                # above, because there is no label next to the date. Every one
                # of UNDP Procurement, World Bank, UN Partner Portal and ADB
                # came out with 0% deadlines for exactly this reason — and a row
                # with no deadline becomes a permanently-open "Ongoing" that
                # nothing can ever expire. Point "deadline_selector" at the cell
                # in sources.json and the date is read directly.
                dl_sel = self.config.get("deadline_selector")
                if dl_sel:
                    cell = block.select_one(dl_sel)
                    if cell is not None:
                        raw = (cell.get("datetime") or cell.get("title")
                               or cell.get_text(" ", strip=True) or "").strip()
                        if raw:
                            m = _DATE_ONLY.search(raw) or m

                # Positive test, applied last because it needs the block text.
                # Without it a page with no open calls still yields whatever
                # links it happens to contain — which is how Clean Air Fund
                # produced three navigation entries from a page with nothing on
                # it. Set "require_funding_signal": false in sources.json for a
                # source whose listings genuinely carry no funding vocabulary.
                if self.config.get("require_funding_signal", True) and \
                        not looks_like_funding(title, href, text):
                    rejected += 1
                    continue

                # Navigation and boilerplate, rejected before it becomes a row.
                # The funding-signal test above passes these, because the block
                # text around them IS a funding page — which is exactly how
                # "Skip to main content" (25 live rows under Pfizer Foundation
                # alone) and Clean Air Fund's "Navigation breadcrumbs" /
                # "Related case study" reached the dashboard as fundable calls.
                if is_furniture(title, url):
                    rejected += 1
                    continue

                items.append(RawOpportunity(
                    title=title[:500],
                    organization=self.display_name,   # the funder IS the source here
                    summary=text[:1000] if len(text) > len(title) + 40 else "",
                    deadline_raw=(m.group(1) if m else "")[:64],
                    opportunity_url=url,
                    website=self.website,
                    source_website=self.display_name,
                    category_hint=Category.GRANT,     # hint only; classifier decides
                    # assume_active means "the source SAYS this has no closing
                    # date" — rolling, open-ended, until filled. It does NOT mean
                    # "our regex failed to find one".
                    #
                    # This line used to read `assume_active=not bool(m)`, which
                    # conflated the two, and it is why closed calls sat on the
                    # dashboard as permanently "Ongoing". Every row whose
                    # deadline the regex missed was flagged as a rolling call,
                    # and _ingest treats a rolling call as never expiring — so
                    # nothing downstream could ever close it. A call that shut in
                    # March was still shown as open in August.
                    #
                    # Now only an explicit statement counts. A row we simply
                    # could not read a date from is UNKNOWN: it stays live and is
                    # retired by audit_deadlines() once its source stops listing
                    # it (LOP_ONGOING_MAX_AGE_DAYS).
                    assume_active=_says_rolling(text),
                ))
        self._page_had_items = bool(items)
        if rejected:
            # Visible on purpose. A page that yields 0 kept and 40 rejected is a
            # page with no open calls; 0 kept and 0 rejected means the parser
            # found no links at all, which is a different problem. Reporting one
            # number would conflate them.
            log.info("[%s] kept %s, rejected %s link(s) with no funding signal",
                     self.name, len(items), rejected)
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
        # 0. An explicit template from sources.json wins over any guessing.
        #    Auto-detection only knows the common shapes (?page=N, /page/N/), and
        #    misses anything else — ADB paginates with searchstax%5Bpage%5D=N, so
        #    every run stopped at page 1 while reporting success. A template also
        #    lets a source declare its English URL, since several of these boards
        #    default to the local language.
        template = getattr(self, "page_url_template", "")
        if template and self._page_had_items:
            # Two pagination dialects, because sites disagree about what the
            # number in the URL means:
            #   {page}   1-based page index   (?page=2 = the second page)
            #   {offset} 0-based row offset   (?os=10  = the second page of 10)
            # World Bank uses the second. It was configured with {page}, so the
            # crawler asked for os=2, os=3 … — sliding the window down by a
            # single row each time. Nine of every ten results were repeats, the
            # stale-page counter tripped almost immediately, and the source
            # stopped after 38 rows while reporting success.
            size = int(self.config.get("page_size") or 10)
            nxt = template.format(page=page_number + 1, offset=page_number * size)
            if nxt != page_url:
                return PageRequest(nxt)
            return None

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
    """Create and register one scraper class per configured source.

    A name that already has a bespoke module wins and is skipped here.

    This is not defensive tidiness, it is a bug fix. `register()` is a plain
    dictionary assignment, and scrapers/__init__.py imports this module LAST —
    so a sources.json entry sharing a name with a hand-written scraper silently
    replaced it. The comment in __init__.py claimed the opposite was true.
    UN Partner Portal is exactly that case: it had a config entry, so adding
    unpp.py would have changed nothing at all and the generic HTML parser would
    have kept running against a React app that renders no listings in its HTML.
    """
    built = []
    for cfg in _load_config():
        name = cfg.get("name")
        url = cfg.get("url")
        if not name or not url:
            continue
        if name in SCRAPER_REGISTRY:
            log.info("Source %r has a bespoke scraper (%s) — ignoring its "
                     "sources.json entry", name,
                     SCRAPER_REGISTRY[name].__module__)
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
                # Optional "…&page={page}" template for sources whose pagination
                # the generic detector can't see.
                "page_url_template": cfg.get("page_url", ""),
                "stale_page_streak_override": cfg.get("stale_page_streak", None),
                # Proof-of-render hooks for XHR-driven listings (see BaseScraper).
                "render_wait_selector": cfg.get("render_wait_selector", ""),
                "render_wait_text": cfg.get("render_wait_text", ""),
                # "curated": true for a config source that is a dedicated
                # call/tender board rather than a general website. Default
                # False — these are exactly the link-harvested sources the
                # opportunity gate exists to police.
                "curated": bool(cfg.get("curated", False)),
            },
        )
        built.append(register(cls))
    log.info("Registered %s configured funder sources", len(built))
    return built


GENERATED = _build()
