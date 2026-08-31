"""World Bank current procurement opportunities from the canonical page.

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

  1. Open the human-facing Business Opportunities page first.
  2. Observe the first-party data request made by that page after selecting
     Current Opportunities, and page only that observed request. Never replace
     it with a remembered endpoint when the page did not request one.

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

import asyncio
import json
import logging
import queue
import re
import threading
import time
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.database.models import Category
from app.schemas.opportunity import RawOpportunity
from app.services.notice_types import record_type_for
from app.services.source_manifest import RecordType
from app.core.config import settings
from app.scrapers.base_scraper import BaseScraper, ProgressCallback
from app.scrapers.registry import register

log = logging.getLogger("scraper")

SITE = "https://projects.worldbank.org"
CANONICAL_URL = f"{SITE}/en/projects-operations/opportunities"
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

_PAGING_OFFSET_KEYS = {"os", "offset", "start", "from", "skip"}
_PAGING_NUMBER_KEYS = {"page", "pagenr", "pagenumber", "pageindex"}


def _is_worldbank_host(host: str) -> bool:
    """Accept World Bank itself and its subdomains, never look-alike suffixes."""
    host = (host or "").split(":", 1)[0].lower().rstrip(".")
    return host == "worldbank.org" or host.endswith(".worldbank.org")


def is_first_party_data_request(url: str) -> bool:
    """Whether a request made by the canonical page looks like notice data."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not _is_worldbank_host(parsed.hostname or ""):
        return False
    if url.rstrip("/") == CANONICAL_URL.rstrip("/"):
        return False
    haystack = f"{parsed.path}?{parsed.query}".lower()
    return (
        any(word in haystack for word in ("procnotic", "procurement", "opportunit"))
        and any(mark in haystack for mark in ("api", "json", "format=", "rows=", "limit="))
    )


