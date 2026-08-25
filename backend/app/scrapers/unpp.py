"""UN Partner Portal (unpartnerportal.org) — signed-in CFEI scraper.

Why this is a module and not a sources.json entry
-------------------------------------------------
The evidence is in backend/data/debug/un_partner_portal_page1.html, saved by the
last run of the generic scraper. Read it and the failure is unambiguous:

  * the page IS signed in — the header carries "Nitin Editor - Swasti", the
    notification count, Dashboard / Your Applications / Profile. So the Chrome
    session mirror works and the login is NOT the problem;
  * the results table rendered its headers — Project Title, Country, Sector &
    Area of Specialization, UN Agency, Application Deadline, Estimated Start
    Date — and then one cell reading "No data available";
  * the pagination bar reads "0-0 of 0";
  * and a Toastify error toast is sitting on the page saying "Unable to load
    data".

That last one is the whole story. This is a React SPA: the HTML it serves
contains no opportunities at any time, and the table is filled by an XHR to the
portal's own API after load. The toast means that XHR **failed**. The generic
HTML scraper cannot see any of this — all it knows is "0 links that look like
funding", so it reported an empty page and moved on. A source that is silently
returning nothing while reporting success is worse than one that errors.

So this module does three things the generic path cannot:

  1. It talks to the API directly instead of scraping rendered HTML. The table
     is the API's output; going to the source removes a whole layer that can
     silently render nothing.
  2. It never guesses the endpoint. It watches the app's own network traffic
     and takes whichever request returns a paginated payload, and only falls
     back to probing a short candidate list if the app's own call failed.
  3. When something fails it says WHAT failed — the URL, the HTTP status and
     the response body. "Unable to load data" cost a debugging session; a log
     line reading `GET /api/projects/open/ -> 403 {"detail":"..."} ` does not.

Authentication
--------------
Three routes, tried in this order:

  1. A session the browser already holds — your everyday Chrome profile on a
     desktop (site_auth.open_context), or an imported session file. This is why
     the scraper worked on the laptop from day one without ever using the
     password.
  2. The sign-in FORM, with LOP_UNPP_EMAIL / LOP_UNPP_PASSWORD from
     backend/.env. This is the route a server needs, since EC2 has no Chrome
     profile to borrow. UNPP's form is TWO-STEP: email, advance, then password.
  3. The sign-in API, as a fallback.

Every route ends the same way: a GET of /api/accounts/me/ must return 200. That
is the portal's own answer to "who am I", and nothing short of it counts as
signed in — reading the rendered page for the word "Dashboard" is a guess about
a React app mid-render, and it guessed wrong.

If NO route produces a session, this scraper yields nothing and logs an error.
It deliberately does not fall back to /landing/opportunities, the public teaser:
half a listing scraped anonymously looks like success in the dashboard and is
the reason a source can appear healthy for weeks while being wrong.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from queue import Empty, Queue
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.core.config import settings
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import register
from app.services.geography import canonical_country

log = logging.getLogger("scraper")

BASE = "https://www.unpartnerportal.org"
LOGIN_URL = f"{BASE}/login"

# The signed-in list of open Calls for Expression of Interest. /landing/
# opportunities is the public teaser and is NOT this.
LISTING_URL = f"{BASE}/cfei/open"

# The human page for one project. Chosen to match what services/links.py already
# rewrites UNPP machine endpoints into, so a row scraped here and a row repaired
# there end up on the same URL:
#
#     /api/public/projects/{id}  ->  /landing/opportunities/{id}/
#
# It is also the page a colleague without a UNPP account can actually open —
# /cfei/open/{id}/overview requires a login and shows them a sign-in wall.
DETAIL_URL_TEMPLATE = f"{BASE}/landing/opportunities/{{id}}/"
# Where the same record lives for someone who IS signed in. Recorded in the
# summary so the team can jump straight to the application view.
INTERNAL_URL_TEMPLATE = f"{BASE}/cfei/open/{{id}}/overview"

# Candidate list endpoints, used ONLY when the app's own request failed and
# there is therefore nothing to observe. Ordered most- to least-likely. The
# names come from the portal's own DOM: its filter controls are id'd
# `table_filter_select_projects_list_open_*`, i.e. the table is "projects list,
# open", which is what /api/projects/open/ serves.
#
# CONFIRMED LIVE 2026-08-25: the endpoint the portal itself calls is
#     GET https://www.unpartnerportal.org/api/projects/open/?page=N&page_size=50
# returning a DRF page — {"count": 61, "results": [...]} — with the fields
# id / title / agency / country_code / specializations / deadline_date /
# start_date. The first entry below is therefore fact, not a guess. The list is
# kept anyway: discovery still runs first, so the day the portal moves this
# endpoint the scraper follows it instead of failing on a hard-coded path.
CANDIDATE_ENDPOINTS = (
    "/api/projects/open/",
    "/api/projects/",
    "/api/projects/open",
    "/api/public/projects/",
)

# A paginated DRF payload. Both spellings appear across the portal's endpoints.
_COUNT_KEYS = ("count", "total", "total_count")
_RESULTS_KEYS = ("results", "items", "data")


def _first(d: dict, *names, default=""):
    """First present, non-empty value among several possible key names.

    The portal's serializers are not consistent between endpoints (deadline_date
    vs application_deadline_date, agency vs agency_name), and a scraper that
    hard-codes one spelling breaks silently on the day the other one ships —
    producing rows with an empty deadline, which the pipeline then stores as
    permanently open. Trying the known spellings costs nothing.
    """
    for n in names:
        v = d.get(n)
        if v not in (None, "", [], {}):
            return v
    return default


def _as_name(value) -> str:
    """Flatten the several shapes the API uses for a named thing.

    Seen: "UNICEF" / {"name": "UNICEF"} / [{"name": ...}, ...].
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(_first(value, "name", "title", "display_name", "label")).strip()
    if isinstance(value, (list, tuple)):
        parts = [_as_name(v) for v in value]
        return ", ".join(p for p in parts if p)
    return str(value or "").strip()


