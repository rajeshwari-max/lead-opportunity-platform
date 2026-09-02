"""Opportunity link validation and repair.

The recurring complaint is that clicking an opportunity opens the funder's
homepage rather than the call itself. Auditing the database found three
distinct causes, none of which is "the scraper picked the wrong anchor":

  1. 5,234 DevelopmentAid rows hold a bare slug with no scheme or domain
     ("loan-51157-001-ino-flood-management..."). A browser resolves that
     relative to whatever page it is on, which is how a link in an email ends
     up somewhere unrelated. These pre-date the fix in the DevelopmentAid
     scraper and were never re-written.

  2. 943 rows carry the old donorId links ("/tenders/view/118345,118364"),
     where several rows share one URL that opens nothing in particular.

  3. 17 rows are not opportunities at all — "mailto:" and "javascript:void(0)"
     anchors scraped from page furniture, with titles like an email address or
     "Skip to main content".

A link that goes somewhere wrong is worse than a link that is absent: it costs
the reader a click and their trust in every other link in the list. So anything
that cannot be verified as a deep link is cleared, and the UI falls back to
offering the source site explicitly labelled as such.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlparse

# Several ids joined by commas — the donorIds bug. The record's own id is not
# recoverable from these, so they cannot be repaired, only dropped.
_MULTI_ID = re.compile(r"/view/\d+(?:,\d+)+/?$")

# Anchors that are page furniture rather than a destination.
_NOT_A_DESTINATION = ("mailto:", "javascript:", "tel:", "#", "data:")


def is_usable_link(url: str, website: str = "") -> bool:
    """True when the URL plausibly points at one specific opportunity."""
    u = (url or "").strip()
    if not u or u.lower().startswith(_NOT_A_DESTINATION):
        return False

    parsed = urlparse(u)
    # No scheme/host: a bare slug or a relative path that only resolves
    # correctly on the page it was scraped from.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # Domain root with no path — that is the homepage, which is exactly the
    # complaint this module exists to fix.
    path = parsed.path.strip("/")
    if not path and not parsed.query:
        return False

    if _MULTI_ID.search(u):
        return False

    # Identical to the source's own homepage.
    if website and u.rstrip("/") == website.rstrip("/"):
        return False

    return True


# The final path segment of an INDEX page, matched WHOLE. A set, not a regex
# with an optional suffix, because the regex form had a bug that quietly
# distorted every source's quality score:
#
#     calls?(-for-[\w-]+)?$
#
# `[\w-]+` is greedy, so it swallowed the entire slug. That made
#     /community-development-2/call-for-proposals-biodiversity-fund-2026-ireland
# match as a LISTING — a URL that names one specific call, in one specific
# country, in one specific year. FundsForNGOs was scoring 83% deep when the
# real figure is higher, and the dashboard was telling readers those rows open
# an index when they open the call itself.
_LISTING_SEGMENTS = frozenset({
    "funding", "fund", "funds", "grant", "grants", "tender", "tenders",
    "opportunity", "opportunities", "call", "calls", "call-for-proposals",
    "calls-for-proposals", "rfp", "rfps", "rfq", "rfqs", "proposals",
    "program", "programs", "programme", "programmes", "apply", "applications",
    "how-to-apply", "search", "search-results", "grants-funding",
    "funding-opportunities", "open-call", "open-calls", "notices", "listing",
})
_SEARCH_QUERY = re.compile(r"[?&](s|q|search|keyword)=", re.IGNORECASE)

# A slug long or structured enough to name one particular thing.
#
# This exists for single-segment URLs. Treating every depth-1 path as an index
# was wrong in the same direction: NGOBOX publishes each grant at
#     /full_grant_announcement_Applications-Invited-for-2026-Civil-Society-…
# which is one segment and unmistakably one grant, and the source scored 0%
# deep links — reported as "more than half the links open a listing" when in
# fact none of them did.
def _looks_specific(segment: str) -> bool:
    seg = (segment or "").strip().lower()
    if not seg or seg in _LISTING_SEGMENTS:
        return False
    if seg.endswith((".php", ".aspx", ".html", ".htm")) and len(seg) < 30:
        return False          # a bare script name: listing.php, index.aspx
    # Either long, or built from several words — both mean "this names a thing".
    return len(seg) >= 25 or (seg.count("-") + seg.count("_")) >= 3


def link_kind(url: str) -> str:
    """'deep' for a per-opportunity page, 'listing' for an index/search page.

    Some sources genuinely never publish a per-call URL — DevNetJobsIndia lists
    every RFP on one .aspx page, SNF exposes only a search view. For those, the
    listing page is the sole route to the opportunity, so clearing it would
    remove the only way in. Labelling it is the honest option: the reader is
    told they will land on a list and still need to find the row, rather than
    discovering that after the click.

    Limits of doing this from the URL alone: a programme landing page like
    /programs/global-foods/ is indistinguishable from a real call at
    /programs/water-fund-2026/. Only fetching the page settles those, which is
    what scripts/check_links.py is for.
    """
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    path = (parsed.path or "").rstrip("/")
    segments = [seg for seg in path.split("/") if seg]
    last = segments[-1] if segments else ""

    if _SEARCH_QUERY.search(u):
        return "listing"
    # The last segment IS an index name ("/funding", "/grants", "/apply").
    if last.lower() in _LISTING_SEGMENTS:
        return "listing"
    if not segments:
        return "listing"          # bare domain
    if len(segments) <= 1 and not parsed.query and not _looks_specific(last):
        return "listing"
    return "deep"


# Some boards expose each record twice: a machine endpoint that serves a file
# download, and the human page. Scrapers naturally grab whichever anchor is in
# the markup, and on the UN Partner Portal that is the export API — clicking it
# in the dashboard downloads a blob instead of opening the call. Same record,
# so this is a rewrite, not a deletion.
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(https://(?:www\.)?unpartnerportal\.org)/api/public/export/projects/(\d+)/?$",
                re.IGNORECASE), r"\1/landing/opportunities/\2/"),
    (re.compile(r"^(https://(?:www\.)?unpartnerportal\.org)/api/public/projects/(\d+)/?$",
                re.IGNORECASE), r"\1/landing/opportunities/\2/"),
)


def canonical_link(url: str) -> str:
    """Swap a known machine/download endpoint for its human-readable page."""
    u = (url or "").strip()
    for pattern, repl in _REWRITES:
        new = pattern.sub(repl, u)
        if new != u:
            return new
    return u


def repair_links() -> dict:
    """Clear unusable opportunity_url values on existing rows.

    Idempotent. Returns a small summary so the effect is visible in the log
    rather than being a silent mass update.
    """
    import logging

    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    stats = {"checked": 0, "cleared": 0, "kept": 0, "rewritten": 0}

    # Chunked — see services/backfill.py. `.scalars()` streams the rows but the
    # session keeps every one it has yielded, so peak memory was still the whole
    # table.
    from app.services.backfill import iter_opportunities

    with session_scope() as db:
        for opp in iter_opportunities(db):
            stats["checked"] += 1
            if not (opp.opportunity_url or "").strip():
                continue
            fixed = canonical_link(opp.opportunity_url)
            if fixed != opp.opportunity_url:
                opp.opportunity_url = fixed
                stats["rewritten"] += 1
            if is_usable_link(opp.opportunity_url, opp.website):
                stats["kept"] += 1
            else:
                opp.opportunity_url = ""
                stats["cleared"] += 1

    if stats["cleared"] or stats["rewritten"]:
        log.info("Link repair: cleared %s, rewritten %s, of %s checked",
                 stats["cleared"], stats["rewritten"], stats["checked"])
    return stats


# Titles that are navigation or boilerplate, not the name of a call. Grown from
# what actually reached the live dashboard: "Skip to main content" appeared 25
# times under Pfizer Foundation alone, and Clean Air Fund's three "open" rows
# were "Navigation breadcrumbs", "Navigation breadcrumbs" and "Related case
# study" — a page with no calls on it at all.
FURNITURE_TITLES = frozenset({
    "skip to main content", "skip to content", "skip navigation", "read more",
    "apply now", "click here", "learn more", "find out more", "subscribe",
    "menu", "home", "navigation breadcrumbs", "breadcrumbs", "navigation",
    "related case study", "related case studies", "case study", "search",
    "contact us", "contact", "privacy policy", "cookie policy", "cookies",
    "terms and conditions", "terms of use", "newsletter", "sign up", "log in",
    "about us", "about", "our work", "our impact", "news", "events", "blog",
    "publications", "resources", "careers", "donate", "back to top",
    "view all", "see all", "next", "previous", "share", "download",
})

_EMAIL_TITLE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)

# Structural furniture, matched by SHAPE rather than by exact string.
#
# FURNITURE_TITLES above is an exact-match English set, and an exact-match set
# can only ever catch furniture it has already met. The 2026-08-29 database had
# 98 rows it had not met:
#
#     53  "Overslaan en naar inhoud gaan"        Dutch "skip to main content"
#     20  'Search results for: "grants" Clear Search'
#     15  "(E: 404) Content Not Found"
#      5  "Increase Font Size"
#      5  "Browse by Focus Area"
#
# Each is an instance of a class — a translated skip link, a search-result
# header, an error page, an accessibility control, a nav label — and the class
# is what these patterns match. Anchored deliberately: a real call named
# "Increasing Font Accessibility in Rural Schools" must survive, so nothing here
# matches a bare substring.
_FURNITURE_PATTERNS = (
    # Error and empty-state pages. Any source can serve one, and it will be
    # scraped with whatever the template's heading happens to be.
    re.compile(r"^\(?\s*(e[:\s-]*)?\b(400|401|403|404|410|500|502|503)\b.*"
               r"(not found|error|forbidden|unavailable|denied)", re.I),
    re.compile(r"^(page|content|file|document)\s+not\s+found\b", re.I),
    re.compile(r"^(oops|sorry)[!,.\s]", re.I),
    re.compile(r"^access\s+denied\b", re.I),
    # Search-result headers, which are a description of a query, not a call.
    re.compile(r"^search\s+results?\b", re.I),
    re.compile(r"^showing\s+\d+", re.I),
    re.compile(r"^\d+\s+results?\s+(found|for)\b", re.I),
    re.compile(r"\bclear\s+(search|filters?|all)\s*$", re.I),
    # Anchored to the WHOLE title. Unanchored, "^no results" ate
    # "No Results Left Behind: Evaluation Capacity Grant" — a real grant.
    re.compile(r"^no\s+results?(\s+(found|match\w*))?\s*[.!]?$", re.I),
    # Accessibility and display controls.
    re.compile(r"^(increase|decrease|reset|change)\s+(the\s+)?"
               r"(font|text)\s*size\b", re.I),
    re.compile(r"^(font|text)\s*size\b", re.I),
    re.compile(r"^(high|low)\s+contrast\b", re.I),
    re.compile(r"^(dark|light)\s+mode\b", re.I),
    # Faceting and sorting labels.
    re.compile(r"^(browse|filter|sort|search|view|explore)\s+by\b", re.I),
    # Same: unanchored, this ate "Show More Women in STEM — Innovation
    # Challenge". A pagination control IS the entire title; a call is not.
    re.compile(r"^(load|show)\s+more(\s+(results?|items?))?\s*[.!]?$", re.I),
    re.compile(r"^page\s+\d+(\s+of\s+\d+)?$", re.I),
    # Cookie and consent banners.
    re.compile(r"^(accept|reject|manage)\s+(all\s+)?cookies?\b", re.I),
    re.compile(r"^cookie\s+(settings|preferences|consent)\b", re.I),
)

# Skip links in the languages these sources actually publish in. A blocklist
# can only catch what it has met, so this is the honest scope: the five
# European languages present in the current source list, not a claim to
# handle every language.
_SKIP_LINK_TITLES = frozenset({
    "overslaan en naar inhoud gaan",        # nl — 53 rows in the 2026-08-29 db
    "naar de inhoud",
    "aller au contenu principal", "aller au contenu",       # fr
    "zum hauptinhalt springen", "zum inhalt springen",      # de
    "saltar al contenido principal", "ir al contenido",     # es
    "salta al contenuto principale",                        # it
    "pular para o conteudo principal",                      # pt
})

# Anchors that jump within a page rather than to an opportunity.
_SKIP_FRAGMENTS = {"main-content", "content", "main", "top", "skip", "nav"}


def is_furniture(title: str, url: str = "") -> bool:
    """True when this row is page furniture rather than an opportunity.

    Deliberately conservative: it matches whole titles, never substrings, so a
    real call named "Apply now for the 2026 Water Fund" is untouched.
    """
    t = " ".join((title or "").split()).strip().lower().strip(":-–—|")
    if not t or len(t) < 8:
        return True
    if t in FURNITURE_TITLES or t in _SKIP_LINK_TITLES or _EMAIL_TITLE.match(t):
        return True
    if any(p.search(t) for p in _FURNITURE_PATTERNS):
        return True
    # "Skip to main content" scraped with the page's own skip-link href.
    if url and "#" in url and url.rsplit("#", 1)[-1].lower() in _SKIP_FRAGMENTS:
        return t in FURNITURE_TITLES
    return False


def junk_rows() -> list[tuple[int, str, str, str]]:
    """Rows that look like scraped page furniture rather than opportunities."""
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    from app.services.backfill import iter_opportunities

    out = []
    with session_scope() as db:
        for opp in iter_opportunities(db):
            if is_furniture(opp.title or "", opp.opportunity_url or ""):
                out.append((opp.id, opp.source_website, opp.title or "",
                            opp.opportunity_url or ""))
    return out


def purge_junk_rows() -> int:
    """Delete page-furniture rows. Returns how many were removed.

    These were previously only *reported* by junk_rows(), and nothing called
    even that — so "Skip to main content" and "Navigation breadcrumbs" sat in
    the live dashboard as though they were fundable calls. They carry no
    information a human would ever want, so unlike a closed opportunity there is
    nothing to archive: deleting is the honest outcome, and the count is logged
    so the removal is never silent.
    """
    import logging

    from sqlalchemy import delete, select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    with session_scope() as db:
        from app.services.backfill import iter_opportunities

        # Only the ids of the rows that ARE junk are kept — previously every
        # row was hydrated to build this list.
        ids = [opp.id for opp in iter_opportunities(db)
               if is_furniture(opp.title or "", opp.opportunity_url or "")]
        if ids:
            for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
                db.execute(delete(Opportunity).where(Opportunity.id.in_(chunk)))
            log.info("Removed %s page-furniture row(s) that were not opportunities", len(ids))
    return len(ids)

# ---------------------------------------------------------------- fallbacks
# Where to send someone when we have no direct link to the listing itself.
#
# The alternative — showing "no direct link" and a homepage — makes the row a
# dead end: the reader has to retype the title into the site's own search. That
# is exactly the manual work this tool exists to remove, so a search URL that
# lands on (or beside) the opportunity is strictly better even though it is not
# the listing itself.
_SEARCH_TEMPLATES: dict[str, str] = {
    "DevelopmentAid":     "https://www.developmentaid.org/tenders/search?keywords={q}",
    "UNDP Procurement":   "https://procurement-notices.undp.org/search.cfm?keywords={q}",
    "UN Partner Portal":  "https://www.unpartnerportal.org/landing/opportunities/?search={q}",
    "World Bank":         "https://projects.worldbank.org/en/projects-operations/procurement?searchTerm={q}",
    "ADB Tenders":        "https://www.adb.org/projects/tenders?searchstax%5Bquery%5D={q}",
    "FundsForNGOs":       "https://www2.fundsforngos.org/?s={q}",
    "Bond UK":            "https://www.bond.org.uk/search/?keywords={q}",
    "NGOBOX":             "https://ngobox.org/search.php?q={q}",
    "GrantWatch Intl":    "https://www.grantwatch.com/cat/search.php?keyword={q}",
    "DevNetJobsIndia":    "https://www.devnetjobsindia.org/rfp_assignments.aspx",
}


def search_link(title: str, website: str = "", source_website: str = "",
                category: str = "") -> str:
    """A URL that will find this opportunity when we have no direct one.

    Order of preference:
      1. the source's own search page, which knows about its own listings
      2. a web search restricted to the source's domain
      3. the funder's homepage, as an absolute last resort
    """
    q = quote_plus(" ".join((title or "").split())[:180])
    if not q:
        return (website or "").strip()

    source = (source_website or "").strip()
    # DevelopmentAid keeps grants and tenders in separate catalogues that do not
    # search each other. The single template sent every fallback to the TENDER
    # search, so a grant's "find it" link searched a catalogue that cannot
    # contain it and always came back empty.
    if source == "DevelopmentAid":
        section = "grants" if str(category or "").lower().startswith("grant") else "tenders"
        return f"https://www.developmentaid.org/{section}/search?keywords={q}"

    template = _SEARCH_TEMPLATES.get(source)
    if template:
        return template.format(q=q) if "{q}" in template else template

    # NO web-search fallback, and no homepage fallback either.
    #
    # This used to return https://duckduckgo.com/?q=site:<domain>+<title>. The
    # intention was decent — better than a dead end — but the effect on the
    # dashboard was that rows opened a search engine instead of an opportunity,
    # which is what "these links go to some random page" meant. A search result
    # is not a lead: the reader still has to find the call, decide whether the
    # top hit even is the call, and often discover it never existed.
    #
    # The real fix is upstream: ScraperManager._ingest now refuses to store a
    # row that has no link to the call itself (LOP_REQUIRE_USABLE_LINK), so this
    # branch should never be reached for newly scraped data. Returning "" makes
    # any remaining row visibly linkless instead of dressing it up, and
    # scripts/clean_dashboard.py removes the ones already in the database.
    return ""


def resolve_link(opportunity_url: str, website: str, source_website: str,
                 title: str, category: str = "") -> tuple[str, str]:
    """(url, kind) where kind is "direct", "listing" or "search".

    Every row gets something clickable. `kind` is returned so the UI can be
    honest about which it is — a search result presented as the listing itself
    would be worse than the dead end it replaces.

    "listing" is the case this used to get wrong. link_kind() has always been
    able to tell a per-call page from a section index, but resolve_link never
    consulted it, so a URL like https://www.idrc.ca/en/funding or a funder's
    generic "apply for a grant" page was handed to the reader labelled
    "direct" — they clicked expecting the call and landed on a menu. On a live
    snapshot that was 584 of 7,772 rows. Nothing is hidden: the same link is
    still offered, it is just no longer described as the opportunity itself.
    """
    if is_usable_link(opportunity_url, website):
        kind = "direct" if link_kind(opportunity_url) == "deep" else "listing"
        return opportunity_url, kind
    fallback = search_link(title, website, source_website, category)
    # "none" is a real answer. Before, every row got *something* clickable, and
    # a row with nothing to click was given a web search — which reads as a
    # working link right up until the reader follows it. A row that reaches here
    # now should not exist at all (see _ingest and clean_dashboard.py); if one
    # does, the UI can say so rather than sending someone on an errand.
    return (fallback, "search") if fallback else ("", "none")