def pick_data_endpoint(observed: list[str]) -> str:
    """Choose the most-filtered first-party data request the page actually made."""
    candidates = list(dict.fromkeys(u for u in observed if is_first_party_data_request(u)))
    if not candidates:
        return ""

    def rank(url: str) -> tuple[int, int, int]:
        parsed = urlparse(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        # Current/open/facet parameters are stronger evidence than a bare API
        # URL, followed by the number of parameters and then URL specificity.
        current = sum(
            1 for k, v in pairs
            if any(t in f"{k}={v}".lower() for t in ("current", "open", "status", "facet", "fct"))
        )
        return current, len(pairs), len(url)

    return max(candidates, key=rank)


def _paged_endpoint(url: str, page_number: int, page_size: int) -> str:
    """Advance whichever paging parameter the observed request already uses.

    An endpoint with no recognised paging parameter is returned unchanged; the
    caller detects that and stops after its first response instead of inventing
    a contract the canonical page never demonstrated.
    """
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    output: list[tuple[str, str]] = []
    for key, value in pairs:
        low = key.lower()
        if low in _PAGING_OFFSET_KEYS:
            value = str((page_number - 1) * page_size)
            changed = True
        elif low in _PAGING_NUMBER_KEYS:
            value = str(page_number)
            changed = True
        output.append((key, value))
    if page_number > 1 and not changed:
        return url
    return urlunparse(parsed._replace(query=urlencode(output, doseq=True)))


def notice_evidence(record: dict) -> str:
    """The source field proving this is a procurement notice, not a project."""
    if _text(_first(record, "notice_type", "noticetype", "procurement_type")):
        return "notice_type"
    if _text(_first(record, "bid_description", "noticetitle", "notice_title")):
        return "bid_description"
    if _text(record.get("bid_reference_no")):
        return "bid_reference_no"
    if _text(_first(record, "procurement_method_name", "procurement_method",
                    "procurement_method_code")):
        return "procurement_method"
    return ""


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
    start_url = CANONICAL_URL
    requires_js = True
    prefer_js = True
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

    def __init__(self) -> None:
        super().__init__()
        self.rejected_no_evidence = 0
        self.rejected_closed = 0
        self.observed_endpoint = ""
        self.final_url = ""
        self.current_selector = ""

    def rejection_counts(self) -> dict[str, int]:
        counts = {
            "no evidence of a procurement notice": self.rejected_no_evidence,
            "closed/already-decided notice (award/cancelled)": self.rejected_closed,
        }
        return {reason: count for reason, count in counts.items() if count}

    # ------------------------------------------------------------ browser walk
    async def crawl(
        self,
        stop_event: asyncio.Event,
        pause_event: asyncio.Event,
        progress: ProgressCallback,
    ) -> AsyncIterator[list[RawOpportunity]]:
        """Keep one browser alive while consuming the page-observed endpoint.

        A bounded queue and per-page acknowledgement prevent the browser thread
        from racing ahead of callers such as ``verify_source --pages 3``. When
        the caller stops, the worker sees the stop event before another request
        and the browser is closed through ``site_auth.close_owned``.
        """
        messages: queue.Queue = queue.Queue(maxsize=1)
        worker = asyncio.create_task(asyncio.to_thread(
            self._browser_worker, messages, stop_event, pause_event))
        pending_ack: threading.Event | None = None
        try:
            while True:
                kind, payload, ack = await asyncio.to_thread(messages.get)
                pending_ack = ack
                if kind == "done":
                    break
                if kind == "error":
                    raise RuntimeError(payload)
                page_number = int(payload["page"])
                url = str(payload["url"])
                items = payload["items"]
                await progress("page_start", {
                    "source": self.name, "page": page_number, "url": url,
                })
                yield items
                await progress("page_done", {
                    "source": self.name, "page": page_number, "found": len(items),
                })
                ack.set()
                pending_ack = None
            await progress("pages_end", {"source": self.name})
        finally:
            stop_event.set()
            if pending_ack is not None:
                pending_ack.set()
            try:
                await worker
            except Exception:
                if not worker.cancelled():
                    raise

    @staticmethod
    def _wait_until_resumed(stop_event: asyncio.Event,
                            pause_event: asyncio.Event) -> bool:
        while not pause_event.is_set() and not stop_event.is_set():
            time.sleep(0.1)
        return not stop_event.is_set()

    def _browser_worker(self, messages: queue.Queue, stop_event: asyncio.Event,
                        pause_event: asyncio.Event) -> None:
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        context = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=settings.user_agent)
                page = context.new_page()
                observed: list[str] = []
                page.on("request", lambda request: observed.append(request.url))

                log.info("[%s] opening the canonical page: %s", self.name,
                         CANONICAL_URL)
                page.goto(CANONICAL_URL,
                          timeout=int(settings.request_timeout * 1000),
                          wait_until="domcontentloaded")
                self.final_url = page.url
                log.info("[%s] canonical page final URL: %s", self.name,
                         self.final_url)

                self.current_selector = self._select_current_opportunities(page)
                if self.current_selector:
                    log.info("[%s] selected Current Opportunities via %r",
                             self.name, self.current_selector)
                else:
                    log.warning(
                        "[%s] no Current Opportunities control matched. The page "
                        "may default to it, but this run cannot prove the tab was selected.",
                        self.name)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    page.wait_for_timeout(3_000)

                endpoint = pick_data_endpoint(observed)
                self.observed_endpoint = endpoint
                if endpoint:
                    log.info("[%s] the page loads its list from: %s", self.name,
                             endpoint)
                    self._walk_observed_endpoint(
                        page, endpoint, messages, stop_event, pause_event)
                else:
                    log.warning(
                        "[%s] no first-party procurement data request was observed; "
                        "parsing the rendered Current Opportunities DOM without "
                        "substituting a remembered endpoint", self.name)
                    items = self.parse_rendered_dom(page.content(), page.url)
                    if items and not stop_event.is_set():
                        self._send_batch(messages, 1, page.url, items, stop_event)
        except Exception as exc:  # browser failures must reach the async caller
            messages.put(("error", f"{type(exc).__name__}: {exc}", threading.Event()))
        finally:
            site_auth.close_owned(context)
            messages.put(("done", {}, threading.Event()))

    def _walk_observed_endpoint(self, page, endpoint: str, messages: queue.Queue,
                                stop_event: asyncio.Event,
                                pause_event: asyncio.Event) -> None:
        seen: set[str] = set()
        page_size = 0
        for page_number in range(1, self.max_pages + 1):
            if not self._wait_until_resumed(stop_event, pause_event):
                return
            request_url = endpoint if page_number == 1 else _paged_endpoint(
                endpoint, page_number, page_size or ROWS)
            if page_number > 1 and request_url == endpoint:
                log.warning(
                    "[%s] the page-observed request has no recognised paging "
                    "parameter; stopping after one batch instead of inventing one",
                    self.name)
                return
            response = page.evaluate(
                """async url => {
                    const r = await fetch(url, {
                      credentials: 'include',
                      headers: {'Accept': 'application/json, text/plain, */*'}
                    });
                    return {status: r.status, url: r.url, text: await r.text()};
                }""",
                request_url,
            )
            status = int(response.get("status") or 0)
            body = response.get("text") or ""
            final_url = response.get("url") or request_url
            if status < 200 or status >= 300:
                raise RuntimeError(
                    f"page-observed World Bank request returned HTTP {status}: "
                    f"{final_url}")
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise RuntimeError(
                    f"page-observed World Bank response was not JSON: {final_url}") from exc
            records = _rows(payload)
            if not records:
                log.info("[%s] observed endpoint returned no records on page %s",
                         self.name, page_number)
                return
            if not page_size:
                page_size = len(records)
            signature = json.dumps(records, sort_keys=True, default=str)
            if signature in seen:
                log.warning("[%s] page %s repeated an earlier data batch; stopping",
                            self.name, page_number)
                return
            seen.add(signature)
            items = self.parse_listing(body, final_url)
            if not items:
                # A page of awards can legitimately yield no opportunities. It
                # is not proof the archive ended, so continue while raw records
                # and a paging contract remain.
                log.info("[%s] page %s contained %s record(s), all rejected",
                         self.name, page_number, len(records))
            self._send_batch(messages, page_number, final_url, items, stop_event)
            total = _total(payload)
            if total is not None and page_number * page_size >= total:
                return
            if len(records) < page_size:
                return

    @staticmethod
    def _send_batch(messages: queue.Queue, page_number: int, url: str,
                    items: list[RawOpportunity], stop_event: asyncio.Event) -> None:
        ack = threading.Event()
        messages.put(("batch", {
            "page": page_number, "url": url, "items": items,
        }, ack))
        while not ack.wait(0.1):
            if stop_event.is_set():
                return

    @staticmethod
    def _select_current_opportunities(page) -> str:
        pattern = re.compile(r"current\s+opportunities", re.IGNORECASE)
        for role in ("tab", "button", "link"):
            locator = page.get_by_role(role, name=pattern)
            for index in range(min(locator.count(), 5)):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        candidate.click(timeout=10_000)
                        page.wait_for_timeout(1_500)
                        return f"role={role}, name=Current Opportunities"
                except Exception:
                    continue
        for selector in (
            "[data-tab*='current' i]", "[data-target*='current' i]",
            "a[href*='current' i]", "button[id*='current' i]",
        ):
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible():
                        candidate.click(timeout=10_000)
                        page.wait_for_timeout(1_500)
                        return selector
                except Exception:
                    continue
        return ""

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
            evidence = notice_evidence(r)
            if not evidence:
                self.rejected_no_evidence += 1
                continue
            notice_type_raw = _text(_first(r, "notice_type", "noticetype",
                                           "procurement_type"))
            types[notice_type_raw or "(none)"] = types.get(notice_type_raw or "(none)", 0) + 1
            # Contract Awards are decisions already taken. They dominate this
            # feed and nobody can bid on them.
            if not is_open_notice(notice_type_raw):
                skipped_closed += 1
                self.rejected_closed += 1
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
                record_type=record_type_for(notice_type_raw),
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

    def parse_rendered_dom(self, html: str, page_url: str) -> list[RawOpportunity]:
        """Conservative fallback when the page exposes no observable data call.

        Only links that look like procurement-notice detail pages are accepted;
        project cards and generic links are ignored even when their text looks
        useful. This fallback intentionally prefers a visible failure over
        filling the dashboard with projects.
        """
        soup = BeautifulSoup(html or "", "lxml")
        items: list[RawOpportunity] = []
        seen: set[str] = set()
        deadline_re = re.compile(
            r"(?:closing|submission|deadline|due)\s*(?:date)?\s*[:\-]?\s*"
            r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
            r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|"
            r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
            re.IGNORECASE,
        )
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            low = href.lower()
            if not any(marker in low for marker in (
                "procurement-detail", "procurement-notice", "procnotic")):
                continue
            from urllib.parse import urljoin

            url = urljoin(page_url or CANONICAL_URL, href)
            if url in seen:
                continue
            title = anchor.get_text(" ", strip=True)
            if len(title) < 8:
                continue
            container = anchor.find_parent(["article", "li", "tr", "div"])
            text = container.get_text(" ", strip=True) if container else title
            match = deadline_re.search(text)
            deadline = match.group(1) if match else ""
            seen.add(url)
            items.append(RawOpportunity(
                title=title[:500],
                organization="World Bank",
                summary=text[:2000],
                deadline_raw=deadline,
                opportunity_url=url,
                website=SITE,
                source_website=self.display_name,
                category_hint=Category.TENDER,
                record_type=RecordType.TENDER.value,
                source_status="Current Opportunity",
                assume_active=False,
                dayfirst=False,
            ))
        return items


__all__ = [
    "CANONICAL_URL", "WorldBankScraper", "is_first_party_data_request",
    "notice_evidence", "pick_data_endpoint",
]