# ISO 3166-1 alpha-2 -> the country name services/geography.py recognises.
#
# The portal returns country CODES ("SD", "RW", "IN"), and geography.py is a
# deliberate whitelist of country NAMES: `canonical_country("SD")` returns "",
# so every row lost its country AND its region on the way into the database.
# The first live run scraped 61 rows carrying 15 different countries and stored
# none of them.
#
# The fix belongs here rather than in geography.py. Adding two-letter aliases to
# that module's global table would make every scraper treat a bare "IT", "IN",
# "IS", "AT", "BE", "NO", "SO", "ME" or "OR" found anywhere on any page as a
# country — and stopping exactly that kind of junk is why that whitelist exists.
# Here the field is known to be an ISO code, so the mapping is safe.
#
# Every value below is verified against canonical_country() at import time.
_ISO_ALPHA2: dict[str, str] = {
    "AF": "Afghanistan", "AL": "Albania", "DZ": "Algeria", "AS": "American Samoa",
    "AD": "Andorra", "AO": "Angola", "AI": "Anguilla", "AG": "Antigua and Barbuda",
    "AR": "Argentina", "AM": "Armenia", "AW": "Aruba", "AU": "Australia",
    "AT": "Austria", "AZ": "Azerbaijan", "BS": "Bahamas", "BH": "Bahrain",
    "BD": "Bangladesh", "BB": "Barbados", "BY": "Belarus", "BE": "Belgium",
    "BZ": "Belize", "BJ": "Benin", "BM": "Bermuda", "BT": "Bhutan",
    "BO": "Bolivia", "BQ": "Caribbean Netherlands", "BA": "Bosnia and Herzegovina",
    "BW": "Botswana", "BR": "Brazil", "VG": "British Virgin Islands",
    "BN": "Brunei", "BG": "Bulgaria", "BF": "Burkina Faso", "BI": "Burundi",
    "KH": "Cambodia", "CM": "Cameroon", "CA": "Canada", "CV": "Cape Verde",
    "KY": "Cayman Islands", "CF": "Central African Republic", "TD": "Chad",
    "CL": "Chile", "CN": "China", "CO": "Colombia", "KM": "Comoros",
    "CG": "Congo", "CD": "DR Congo", "CK": "Cook Islands", "CR": "Costa Rica",
    "HR": "Croatia", "CU": "Cuba", "CW": "Curaçao", "CY": "Cyprus",
    "CZ": "Czech Republic", "CI": "Côte d’Ivoire", "DK": "Denmark",
    "DJ": "Djibouti", "DM": "Dominica", "DO": "Dominican Republic",
    "EC": "Ecuador", "EG": "Egypt", "SV": "El Salvador", "GQ": "Equatorial Guinea",
    "ER": "Eritrea", "EE": "Estonia", "SZ": "Eswatini", "ET": "Ethiopia",
    "FO": "Faroe Islands", "FJ": "Fiji", "FI": "Finland", "FR": "France",
    "GF": "French Guiana", "PF": "French Polynesia", "GA": "Gabon",
    "GM": "Gambia", "GE": "Georgia", "DE": "Germany", "GH": "Ghana",
    "GI": "Gibraltar", "GR": "Greece", "GL": "Greenland", "GD": "Grenada",
    "GP": "Guadeloupe", "GU": "Guam", "GT": "Guatemala", "GG": "Guernsey",
    "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana", "HT": "Haiti",
    "HN": "Honduras", "HK": "Hong Kong", "HU": "Hungary", "IS": "Iceland",
    "IN": "India", "ID": "Indonesia", "IR": "Iran", "IQ": "Iraq",
    "IE": "Ireland", "IM": "Isle of Man", "IL": "Israel", "IT": "Italy",
    "JM": "Jamaica", "JP": "Japan", "JE": "Jersey", "JO": "Jordan",
    "KZ": "Kazakhstan", "KE": "Kenya", "KI": "Kiribati", "XK": "Kosovo",
    "KW": "Kuwait", "KG": "Kyrgyzstan", "LA": "Laos", "LV": "Latvia",
    "LB": "Lebanon", "LS": "Lesotho", "LR": "Liberia", "LY": "Libya",
    "LI": "Liechtenstein", "LT": "Lithuania", "LU": "Luxembourg", "MO": "Macau",
    "MG": "Madagascar", "MW": "Malawi", "MY": "Malaysia", "MV": "Maldives",
    "ML": "Mali", "MT": "Malta", "MH": "Marshall Islands", "MQ": "Martinique",
    "MR": "Mauritania", "MU": "Mauritius", "YT": "Mayotte", "MX": "Mexico",
    "FM": "Micronesia", "MD": "Moldova", "MC": "Monaco", "MN": "Mongolia",
    "ME": "Montenegro", "MS": "Montserrat", "MA": "Morocco", "MZ": "Mozambique",
    "MM": "Myanmar", "NA": "Namibia", "NR": "Nauru", "NP": "Nepal",
    "NL": "Netherlands", "NC": "New Caledonia", "NZ": "New Zealand",
    "NI": "Nicaragua", "NE": "Niger", "NG": "Nigeria", "NU": "Niue",
    "NF": "Norfolk Island", "KP": "North Korea", "MK": "North Macedonia",
    "MP": "Northern Mariana Islands", "NO": "Norway", "OM": "Oman",
    "PK": "Pakistan", "PW": "Palau", "PS": "Palestine", "PA": "Panama",
    "PG": "Papua New Guinea", "PY": "Paraguay", "PE": "Peru",
    "PH": "Philippines", "PN": "Pitcairn Islands", "PL": "Poland",
    "PT": "Portugal", "PR": "Puerto Rico", "QA": "Qatar", "RO": "Romania",
    "RU": "Russian Federation", "RW": "Rwanda", "RE": "Réunion",
    "BL": "Saint Barthélemy", "SH": "Saint Helena", "KN": "Saint Kitts and Nevis",
    "LC": "Saint Lucia", "MF": "Saint Martin",
    "VC": "Saint Vincent and the Grenadines", "WS": "Samoa", "SM": "San Marino",
    "ST": "Sao Tome and Principe", "SA": "Saudi Arabia", "SN": "Senegal",
    "RS": "Serbia", "SC": "Seychelles", "SL": "Sierra Leone", "SG": "Singapore",
    "SX": "Sint Maarten", "SK": "Slovakia", "SI": "Slovenia",
    "SB": "Solomon Islands", "SO": "Somalia", "ZA": "South Africa",
    "KR": "South Korea", "SS": "South Sudan", "ES": "Spain", "LK": "Sri Lanka",
    "SD": "Sudan", "SR": "Suriname", "SE": "Sweden", "CH": "Switzerland",
    "SY": "Syria", "TW": "Taiwan", "TJ": "Tajikistan", "TZ": "Tanzania",
    "TH": "Thailand", "TL": "Timor-Leste", "TG": "Togo", "TK": "Tokelau",
    "TO": "Tonga", "TT": "Trinidad and Tobago", "TN": "Tunisia", "TR": "Turkey",
    "TM": "Turkmenistan", "TC": "Turks and Caicos Islands", "TV": "Tuvalu",
    "VI": "U.S. Virgin Islands", "UG": "Uganda", "UA": "Ukraine",
    "AE": "United Arab Emirates", "GB": "United Kingdom", "US": "United States",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VU": "Vanuatu", "VA": "Vatican City",
    "VE": "Venezuela", "VN": "Vietnam", "WF": "Wallis and Futuna",
    "EH": "Western Sahara", "YE": "Yemen", "ZM": "Zambia", "ZW": "Zimbabwe",
    "AX": "Åland Islands",
}


