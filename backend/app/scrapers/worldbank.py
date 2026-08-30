"""World Bank procurement notices — read the API the site itself reads.

Why this replaced the sources.json entry
----------------------------------------
World Bank looked like the best-configured source in the platform. It was the
only one with a pagination template, and the right dialect too:

    "page_url": ".../opportunities?lang=en&os={offset}",  "page_size": 20

`scripts/probe_pagination.py` showed that template does nothing:

    page 1: 34 listing(s)
    [no] ?os=34      ...&os=34    same rows as page 1 (100% overlap)
    ... every one of 18 candidates: same rows as page 1
    the page loads its listings from an API:
      https://search.worldbank.org/api/v2/procnotices?format=json&fct=...
    => SINGLE PAGE

The `os` parameter is not part of that page's URL contract. The listing is
rendered client-side from `search.worldbank.org/api/v2/procnotices`, and the
paging lives in THAT request — so no query parameter on the page URL could ever
have worked, and the source had been returning its first 34 rows while looking
perfectly configured. A template that is present is not a template that works.

Two things follow, and the second is the reason this module is short.

  1. Scrape the API, not the rendered page. It is the same data one layer
     earlier, with no client-side rendering to wait for.
  2. Because it is a plain JSON endpoint, this needs no browser at all. Setting
     `requires_js = False` and parsing JSON inside `parse_listing()` reuses
     every part of BaseScraper — retries with backoff, rate limiting, the
     pagination loop, the repeated-content guard — instead of reimplementing
     them in a custom `crawl()`. The only unusual thing here is that the "html"
     handed to the parser is JSON.

Field names are not hard-coded
------------------------------
Every field is read through a list of candidate names, and the first run LOGS
the keys it actually saw. The first live run turned the guess into a fact:

    bid_description · bid_reference_no · contact_* · id · notice_lang_name
    notice_status · notice_text · notice_type · noticedate · procurement_group
    procurement_method_code · procurement_method_name · project_ctry_name
    project_id · project_name · submission_date · submission_deadline_date
    submission_deadline_time

...and it also exposed three faults that only real data could show. See
DEADLINE, AWARDS and SCALE below.

DEADLINE — the first version read `submission_date` before
`submission_deadline_date`, and every one of 300 rows came back with the same
date: yesterday. `submission_date` is when the notice was PUBLISHED. The
deadline is `submission_deadline_date`, and the priority is now that way round.
A uniform value across hundreds of rows is the shape of a wrong field, not of a
real coincidence.

AWARDS — most records are `notice_type: Contract Award`: contracts already
given to someone. They are not opportunities, nobody can bid on them, and they
would have flooded the dashboard. Open notice types are kept and the rest are
dropped, with the counts logged so the filter can be checked rather than
trusted.

SCALE — the API reports **416,361** notices. That is the entire historical
archive, not the open ones: at 100 per page it is 4,164 pages, and the platform
default cap of 2,000 pages would have walked 200,000 records of mostly closed
history. The API returns newest-first, so this walks a bounded recent window
instead.
"""
from __future__ import annotations

import json
import logging
import re

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.services.notice_types import record_type_for
from app.services.source_manifest import RecordType
from app.scrapers.base_scraper import BaseScraper, PageRequest
from app.scrapers.registry import register

log = logging.getLogger("scraper")

API = "https://search.worldbank.org/api/v2/procnotices"
SITE = "https://projects.worldbank.org"
# The human page for one notice. Used only when the record carries no URL of
# its own — services/links.py labels a listing-shaped link honestly, so a
# constructed detail URL is better than none but worse than the real one.
DETAIL_URL_TEMPLATE = f"{SITE}/en/projects-operations/procurement-detail/{{id}}"

# 100 keeps the request count low without asking the API for an unusual page
# size. The site's own call uses far less; this is a scrape, not a UI.
ROWS = 100

# Where the list of records sits in the response, across the shapes this API
# family uses.
_ROW_KEYS = ("procnotices", "documents", "results", "rows_data", "data")
_TOTAL_KEYS = ("total", "totalRecords", "numFound", "count")

# Notice types that are still open to bid on. Anything else — above all
# "Contract Award" — is a record of a decision already taken.
#
# Matched as a substring, case-insensitively, because the API spells these out
# in prose ("Request for Expressions of Interest", "Invitation for Bids") and a
# whole-string list would miss every variant.
OPEN_NOTICE_TYPES = (
    "invitation for bid", "invitation to bid", "request for bid",
    "request for expressions of interest", "expression of interest",
    "request for proposal", "request for quotation",
    "general procurement notice", "specific procurement notice",
    "prequalification", "invitation for prequalification",
    "consultant", "procurement notice", "invitation",
)
# Notice types that are definitively finished, checked first so a string like
# "Contract Award Notice" cannot match "procurement notice" above.
CLOSED_NOTICE_TYPES = (
    "award", "cancel", "annul", "abandon",
)