def _check_iso_table() -> None:
    """Warn if a name above stopped matching geography.py's spelling.

    Cheap (238 dict lookups at import) and self-maintaining: the day that module
    renames "Czech Republic" or "DR Congo", this says so instead of silently
    dropping every row from that country again.
    """
    unknown = sorted(code for code, nm in _ISO_ALPHA2.items()
                     if canonical_country(nm) != nm)
    if unknown:
        log.warning("[un_partner_portal] %s ISO name(s) no longer match "
                    "services/geography.py and will be stored unnormalised: %s",
                    len(unknown), ", ".join(unknown[:20]))


_check_iso_table()


def _country_name(value: str) -> str:
    """Country codes and names in, names geography.py accepts out.

    Anything that resolves to nothing is passed through unchanged rather than
    dropped: a code this table has not met is still better in the field than an
    empty string, and it shows up in the dashboard as something to look at.
    """
    out: list[str] = []
    for token in re.split(r"[,;/|]", value or ""):
        t = token.strip()
        if not t:
            continue
        name = _ISO_ALPHA2.get(t.upper()) if len(t) == 2 else ""
        name = name or canonical_country(t) or t
        if name not in out:
            out.append(name)
    return ", ".join(out)


def _agency_name(value: str) -> str:
    """"UN_SECRETARIAT" -> "UN Secretariat"; "UNICEF" left exactly as it is.

    The API returns an enum key for multi-word agencies. Only values containing
    an underscore are touched, so a genuine acronym is never lower-cased into
    "Unicef".
    """
    v = (value or "").strip()
    if "_" not in v:
        return v
    return " ".join(w if (w.isupper() and len(w) <= 3) else w.capitalize()
                    for w in v.split("_") if w)


def _payload_rows(payload) -> list[dict] | None:
    """The list of records inside a paginated API response, or None.

    Returns None — not [] — when the payload is not a listing at all, so an
    endpoint that answers 200 with a user profile is not mistaken for a listing
    that happens to be empty.
    """
    if isinstance(payload, list):
        return payload if all(isinstance(r, dict) for r in payload) else None
    if not isinstance(payload, dict):
        return None
    for key in _RESULTS_KEYS:
        rows = payload.get(key)
        if isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
            return rows
    return None


def _payload_count(payload) -> int | None:
    if isinstance(payload, dict):
        for key in _COUNT_KEYS:
            if isinstance(payload.get(key), int):
                return payload[key]
    return None


def _with_page(url: str, page: int, page_size: int) -> str:
    """Same URL with page/page_size replaced, every other filter preserved.

    Rebuilding the query rather than appending is the point: the app's own
    request carries the user's filters (country, agency, specialization), and
    appending a second `page=` leaves the server free to honour either one.
    """
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in ("page", "page_size", "limit", "offset")]
    q.append(("page", str(page)))
    q.append(("page_size", str(page_size)))
    return urlunparse(parts._replace(query=urlencode(q)))


# Read inside the browser so the request carries the session exactly as the app
# would send it — cookies, and the Authorization header when the SPA uses one.
# Doing this with httpx outside the browser means reproducing that by hand, and
# getting it subtly wrong is how a scraper ends up reading the signed-out view.
_FETCH_JS = """
async ([url, headers, method, body]) => {
    const opts = {
        method: method || 'GET',
        credentials: 'include',
        headers: Object.assign({}, headers || {}),
    };
    if (body !== null && body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
        // Django refuses an unsafe method without this when the endpoint uses
        // session authentication. Harmless when it doesn't.
        const m = document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/);
        if (m) { opts.headers['X-CSRFToken'] = decodeURIComponent(m[1]); }
    }
    let status = 0, text = '';
    try {
        const r = await fetch(url, opts);
        status = r.status;
        text = await r.text();
    } catch (e) {
        return {status: -1, error: String(e), json: null, body: ''};
    }
    let json = null;
    try { json = JSON.parse(text); } catch (e) { /* not JSON */ }
    return {status, json, body: json === null ? text.slice(0, 1500) : '', error: ''};
}
"""

# Where the portal says who you are. A 200 here is the only proof of a session
# that cannot be faked by a page that merely looks signed in — and it is how the
# scraper found out it was signed OUT on the server (401) while the same code
# was signed in on a laptop.
WHOAMI_URL = f"{BASE}/api/accounts/me/"

# Sign-in endpoints to try, most likely first. The portal is Django REST
# Framework (its list endpoint is a DRF page and /api/accounts/me/ answers 401),
# so a token endpoint under /api/accounts/ is the expected shape.
LOGIN_ENDPOINTS = (
    "/api/accounts/login/",
    "/api/accounts/login",
    "/api/rest-auth/login/",
    "/api/auth/login/",
    "/api/login/",
)
# Field names DRF login serializers use, and the keys they return a token under.
LOGIN_FIELDS = (("email", "password"), ("username", "password"))
TOKEN_KEYS = ("token", "key", "access", "auth_token", "access_token", "access_key")
# DRF's TokenAuthentication uses "Token"; SimpleJWT uses "Bearer". Both are
# tried and the one that makes /api/accounts/me/ answer 200 is kept.
TOKEN_SCHEMES = ("Token", "Bearer", "JWT")


@register
class UNPartnerPortalScraper(BaseScraper):
    name = "un_partner_portal"
    display_name = "UN Partner Portal"
    # Every row on this page is a published call/tender notice, so a row
    # does not have to contain funding vocabulary to be an opportunity.
    # See services/opportunity_gate.py.
    curated = True
    website = BASE
    start_url = f"{LISTING_URL}?page=1&page_size=50"
    requires_js = True          # there is no server-rendered listing to read
    enrich_details = False      # the list payload already carries every field

    # The portal's own rows-per-page control offers 10 / 25 / 50 / 100.
    page_size = 50
    max_pages = 200             # 10,000 records; a safety net, not a target

    # Walk to the end of the list rather than stopping after N pages that saved
    # nothing new. The whole open-CFEI list was 61 records over 2 pages on
    # 2026-08-25, so "the end" costs two requests — while the default streak of
    # 3 would, once the list grows past three pages of already-stored calls,
    # abandon the source before reaching pages holding calls never seen before.
    # Cheap insurance against a failure that only appears months later.
    stale_page_streak_override = 0

    # ------------------------------------------------------------------ parse
    def parse_listing(self, html: str, page_url: str) -> list[RawOpportunity]:
        """DOM fallback: read the rendered MUI table.

        Only used if the API path yields nothing at all. It is second choice on
        purpose — the table shows one page, carries no project ids (the row
        links are client-side routes), and its CSS classes are emotion-generated
        hashes that change on every frontend deploy. The column HEADERS are the
        stable part, so the columns are located by their header text rather than
        by position or class.
        """
        soup = BeautifulSoup(html, "lxml")
        table = None
        for candidate in soup.select("table"):
            headers = [th.get_text(" ", strip=True).lower()
                       for th in candidate.select("thead th")]
            if any("project title" in h for h in headers):
                table = candidate
                break
        if table is None:
            return []

        headers = [th.get_text(" ", strip=True).lower()
                   for th in table.select("thead th")]

        def col(*wanted) -> int | None:
            for i, h in enumerate(headers):
                if any(w in h for w in wanted):
                    return i
            return None

        i_title = col("project title", "title")
        i_country = col("country")
        i_sector = col("sector", "specialization")
        i_agency = col("agency")
        i_deadline = col("deadline")
        i_start = col("start date")

        items: list[RawOpportunity] = []
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue      # the "No data available" row spans the table
            def cell(idx):
                return cells[idx].get_text(" ", strip=True) if (
                    idx is not None and idx < len(cells)) else ""

            title = cell(i_title)
            if len(title) < 8:
                continue
            href = ""
            a = cells[i_title].find("a", href=True) if i_title is not None else None
            if a:
                href = urljoin(page_url, a["href"])
            pid = self._id_from_url(href)
            items.append(self._build(
                pid=pid,
                title=title,
                country=_country_name(cell(i_country)),
                sector=cell(i_sector),
                agency=_agency_name(cell(i_agency)),
                deadline=cell(i_deadline),
                start=cell(i_start),
                url=DETAIL_URL_TEMPLATE.format(id=pid) if pid else href,
            ))
        log.info("[%s] DOM fallback parsed %s row(s)", self.name, len(items))
        return items

    @staticmethod
    def _id_from_url(url: str) -> str:
        m = re.search(r"/(?:cfei/\w+|landing/opportunities|projects)/(\d+)", url or "")
        return m.group(1) if m else ""

    def _build(self, *, pid, title, country, sector, agency, deadline, start,
               url, summary_extra="") -> RawOpportunity:
        bits = [
            f"UN Agency: {agency}" if agency else "",
            f"Sector & area of specialization: {sector}" if sector else "",
            f"Estimated start date: {start}" if start else "",
            f"Signed-in view: {INTERNAL_URL_TEMPLATE.format(id=pid)}" if pid else "",
            summary_extra,
        ]
        return RawOpportunity(
            title=(title or "")[:500],
            # The UN agency running the call is the funder — "UN Partner Portal"
            # is only where it was found. Storing the portal as the organisation
            # is what makes a dashboard full of rows that all look like the same
            # funder.
            organization=(agency or "UN Partner Portal")[:256],
            country=(country or "")[:128],
            location=(country or "")[:512],
            vertical=(sector or "")[:256],
            summary=" | ".join(b for b in bits if b)[:2000],
            deadline_raw=(deadline or "")[:64],
            opportunity_url=url or "",
            website=self.website,
            source_website=self.display_name,
            # Every row here is an OPEN call — /cfei/open is already filtered
            # to those — but a call with no published deadline must not be
            # stored as permanently live.
            assume_active=not bool(deadline),
            # The API returns ISO dates (2026-09-12), which are unambiguous.
            dayfirst=False,
        )

    def _rows_to_items(self, rows: list[dict]) -> list[RawOpportunity]:
        """Map API records onto RawOpportunity, tolerating key-name drift."""
        items: list[RawOpportunity] = []
        for r in rows:
            pid = str(_first(r, "id", "pk", "project_id", default="")).strip()
            title = _as_name(_first(r, "title", "name", "project_title"))
            if not title:
                continue
            country = _country_name(_as_name(_first(
                r, "country_name", "country", "country_code", "countries",
                "locations", "country_codes")))
            sector = _as_name(_first(
                r, "specializations", "specialization", "sectors", "sector",
                "areas_of_specialization"))
            agency = _agency_name(_as_name(
                _first(r, "agency", "agency_name", "un_agency")))
            deadline = str(_first(
                r, "deadline_date", "application_deadline_date", "deadline",
                "application_deadline", default=""))[:64]
            start = str(_first(
                r, "start_date", "estimated_start_date", "expected_start_date",
                default=""))[:64]
            url = DETAIL_URL_TEMPLATE.format(id=pid) if pid else ""
            items.append(self._build(
                pid=pid, title=title, country=country, sector=sector,
                agency=agency, deadline=deadline, start=start, url=url,
            ))
        return items

    def next_page(self, html, page_url, page_number):
        """Unused — pagination happens against the API inside _walk()."""
        return None

    # ------------------------------------------------------------------ crawl
    async def crawl(self, stop_event, pause_event, progress):
        """Yield one batch per API page. The browser work runs in a thread.

        Same shape as adb.py: Playwright's sync API cannot be driven from the
        event loop, and on Windows uvicorn's selector loop cannot spawn the
        browser subprocess at all, so the walk lives in its own thread and
        hands batches back through a queue.
        """
        queue: Queue = Queue()
        done = threading.Event()

        def worker() -> None:
            try:
                self._walk(queue, stop_event, done)
            except Exception:
                log.exception("[%s] browser walk failed", self.name)
            finally:
                done.set()
                queue.put(None)

        threading.Thread(target=worker, daemon=True).start()

        page_number = 0
        while True:
            if stop_event.is_set():
                done.set()
                break
            await pause_event.wait()
            try:
                payload = await asyncio.to_thread(queue.get, True, 1.0)
            except Empty:
                if done.is_set():
                    break
                continue
            if payload is None:
                break
            page_number += 1
            await progress("page_start", {"source": self.name, "page": page_number,
                                          "url": payload.get("url", LISTING_URL)})
            if payload["kind"] == "api":
                items = self._rows_to_items(payload["rows"])
            else:
                items = self.parse_listing(payload["html"], LISTING_URL)
            if items:
                yield items
            await progress("page_done", {"source": self.name, "page": page_number,
                                         "found": len(items)})
        await progress("pages_end", {"source": self.name, "page": page_number})

    # ------------------------------------------------------------- browser
    def _walk(self, queue: Queue, stop_event, done) -> None:
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        headless = bool(getattr(settings, "unpp_headless", True))
        seen: list[dict] = []          # every /api/ response the app made

        with sync_playwright() as pw:
            context = site_auth.open_context(pw, self.name, headless=headless)
            try:
                page = context.pages[0] if context.pages else context.new_page()

                def on_response(resp) -> None:
                    """Record the app's own API traffic — successes and failures.

                    Failures matter more than successes here. The last run's
                    saved page shows an "Unable to load data" toast and nothing
                    else; with this listener that becomes a log line naming the
                    URL and the status, which is the difference between a fix
                    and another guess.
                    """
                    try:
                        url = resp.url
                        if "/api/" not in url:
                            return
                        entry = {"url": url, "status": resp.status,
                                 "headers": dict(resp.request.headers)}
                        seen.append(entry)
                        if resp.status >= 400:
                            body = ""
                            try:
                                body = resp.text()[:500]
                            except Exception:
                                pass
                            log.error("[%s] the portal's own request failed: "
                                      "%s %s -> %s %s", self.name,
                                      resp.request.method, url, resp.status, body)
                    except Exception:
                        pass

                page.on("response", on_response)

                signed_in, auth = self._ensure_signed_in(page)
                if not signed_in:
                    log.error(
                        "[%s] not signed in, so /cfei/open has nothing to show. "
                        "Either set LOP_UNPP_EMAIL / LOP_UNPP_PASSWORD in "
                        "backend/.env, or sign in to %s in the Chrome profile "
                        "named by LOP_CHROME_USER_DATA_DIR / "
                        "LOP_CHROME_PROFILE_DIR and close Chrome before the run. "
                        "Refusing to scrape the public teaser instead — those "
                        "rows are not the same listings.",
                        self.name, LOGIN_URL,
                    )
                    self._dump(page, "unpp_signed_out")
                    return

                # Load the list once and let the app make its own API call. The
                # request it produces carries the account's filters and whatever
                # auth header the SPA uses, which is why it is worth observing
                # rather than reconstructing.
                start = _with_page(LISTING_URL, 1, self.page_size)
                page.goto(start, timeout=90_000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                page.wait_for_timeout(3_000)   # the table XHR trails networkidle

                endpoint, headers = self._discover_endpoint(page, seen, auth)
                if not endpoint:
                    log.error(
                        "[%s] could not identify the listing API. The portal's "
                        "own requests were: %s", self.name,
                        ", ".join(f"{e['status']} {e['url']}" for e in seen[-15:])
                        or "(none seen at all)")
                    # Last resort: whatever the table managed to render.
                    html = page.content()
                    self._dump(page, "unpp_no_endpoint")
                    if self.parse_listing(html, LISTING_URL):
                        queue.put({"kind": "html", "html": html, "url": start})
                    return

                log.info("[%s] listing API: %s", self.name, endpoint)
                self._paginate(page, endpoint, headers, queue, stop_event, done)
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    def _ensure_signed_in(self, page) -> tuple[bool, dict]:
        """(signed_in, auth_headers). Checked against the API, never assumed.

        Three routes, in this order:

          1. a session the browser already holds — your Chrome profile on a
             desktop, or an imported session file;
          2. the sign-in FORM, driven properly;
          3. the portal's own sign-in API, as a fallback.

        The form comes before the API because the server told us it is the
        supported route. Every candidate API endpoint answered with a Django
        "403 Forbidden" HTML page — the CSRF middleware's response, not DRF's,
        so those requests never reached a view. Meanwhile the login page itself
        rendered and reported exactly one field: `inputs=['email:email']`.

        That single line is the whole diagnosis. UNPP's sign-in is a TWO-STEP
        form: enter the email, advance, and only then does the password field
        appear. Code that navigates to /login and waits for `input[type=
        password]` waits forever on a page that is working perfectly — which is
        precisely what the 30-second timeout was reporting.
        """
        page.goto(LISTING_URL, timeout=90_000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        if self._whoami(page, {}):
            log.info("[%s] already signed in via the saved browser session",
                     self.name)
            return True, {}

        email = (getattr(settings, "unpp_email", "") or "").strip()
        password = getattr(settings, "unpp_password", "") or ""
        if not (email and password):
            log.error("[%s] no saved browser session and no LOP_UNPP_EMAIL / "
                      "LOP_UNPP_PASSWORD in backend/.env", self.name)
            return False, {}

        log.info("[%s] signing in as %s", self.name, email)
        if self._form_login(page, email, password):
            # The form having been submitted is not proof of a session, and the
            # portal may authenticate by cookie OR by a token the app keeps in
            # browser storage. Both are checked, and whichever answers
            # /api/accounts/me/ with a 200 is what the rest of the run uses.
            if self._whoami(page, {}):
                log.info("[%s] signed in through the form (session cookie)",
                         self.name)
                return True, {}
            headers = self._token_from_storage(page)
            if headers:
                log.info("[%s] signed in through the form (token from browser "
                         "storage)", self.name)
                return True, headers
            log.error("[%s] the form was submitted but the portal still does "
                      "not recognise the session — wrong password, an expired "
                      "account, or a second factor.", self.name)
            self._dump(page, "unpp_login_failed")

        log.info("[%s] the form did not sign us in — trying the sign-in API",
                 self.name)
        headers = self._api_login(page, email, password)
        if headers:
            return True, headers
        return False, {}

    def _whoami(self, page, headers: dict) -> bool:
        """True when the portal says who we are — 200 from /api/accounts/me/.

        This replaces reading the rendered page for words like "Dashboard".
        Those are a guess about a React app mid-render; this is the portal's own
        answer, and it is the check that correctly reported 401 on the server
        while the same code was signed in on a laptop.
        """
        r = self._fetch(page, WHOAMI_URL, headers)
        return r["status"] == 200 and isinstance(r["json"], dict)

    def _api_login(self, page, email: str, password: str) -> dict:
        """Sign in through the portal's API. Returns auth headers, or {}.

        Every combination is *verified* against /api/accounts/me/ before being
        returned — a 200 from a login endpoint is not proof the token works, and
        a token used with the wrong scheme fails silently as an empty listing,
        which is the failure mode this whole module exists to stop.
        """
        # Land on a portal page first so Django can set its csrftoken cookie;
        # the fetch helper reads that cookie and sends X-CSRFToken. Without it
        # every POST came back as Django's own "403 Forbidden" HTML page — the
        # CSRF middleware's response, which never reaches a view, and which is
        # easy to misread as "wrong password".
        try:
            if "unpartnerportal.org" not in (page.url or ""):
                page.goto(LOGIN_URL, timeout=90_000, wait_until="domcontentloaded")
                page.wait_for_timeout(1_500)
        except Exception:
            pass

        for path in LOGIN_ENDPOINTS:
            url = urljoin(BASE, path)
            for user_field, pass_field in LOGIN_FIELDS:
                r = self._fetch(page, url, {}, method="POST",
                                body={user_field: email, pass_field: password})
                if r["status"] in (404, 405):
                    break        # endpoint isn't there; field names won't help
                if r["status"] not in (200, 201):
                    body = (r["body"] or str(r["json"] or ""))[:200]
                    if r["status"] == 403 and "<html" in (r["body"] or "").lower():
                        body = ("Django's own 403 page — CSRF was rejected, so "
                                "this never reached a view. Not a credentials "
                                "problem.")
                    log.info("[%s] %s (%s) -> %s %s", self.name, path,
                             user_field, r["status"], body)
                    continue
                payload = r["json"] if isinstance(r["json"], dict) else {}
                token = ""
                for key in TOKEN_KEYS:
                    if isinstance(payload.get(key), str) and payload[key]:
                        token = payload[key]
                        break
                if not token:
                    # Some deployments answer 200 and set a session cookie
                    # instead of returning a token. The browser already holds
                    # that cookie now, so test it as-is.
                    if self._whoami(page, {}):
                        log.info("[%s] signed in via %s (session cookie)",
                                 self.name, path)
                        return {}
                    log.info("[%s] %s answered 200 but with no usable token "
                             "(keys: %s)", self.name, path,
                             list(payload)[:10])
                    continue
                for scheme in TOKEN_SCHEMES:
                    headers = {"Authorization": f"{scheme} {token}"}
                    if self._whoami(page, headers):
                        log.info("[%s] signed in via %s using an %s token",
                                 self.name, path, scheme)
                        return headers
                log.info("[%s] %s returned a token but none of %s "
                         "authenticated it", self.name, path,
                         "/".join(TOKEN_SCHEMES))
        return {}

    _EMAIL_SELECTORS = ("input[type='email']", "input[name='email']",
                        "input[id*='email' i]", "input[name='username']",
                        "input[type='text']")
    _SUBMIT_SELECTORS = ("button[type='submit']", "input[type='submit']",
                         "button:has-text('Continue')", "button:has-text('Next')",
                         "button:has-text('Log in')", "button:has-text('Login')",
                         "button:has-text('Sign in')", "button:has-text('Submit')")

    def _form_login(self, page, email: str, password: str) -> bool:
        """Drive the sign-in form, including the two-step email→password flow.

        The portal asks for the email first and only renders the password field
        after that step is submitted. The previous version navigated to /login
        and waited 30 seconds for `input[type=password]`, which on a two-step
        form never appears — so a page that was working perfectly was reported
        as broken. Its own diagnostic said so plainly once it printed the field
        list: `inputs=['email:email']`, one field, no password.

        Written to handle both shapes without knowing which it is: fill the
        email, look briefly for a password field already on the page, and only
        if there isn't one submit the email step and wait for it to appear.
        """
        try:
            page.goto(LOGIN_URL, timeout=90_000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("input", timeout=30_000)
            except Exception:
                self._report_login_page(page)   # nothing rendered at all
                return False

            user = self._first(page, self._EMAIL_SELECTORS)
            if user is None:
                self._report_login_page(page)
                return False
            user.fill(email)

            # Single-step form? Short wait — this is the "is the password field
            # already here" question, not the "has the next step loaded" one, so
            # it must not cost 30s on the two-step flow that is the common case.
            pw = self._wait_password(page, 2_500)
            if pw is None:
                log.info("[%s] no password field yet — submitting the email "
                         "step of a two-step sign-in", self.name)
                self._submit(page, user)
                pw = self._wait_password(page, 30_000)
            if pw is None:
                self._report_login_page(page)
                return False

            pw.fill(password)
            self._submit(page, pw)
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=45_000)
            except Exception:
                pass
            page.wait_for_timeout(3_000)
            return True
        except Exception as exc:                                # noqa: BLE001
            log.error("[%s] the form sign-in raised %s: %s",
                      self.name, type(exc).__name__, exc)
            return False

    @staticmethod
    def _first(page, selectors):
        """First element matching any of these selectors, or None."""
        for sel in selectors:
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el is not None:
                return el
        return None

    @staticmethod
    def _wait_password(page, timeout_ms: int):
        """The password field once it exists, or None within the timeout."""
        try:
            page.wait_for_selector("input[type='password']", timeout=timeout_ms,
                                   state="visible")
        except Exception:
            return None
        return page.query_selector("input[type='password']")

    def _submit(self, page, field) -> None:
        """Advance the form: click its submit control, else press Enter.

        Enter is the fallback rather than the default because a two-step form
        sometimes binds Enter to nothing at all, and a click on the visible
        button is what a person would do.
        """
        btn = self._first(page, self._SUBMIT_SELECTORS)
        if btn is not None:
            try:
                btn.click()
                return
            except Exception:
                pass
        try:
            field.press("Enter")
        except Exception:
            pass

    def _token_from_storage(self, page) -> dict:
        """An auth token the app left in localStorage/sessionStorage, verified.

        A single-page app that authenticates by token keeps it in browser
        storage and attaches it to its own requests; a plain `fetch` from this
        code would not, so after a successful form sign-in the session can look
        absent while being perfectly real. Every candidate is checked against
        /api/accounts/me/ before it is used — a long string under a key called
        "token" is a guess until the portal confirms it.
        """
        script = """() => {
            const out = {};
            for (const store of [localStorage, sessionStorage]) {
                for (let i = 0; i < store.length; i++) {
                    const k = store.key(i);
                    try { out[k] = store.getItem(k); } catch (e) {}
                }
            }
            return out;
        }"""
        try:
            entries = page.evaluate(script) or {}
        except Exception:
            return {}

        candidates: list[str] = []
        for key, raw in entries.items():
            if not isinstance(raw, str) or not raw:
                continue
            interesting = any(w in key.lower()
                              for w in ("token", "auth", "jwt", "session", "key"))
            # The value may be the token itself, or JSON holding it.
            if raw.startswith("{"):
                try:
                    blob = json.loads(raw)
                except ValueError:
                    blob = None
                if isinstance(blob, dict):
                    for tk in TOKEN_KEYS:
                        v = blob.get(tk)
                        if isinstance(v, str) and len(v) >= 20:
                            candidates.append(v)
            elif interesting and 20 <= len(raw) <= 4096 and " " not in raw:
                candidates.append(raw.strip('"'))

        seen: set[str] = set()
        for token in candidates:
            if token in seen:
                continue
            seen.add(token)
            for scheme in TOKEN_SCHEMES:
                headers = {"Authorization": f"{scheme} {token}"}
                if self._whoami(page, headers):
                    log.info("[%s] browser storage held a working %s token",
                             self.name, scheme)
                    return headers
        if candidates:
            log.info("[%s] browser storage held %s token-like value(s), none of "
                     "which authenticated", self.name, len(seen))
        return {}

    def _report_login_page(self, page) -> None:
        """Say what the login page actually contained, not just that it failed.

        "No email/password field found" is true and useless — it cannot tell
        apart a page that never rendered, a redirect to a corporate SSO host,
        and a genuinely redesigned form. Printing the final URL, the title and
        every input on the page separates all three in one line.
        """
        try:
            url = page.url or ""
            title = page.title() or ""
            inputs = page.eval_on_selector_all(
                "input",
                "els => els.map(e => (e.type||'') + ':' + (e.name||e.id||'?'))",
            )
        except Exception:
            url = title = ""
            inputs = []
        host = urlparse(url).netloc
        if host and "unpartnerportal.org" not in host:
            log.error("[%s] the login page redirected to %s — the portal has "
                      "moved sign-in to an external identity provider, so the "
                      "credential route cannot work. Use an imported session "
                      "instead (site_auth.import_session).", self.name, host)
        else:
            kinds = [i.split(":", 1)[0] for i in inputs]
            hint = ""
            if inputs and not any(k == "password" for k in kinds):
                hint = (" The page rendered but shows no password field, which "
                        "is the signature of a multi-step sign-in that did not "
                        "advance — check whether the email step reported an "
                        "error such as an unknown account.")
            elif not inputs:
                hint = " Nothing rendered at all — the page never loaded."
            log.error("[%s] no password field on %s after 30s. title=%r "
                      "inputs=%s.%s", self.name, url, title, inputs[:12], hint)
        self._dump(page, "unpp_login_form")

    def _discover_endpoint(self, page, seen: list[dict],
                           auth: dict | None = None) -> tuple[str, dict]:
        """The listing API URL + the headers to repeat it with.

        Preference order, and the reason for it:
          1. a 2xx response the app itself made that looks like a listing — the
             ground truth, filters and all;
          2. a FAILED app request that looks like a listing endpoint — the URL
             is still correct even though that particular call errored, and
             repeating it from here surfaces the real status and body;
          3. probing CANDIDATE_ENDPOINTS, which is the only guessing this module
             does, and it is verified before use rather than assumed.
        """
        # `auth` is what the sign-in step proved works (a token header, or {}
        # when a session cookie carries the identity). It takes precedence over
        # anything scraped off the app's own requests: the app may not have made
        # an authenticated call at all on a server where it started signed out,
        # which is exactly the case this scraper has to handle.
        auth = dict(auth or {})

        for entry in reversed(seen):
            if entry["status"] >= 400 or "/api/" not in entry["url"]:
                continue
            if not re.search(r"/projects?\b|/cfei\b", entry["url"], re.I):
                continue
            headers = {**self._auth_headers(entry["headers"]), **auth}
            probe = self._fetch(page, _with_page(entry["url"], 1, self.page_size),
                                headers)
            if probe["status"] == 200 and _payload_rows(probe["json"]) is not None:
                return entry["url"], headers

        headers = dict(auth)
        for entry in seen:
            headers = {**self._auth_headers(entry["headers"]), **auth} or headers

        for entry in reversed(seen):
            if entry["status"] < 400 or not re.search(
                    r"/projects?\b|/cfei\b", entry["url"], re.I):
                continue
            probe = self._fetch(page, _with_page(entry["url"], 1, self.page_size),
                                headers)
            log.warning("[%s] retrying the app's failed request %s -> %s %s",
                        self.name, entry["url"], probe["status"],
                        probe["body"][:300])
            if probe["status"] == 200 and _payload_rows(probe["json"]) is not None:
                return entry["url"], headers

        for path in CANDIDATE_ENDPOINTS:
            url = _with_page(urljoin(BASE, path), 1, self.page_size)
            probe = self._fetch(page, url, headers)
            rows = _payload_rows(probe["json"])
            log.info("[%s] probe %s -> %s%s", self.name, path, probe["status"],
                     f" ({len(rows)} row(s))" if rows is not None
                     else f" {probe['body'][:200]}")
            if probe["status"] == 200 and rows is not None:
                return urljoin(BASE, path), headers
        return "", headers

    @staticmethod
    def _auth_headers(request_headers: dict) -> dict:
        """Only the headers that carry identity, never the whole set.

        Replaying every recorded header would also replay content-length,
        cookie and sec-fetch-* values belonging to a different request, which
        browsers reject or silently override. Authorization is the one the SPA
        adds and fetch() would not.
        """
        out = {}
        for key in ("authorization", "x-csrftoken", "x-requested-with"):
            for k, v in (request_headers or {}).items():
                if k.lower() == key and v:
                    out[k] = v
        return out

    @staticmethod
    def _fetch(page, url: str, headers: dict, method: str = "GET",
               body=None) -> dict:
        try:
            return page.evaluate(_FETCH_JS, [url, headers or {}, method, body])
        except Exception as exc:                                # noqa: BLE001
            return {"status": -1, "json": None, "body": f"{type(exc).__name__}: {exc}",
                    "error": str(exc)}

    def _paginate(self, page, endpoint: str, headers: dict, queue: Queue,
                  stop_event, done) -> None:
        """Walk the API page by page, pushing each batch to the crawl loop."""
        total: int | None = None
        pushed = 0
        for n in range(1, self.max_pages + 1):
            if stop_event.is_set() or done.is_set():
                return
            url = _with_page(endpoint, n, self.page_size)
            resp = self._fetch(page, url, headers)
            if resp["status"] != 200:
                log.error("[%s] page %s: %s -> %s %s", self.name, n, url,
                          resp["status"], resp["body"][:300])
                return
            rows = _payload_rows(resp["json"])
            if rows is None:
                log.error("[%s] page %s returned 200 but no recognisable list "
                          "of records — the response shape has changed. Keys: %s",
                          self.name, n,
                          list(resp["json"])[:12] if isinstance(resp["json"], dict)
                          else type(resp["json"]).__name__)
                return
            if not rows:
                log.info("[%s] page %s is empty — %s record(s) in total",
                         self.name, n, pushed)
                return
            if total is None:
                total = _payload_count(resp["json"])
                if total is not None:
                    log.info("[%s] %s open call(s) to walk at %s per page",
                             self.name, total, self.page_size)
            pushed += len(rows)
            queue.put({"kind": "api", "rows": rows, "url": url})
            if total is not None and pushed >= total:
                log.info("[%s] walked all %s record(s)", self.name, total)
                return
            page.wait_for_timeout(int(settings.rate_limit_delay * 1000))
        log.warning("[%s] stopped at the %s-page safety cap", self.name, self.max_pages)

    def _dump(self, page, stem: str) -> None:
        """Save the page so a failure can be read rather than guessed at."""
        try:
            out = settings.log_dir.parent / "data" / "debug"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{stem}.html").write_text(page.content(), encoding="utf-8")
            log.info("[%s] saved the page to %s", self.name, out / f"{stem}.html")
        except Exception:
            pass


__all__ = ["UNPartnerPortalScraper"]