# procurement_group arrives as a two-letter code. "CW" in the sector column is
# not information; "Civil Works" is.
PROCUREMENT_GROUPS = {
    "CW": "Civil Works", "GO": "Goods", "CS": "Consultant Services",
    "NC": "Non-Consulting Services", "CQ": "Consultant Qualification",
    "IC": "Individual Consultant", "SE": "Services", "TR": "Training",
}

# One log line per run, so the real schema is recorded rather than assumed.
_SCHEMA_LOGGED = False


def is_open_notice(notice_type: str) -> bool:
    """True when this notice is something a bidder can still respond to."""
    t = (notice_type or "").strip().lower()
    if not t:
        return True          # unlabelled: keep it and let the deadline decide
    if any(w in t for w in CLOSED_NOTICE_TYPES):
        return False
    return any(w in t for w in OPEN_NOTICE_TYPES)


def _first(record: dict, *names, default=""):
    for n in names:
        v = record.get(n)
        if v not in (None, "", [], {}):
            return v
    return default


def _text(value) -> str:
    """Flatten the shapes this API uses for a single value."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for k in ("name", "value", "label", "cdata!", "cdata"):
            if isinstance(value.get(k), str):
                return value[k].strip()
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(p for p in (_text(v) for v in value) if p)
    return str(value or "").strip()


def _date(value) -> str:
    """An ISO date out of whatever the API gives. '' when there isn't one.

    Timestamps arrive as 2026-09-30T00:00:00Z; the date half is what the
    deadline parser wants, and keeping the time would only add a timezone
    question nobody asked.
    """
    raw = _text(value)
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else raw[:64]


def _rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _ROW_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        # Some World Bank endpoints key records by id instead of listing them.
        if isinstance(value, dict):
            return [r for r in value.values() if isinstance(r, dict)]
    return []


def _total(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in _TOTAL_KEYS:
        v = payload.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


@register
class WorldBankScraper(BaseScraper):
    name = "world_bank"
    display_name = "World Bank"
    website = SITE
    start_url = f"{API}?format=json&rows={ROWS}&os=0"
    requires_js = False          # a JSON endpoint; a browser would add nothing
    prefer_js = False
    enrich_details = False       # the record carries every field we store
    # A procurement notice board: every record is an opportunity by
    # construction, so rows skip the vocabulary test in opportunity_gate.py.
    curated = True
    # NOT "walk to the end": the end is 416,361 notices, nearly all of them
    # closed history. The API returns newest-first, so a bounded window of the
    # most recent notices is where every open one lives. 60 pages x 100 = the
    # 6,000 most recently published notices.
    #
    # Raising this does not find more OPEN tenders, it finds older closed ones.
    # If open notices are being missed, the fix is a server-side filter on
    # notice_type, not a bigger number here.
    max_pages = 60
    stale_page_streak_override = 0

    # ------------------------------------------------------------------ parse
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """`html` is the JSON body. See the module docstring for why."""
        global _SCHEMA_LOGGED
        try:
            payload = json.loads(html)
        except ValueError:
            head = (html or "")[:300].replace("\n", " ")
            log.error("[%s] the API did not return JSON. First 300 chars: %s",
                      self.name, head)
            return []

        records = _rows(payload)
        if not records:
            log.error("[%s] 200 OK but no record list in the response. "
                      "Top-level keys: %s", self.name,
                      list(payload)[:15] if isinstance(payload, dict) else type(payload).__name__)
            return []

        if not _SCHEMA_LOGGED:
            _SCHEMA_LOGGED = True
            log.info("[%s] API record fields: %s", self.name,
                     sorted(records[0])[:40])
            log.info("[%s] total reported by the API: %s", self.name,
                     _total(payload))

        items: list[RawOpportunity] = []
        skipped_closed = 0
        types: dict[str, int] = {}
        for r in records:
            notice_type_raw = _text(_first(r, "notice_type", "noticetype",
                                           "procurement_type"))
            types[notice_type_raw or "(none)"] = types.get(notice_type_raw or "(none)", 0) + 1
            # Contract Awards are decisions already taken. They dominate this
            # feed and nobody can bid on them.
            if not is_open_notice(notice_type_raw):
                skipped_closed += 1
                continue
            # Where the title came from is evidence about WHAT this record is.
            #
            # `project_name` was in this chain, so a record with no bid
            # description was titled with the project it belongs to — and then
            # read on the dashboard as a project rather than a notice. It is
            # the reason World Bank rows look like projects.
            #
            # It stays in the chain (dropping it would silently lose rows that
            # may be real notices), but a row that had nothing else is marked
            # as a project so the source's contract decides, visibly and
            # centrally, instead of this parser deciding quietly.
            title = _text(_first(
                r, "bid_description", "noticetitle", "notice_title", "title",
                "bid_reference_no"))
            titled_from_project = False
            if not title:
                title = _text(r.get("project_name"))
                titled_from_project = bool(title)
            if not title:
                continue
            nid = _text(_first(r, "id", "noticeid", "notice_id", "uuid", "guid"))
            url = _text(_first(r, "url", "noticeurl", "notice_url", "link"))
            if not url and nid:
                url = DETAIL_URL_TEMPLATE.format(id=nid)
            country = _text(_first(
                r, "project_ctry_name", "country_name", "countryname",
                "ctry_name", "country"))
            # submission_deadline_date FIRST. submission_date is the date the
            # notice was published, and reading it as a deadline gave 300 rows
            # the same date — see DEADLINE in the module docstring.
            deadline = _date(_first(
                r, "submission_deadline_date", "bid_deadline_date",
                "deadline_date", "closing_date", "deadline"))
            notice_type = notice_type_raw
            borrower = _text(_first(r, "borrower", "agency", "implementing_agency",
                                    "project_name"))
            posted = _date(_first(r, "noticedate", "notice_date", "publish_date",
                                  "submitdate"))
            group = _text(_first(r, "procurement_group", "sector", "major_sector"))
            sector = PROCUREMENT_GROUPS.get(group.upper(), group)
            method = _text(_first(r, "procurement_method_name",
                                  "procurement_method"))

            bits = [
                f"Notice type: {notice_type}" if notice_type else "",
                f"Borrower/agency: {borrower}" if borrower else "",
                f"Procurement group: {sector}" if sector else "",
                f"Method: {method}" if method else "",
                f"Published: {posted}" if posted else "",
                f"Reference: {_text(r.get('bid_reference_no'))}"
                if r.get("bid_reference_no") else "",
            ]
            items.append(RawOpportunity(
                title=title[:500],
                # The borrowing country's agency runs the procurement; the World
                # Bank finances it. Naming the agency is more useful to a bidder
                # than repeating the source name on every row.
                organization=(borrower or "World Bank")[:256],
                country=country[:128],
                location=country[:512],
                vertical=sector[:256],
                summary=" | ".join(b for b in bits if b)[:2000],
                deadline_raw=deadline,
                opportunity_url=url,
                website=SITE,
                source_website=self.display_name,
                category_hint=Category.TENDER,
                # The source's OWN words, handed to the contract rather than
                # only written into the summary text. Without these,
                # `record_is_in_scope` sees a blank record type and keeps
                # everything — so World Bank's manifest, which excludes
                # contract awards and projects, could never fire.
                record_type=(RecordType.PROJECT.value if titled_from_project
                             else record_type_for(notice_type_raw)),
                source_status=notice_type_raw[:64],
                # Every record here is a published notice, but one without a
                # readable deadline must not become a permanently open row —
                # see the assume_active note in schemas/opportunity.py.
                assume_active=False,
                dayfirst=False,          # the API returns ISO dates
            ))

        if skipped_closed:
            # Reported, not silent. A filter you cannot see is a filter you
            # cannot check — and if the ratio ever inverts, that is the signal
            # that the notice_type vocabulary changed.
            log.info("[%s] kept %s open notice(s), skipped %s already-decided "
                     "one(s) on this page", self.name, len(items), skipped_closed)
        if types and not items:
            log.warning("[%s] every record on this page was filtered out. "
                        "notice_type values seen: %s — if these look like open "
                        "calls, OPEN_NOTICE_TYPES needs the new wording.",
                        self.name, sorted(types.items(), key=lambda kv: -kv[1])[:8])
        return items

    # ------------------------------------------------------------- pagination
    def next_page(self, html: str, page_url: str, page_number: int) -> PageRequest | None:
        """Bump the offset until the API's own total is reached.

        The total is the stop condition, not a guess about when the list ends —
        the same approach that makes the UN Partner Portal walk exact.
        """
        try:
            payload = json.loads(html)
        except ValueError:
            return None
        total = _total(payload)
        seen_so_far = page_number * ROWS
        if page_number >= self.max_pages:
            log.info("[%s] stopping at the %s-page window (%s most recent "
                     "notices). The API's %s total is the full historical "
                     "archive, not open tenders.", self.name, self.max_pages,
                     f"{self.max_pages * ROWS:,}",
                     f"{total:,}" if total else "?")
            return None
        if total is None:
            # No total published: keep going while a full page comes back, and
            # stop on the first short one. BaseScraper's repeated-content guard
            # catches an API that re-serves the last page instead of ending.
            if len(_rows(payload)) < ROWS:
                return None
        elif seen_so_far >= total:
            log.info("[%s] walked all %s notice(s)", self.name, total)
            return None
        return PageRequest(f"{API}?format=json&rows={ROWS}&os={seen_so_far}")


__all__ = ["WorldBankScraper"]
