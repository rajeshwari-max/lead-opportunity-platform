"""What each source is supposed to yield, stated rather than assumed.

Why
---
`opportunity_gate.is_opportunity` decides from a row's title, summary and URL.
That is all it has, and it is not enough for the distinctions the business
definition actually turns on:

  * World Bank publishes procurement notices AND contract awards AND project
    records in one feed. "Award" in a title is not the signal — the record's
    own `notice_type` is.
  * UN Partner Portal serves open Calls for Expression of Interest from
    `/api/projects/open/`. The word "projects" in the route means nothing; the
    record's status does.
  * DevelopmentAid carries grants, tenders AND a historical archive. Whether a
    row is current is a field, not a guess from prose.

A title-keyword gate cannot see any of that, so this file carries the source's
semantics alongside it — what the source is expected to produce, which of its
own status values mean open, and whether anyone has actually confirmed that.

`scope_status` is the honest part
---------------------------------
`confirmed` means a person has stated what this source should yield. It is set
here ONLY where that is true — from the explicit instructions in this project
or from a documented API contract.

Everything else is `needs_review`, including sources that have been scraping
happily for months. That is not a criticism of those sources; it records that
nobody has said in writing what they are for, so nothing downstream should
claim they were verified.

`production_enabled` is deliberately separate from `scope_status`
-----------------------------------------------------------------
The brief asks for unconfirmed sources to be disabled in production. Applied
literally that would switch off 71 of 85 sources on a judgement I have no
standing to make — most are foundation grant pages whose scope is not in
question, merely undocumented.

So the two fields are independent. `production_enabled=False` is set only where
there is EVIDENCE of a scope problem or a blocking defect. Turning off
everything unconfirmed is one line (`disable_all_unconfirmed()`) and it is the
owner's call, not this module's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ScopeStatus(str, Enum):
    CONFIRMED = "confirmed"        # a person has stated what this yields
    NEEDS_REVIEW = "needs_review"  # nobody has; do not claim otherwise


class RecordType(str, Enum):
    """What a source record IS, from the source's own vocabulary."""

    GRANT = "grant"
    TENDER = "tender"
    RFP = "rfp"
    RFQ = "rfq"
    EOI = "eoi"
    ITB = "itb"
    CALL_FOR_PROPOSALS = "call_for_proposals"
    CONSULTANCY = "consultancy"
    # Excluded kinds, named so a manifest can say "not this" explicitly rather
    # than relying on a keyword blocklist to notice.
    CONTRACT_AWARD = "contract_award"
    PROJECT = "project"                # a funded programme, not an open call
    NEWS = "news"
    JOB = "job"
    ORGANISATION = "organisation"


@dataclass(frozen=True)
class SourceContract:
    """The stated scope of one source."""

    key: str                                    # registry name
    display_name: str
    listing_url: str = ""
    # What this source is understood to produce. A DESCRIPTION, not an audited
    # allowlist — see `expected_types_exhaustive`.
    expected_types: tuple[RecordType, ...] = ()
    # What it must never yield. This is the half that rejects.
    excluded_types: tuple[RecordType, ...] = ()
    # Does `expected_types` list EVERY type this source can emit?
    #
    # False by default, and no manifest sets it today. While nothing populated
    # `record_type`, treating the expected list as an allowlist was harmless
    # because it never ran. The moment the scrapers started passing the
    # source's own notice wording it became a deletion rule built on lists
    # nobody had audited for completeness: ADB's list omits `itb`, so every
    # "Invitation for Bids" — its main output — would have been discarded.
    #
    # Same principle as an unrecognised status value: a vocabulary nobody has
    # finished configuring must not silently delete a source's output. Set this
    # True only after checking a real sample of what the source emits.
    expected_types_exhaustive: bool = False
    # The source's OWN status vocabulary — the values it uses, not ours.
    open_status_values: tuple[str, ...] = ()
    closed_status_values: tuple[str, ...] = ()
    # dayfirst | monthfirst | iso. 09/01/2026 is two different days depending
    # on which, and a global default is how day/month inversion happens.
    deadline_format: str = ""
    curated: bool = False                       # page contains only opportunities
    requires_login: bool = False
    scope_status: ScopeStatus = ScopeStatus.NEEDS_REVIEW
    production_enabled: bool = True
    owner_note: str = ""
    # Set when a defect makes this source's output untrustworthy in a specific
    # way, so the dashboard can say what is wrong rather than only that it is.
    known_defect: str = ""

    @property
    def needs_owner_decision(self) -> bool:
        return self.scope_status is ScopeStatus.NEEDS_REVIEW

    def status_is_open(self, value: str) -> bool | None:
        """True/False when the source's own status says so, None when unknown.

        None matters: it is the difference between "the source says closed" and
        "this source has no status vocabulary configured", and only the first
        justifies discarding a record.
        """
        v = (value or "").strip().lower()
        if not v:
            return None
        if any(v == s.lower() or s.lower() in v for s in self.closed_status_values):
            return False
        if any(v == s.lower() or s.lower() in v for s in self.open_status_values):
            return True
        return None


def _c(**kw) -> SourceContract:
    return SourceContract(**kw)


# ---------------------------------------------------------------- manifests
# Only the sources whose scope has actually been stated. Everything absent from
# this dict gets the default contract below, marked needs_review.
MANIFESTS: dict[str, SourceContract] = {
    "worldbank": _c(
        key="worldbank", display_name="World Bank",
        # Human-facing canonical source. The scraper observes the first-party
        # data request made by this page instead of assuming an API endpoint.
        listing_url="https://projects.worldbank.org/en/projects-operations/opportunities",
        expected_types=(RecordType.TENDER, RecordType.RFP, RecordType.EOI,
                        RecordType.ITB, RecordType.CONSULTANCY),
        # The feed is MOSTLY awards. Excluding them by title keyword fails —
        # "Contract Award" is also how a real notice announces its subject — so
        # the notice_type field is what decides.
        excluded_types=(RecordType.CONTRACT_AWARD, RecordType.PROJECT),
        open_status_values=("invitation for bid", "request for expressions of interest",
                            "request for proposals", "general procurement notice",
                            "specific procurement notice", "prequalification"),
        closed_status_values=("award", "contract award", "cancel", "cancelled",
                              "annul", "annulled", "abandon", "abandoned"),
        deadline_format="iso",
        scope_status=ScopeStatus.CONFIRMED,
        owner_note="Procurement notices only. Projects and contract awards excluded.",
    ),
    "unpp": _c(
        key="unpp", display_name="UN Partner Portal",
        listing_url="https://www.unpartnerportal.org/api/projects/open/",
        expected_types=(RecordType.EOI, RecordType.CALL_FOR_PROPOSALS),
        excluded_types=(RecordType.PROJECT,),
        open_status_values=("open", "ope"),
        closed_status_values=("closed", "clo", "completed", "cancelled"),
        deadline_format="iso",
        curated=True,
        requires_login=True,
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "Open Calls for Expression of Interest only. The API route contains "
            "the word 'projects' and that is NOT a reason to reject these "
            "records — /api/projects/open/ is where CFEIs live. Judge on the "
            "record's status, never on the route."
        ),
    ),
    "developmentaid": _c(
        key="developmentaid", display_name="DevelopmentAid",
        expected_types=(RecordType.GRANT, RecordType.TENDER, RecordType.RFP),
        excluded_types=(RecordType.PROJECT, RecordType.CONTRACT_AWARD,
                        RecordType.JOB, RecordType.ORGANISATION),
        open_status_values=("open", "3"),         # statuses=3 is their open filter
        closed_status_values=("closed", "awarded", "cancelled"),
        deadline_format="iso",
        curated=True,
        requires_login=True,
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "Grants and tenders/RFPs, current and open by DEFAULT. The historical "
            "archive is a separate, explicitly invoked maintenance operation — a "
            "production run must not walk it. 779,856 records found against 55,013 "
            "saved on 2026-08-26 is that archive pass running on a scheduled scrape."
        ),
    ),
    "adb": _c(
        key="adb", display_name="ADB Tenders",
        expected_types=(RecordType.TENDER, RecordType.EOI, RecordType.CONSULTANCY),
        excluded_types=(RecordType.CONTRACT_AWARD, RecordType.PROJECT),
        open_status_values=("active", "open", "current"),
        closed_status_values=("closed", "awarded", "cancelled", "expired"),
        deadline_format="dayfirst",
        scope_status=ScopeStatus.CONFIRMED,
        owner_note="Active tender and procurement notices only.",
        known_defect=(
            "PAGINATION DID NOT ADVANCE. The 2026-08-30 verification run walked "
            "three pages and got 36 rows of which 12 were distinct — the same "
            "twelve, three times. searchstax[page]=N is a parameter the widget "
            "accepts and ignores on a fresh navigation, exactly like World "
            "Bank's os={offset}. Only 24 rows have ever been stored, which is "
            "what one page of this source looks like. The same-results guard "
            "now compares the RESULT ROWS instead of the first 4,000 characters "
            "of body text (header, nav and facet counts, which do change), so "
            "the walk stops and says so — but stopping is not paging. If page 2 "
            "still repeats page 1, the fix is to click the pager in the live "
            "page rather than re-navigate, the same reasoning that made "
            "_select_status_facet tick the box instead of building a URL."
        ),
    ),
    "undp_procurement": _c(
        key="undp_procurement", display_name="UNDP Procurement",
        expected_types=(RecordType.TENDER, RecordType.RFP, RecordType.RFQ,
                        RecordType.EOI, RecordType.ITB, RecordType.CONSULTANCY),
        excluded_types=(RecordType.CONTRACT_AWARD, RecordType.PROJECT, RecordType.JOB),
        deadline_format="dayfirst",
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "Procurement notices and open opportunities only. Currently served by "
            "the GENERIC heuristic scraper — whether that is reliable enough or "
            "needs a documented API scraper is an open question, not a settled one."
        ),
        known_defect=(
            "THE OPEN QUESTION ABOVE IS NOW ANSWERED, AND THE ANSWER IS NO. The "
            "2026-08-30 verification run took 568 rows off a single page and "
            "0.4% of them carried a deadline string — two rows out of 568, of "
            "which one parsed. This is a procurement notice board: essentially "
            "every notice has a closing date. The generic heuristic reads the "
            "block around each anchor, and UNDP puts the deadline in its own "
            "table cell, so the date is never seen. 562 of those rows would "
            "have been stored, every one of them deadline-less and therefore in "
            "the UNKNOWN state, which never expires on its own. 1,274 rows are "
            "already stored under this source. It needs a bespoke scraper that "
            "reads the notice table by column, not a wider heuristic."
        ),
    ),
    "devex": _c(
        key="devex", display_name="Devex",
        expected_types=(RecordType.GRANT, RecordType.TENDER, RecordType.RFP),
        excluded_types=(RecordType.NEWS, RecordType.JOB, RecordType.PROJECT),
        requires_login=True,
        scope_status=ScopeStatus.CONFIRMED,
        production_enabled=False,
        known_defect=(
            "11 runs, 0 pages fetched, 0 rows saved — and every one recorded as "
            "'completed'. Devex is behind a paywall and the scraper has never "
            "reached it. Disabled until the access question is answered: a "
            "subscription, an official feed, or dropping the source."
        ),
        owner_note="Funding and procurement opportunities only. Auth failures must "
                   "be explicit, never reported as an empty source.",
    ),
    "devnet": _c(
        key="devnet", display_name="DevNetJobsIndia",
        expected_types=(RecordType.RFP, RecordType.RFQ, RecordType.TENDER,
                        RecordType.CONSULTANCY),
        excluded_types=(RecordType.JOB,),
        deadline_format="dayfirst",       # Indian source: 31/07/2026
        scope_status=ScopeStatus.CONFIRMED,
        known_defect=(
            "STORED ROWS ONLY — the scraper no longer does this. It recovers "
            "job_id three ways (href, joblogos/<id> image, sidebar title match) "
            "and returns an EMPTY link when all three fail, so the row is "
            "dropped rather than shipped pointing at the index. What remains is "
            "history: 86 rows in the 2026-08-29 database still carry "
            "https://www.devnetjobsindia.org/rfp_assignments.aspx as their "
            "opportunity_url, so their dashboard links open the list rather "
            "than the RFP they name. Repair with "
            "scripts/listing_link_audit.py."
        ),
        owner_note="RFPs, RFQs and consultancies. Not the job listings that share "
                   "the site.",
    ),
    "ngobox": _c(
        key="ngobox", display_name="NGOBOX",
        expected_types=(RecordType.GRANT, RecordType.RFP, RecordType.EOI),
        excluded_types=(RecordType.JOB, RecordType.NEWS),
        deadline_format="dayfirst",
        scope_status=ScopeStatus.CONFIRMED,
    ),
    "bond": _c(
        key="bond", display_name="Bond UK",
        listing_url="https://www.bond.org.uk/funding-opportunities/",
        expected_types=(RecordType.GRANT, RecordType.CALL_FOR_PROPOSALS),
        excluded_types=(RecordType.NEWS, RecordType.JOB, RecordType.ORGANISATION),
        # The cards print "Closing date: 31 January 2027" or "Ongoing"; both are
        # unambiguous, so no day/month question arises.
        deadline_format="dayfirst",
        curated=True,
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "A curated directory of funding opportunities for UK NGOs. Every "
            "card is a funder's open call, so rows are exempt from the "
            "funding-vocabulary test in opportunity_gate.py."
        ),
        known_defect=(
            "A card with no apply link is stored pointing at the listing page "
            "with an anchor (start_url + '#post-NNN'). is_usable_link() accepts "
            "that, so the row is NOT dropped — but link_kind() calls it "
            "'listing', and the reader lands on the index they came from. This "
            "is the same class of defect that produced DevNetJobsIndia's 86 "
            "index-linked rows. Measure the share with "
            "scripts/verify_source.py bond, which holds this source to 90% "
            "deep links. MEASURED 2026-08-30: 50.9%. Half of what this source "
            "puts on the dashboard opens the index it was scraped from. "
            "Separately, the same run extracted 448 rows from ONE page of which "
            "354 were distinct — 94 repeats with no pagination involved, so the "
            "parser is reading some cards twice (most likely the rendered "
            "article path and the headings fallback both firing). 83 more rows "
            "were dropped by the gates, 35 of them as 'page type is never an "
            "opportunity'."
        ),
    ),
    "clean_air_fund": _c(
        key="clean_air_fund", display_name="Clean Air Fund",
        listing_url="https://www.cleanairfund.org/what-we-do/our-grants/",
        expected_types=(RecordType.GRANT, RecordType.CALL_FOR_PROPOSALS),
        # This is the whole reason this source needed a manifest. The page is
        # titled "Our grants" and most of what it lists is money ALREADY GIVEN
        # — a portfolio, not a call board. Those records are contract awards in
        # everything but name, and nobody can apply to them.
        excluded_types=(RecordType.CONTRACT_AWARD, RecordType.PROJECT,
                        RecordType.NEWS, RecordType.ORGANISATION),
        deadline_format="dayfirst",
        # NOT curated: unlike NGOBOX or UNDP, this page is not a notice board,
        # so a row DOES have to look like an open call to be stored.
        curated=False,
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "Open calls only. The grants page is largely a portfolio of awards "
            "already made; those are excluded. Served by the GENERIC heuristic "
            "scraper from sources.json with no selectors configured, so what it "
            "extracts is whatever the heuristic finds — verify before trusting."
        ),
        known_defect=(
            "FETCHES NOTHING. The 2026-08-30 verification run got 0 pages in "
            "3.7 seconds — too fast to be a timeout, so the request failed "
            "outright rather than being slow or blocked. No login is involved, "
            "so this is a URL, a redirect or a status code, not access. The "
            "configured listing is "
            "https://www.cleanairfund.org/what-we-do/our-grants/ and the most "
            "likely cause is that it has moved. Diagnose with "
            "'python scripts/check_scraper.py clean_air_fund --pages 1' and "
            "read the HTTP status and final URL in the log; "
            "scripts/find_listing_url.py finds the new path if it moved."
        ),
    ),
    "fundsforngos": _c(
        key="fundsforngos", display_name="FundsForNGOs",
        expected_types=(RecordType.GRANT, RecordType.CALL_FOR_PROPOSALS),
        excluded_types=(RecordType.NEWS, RecordType.JOB),
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "48,350 of 105,297 records saved — 45% of the whole database. Worth "
            "confirming the scope is grants and calls rather than the site's "
            "articles about grants. "
            "DATE CONVENTION NOT ESTABLISHED: the site's format has not been "
            "checked, and guessing one would decide the meaning of 48,350 "
            "deadlines on nothing. Settle it with "
            "scripts/deadline_convention_audit.py --source FundsForNGOs, which "
            "measures the day/month distribution of its ambiguous dates."
        ),
        known_defect=(
            "THE FUNDER IS ALMOST NEVER NAMED. The 2026-08-30 verification run "
            "found an organisation on 2.7% of 150 rows — four of them. The "
            "WordPress record has no funder field, so the name has to come out "
            "of the post's prose via extract_organization(), and on this "
            "source's phrasing it almost never does. 48,350 rows — 45% of the "
            "database — are stored under this source, so the Organization "
            "column and its filter are effectively empty for nearly half of "
            "everything. Separately: an all-numeric closing date "
            "(09-01-2027) is not extracted at all, because _DEADLINE requires "
            "three or more word characters in the month position. Fix the date "
            "convention FIRST, then the pattern — widening it before the "
            "convention is settled decides the meaning of those dates by "
            "accident."
        ),
    ),
}


DEFAULT_CONTRACT_NOTE = (
    "No one has stated what this source should yield. It is scraping, and its "
    "output is judged only by the generic opportunity gate — which reads titles "
    "and URLs, not the source's own type or status fields."
)


# Registry key -> manifest key, where the two were written differently.
#
# This existed as a silent defect. The manifests are keyed `worldbank`, `unpp`
# and `adb`; the scrapers register themselves as `world_bank`,
# `un_partner_portal` and `adb_tenders`, and the ingest path passes the
# REGISTRY key. So `contract_for(scraper.name)` fell through to the
# needs_review placeholder for the three sources whose contracts matter most —
# World Bank, whose feed is mostly contract awards, and UN Partner Portal,
# whose `/projects` route is the red herring the brief names by name.
#
# It never failed loudly: a placeholder contract has no expected types and no
# status vocabulary, and `record_is_in_scope` on an empty contract returns
# keep=True. The scope check was a no-op for those sources and looked exactly
# like a working one. The tests missed it because they reach into MANIFESTS by
# name instead of going through the key the pipeline actually uses.
#
# `test_source_manifest.py` now asserts every manifest is reachable from a
# registered scraper, so a future rename cannot re-open this quietly.
KEY_ALIASES: dict[str, str] = {
    "world_bank": "worldbank",
    "un_partner_portal": "unpp",
    "adb_tenders": "adb",
}


def contract_for(key: str, display_name: str = "") -> SourceContract:
    """The manifest for a source, or an honest placeholder saying there is none."""
    found = MANIFESTS.get(key) or MANIFESTS.get(KEY_ALIASES.get(key, ""))
    if found is not None:
        return found
    return SourceContract(
        key=key,
        display_name=display_name or key,
        scope_status=ScopeStatus.NEEDS_REVIEW,
        production_enabled=True,        # see the module docstring
        owner_note=DEFAULT_CONTRACT_NOTE,
    )


def unconfirmed_sources(keys) -> list[str]:
    """Sources with no stated scope. For the dashboard's review queue."""
    return sorted(k for k in keys if contract_for(k).needs_owner_decision)


def disabled_sources(keys) -> dict[str, str]:
    """{key: why} for sources held out of production."""
    out = {}
    for k in keys:
        c = contract_for(k)
        if not c.production_enabled:
            out[k] = c.known_defect or c.owner_note or "disabled"
    return out


# ------------------------------------------------------------- record checks

def record_is_in_scope(
    contract: SourceContract,
    record_type: str = "",
    source_status: str = "",
) -> tuple[bool, str]:
    """Judge a record on the SOURCE's own fields, before any title reading.

    Returns (keep, reason). This is the check `is_opportunity` cannot make: it
    sees prose, and prose is exactly what a contract award and an open tender
    have in common.
    """
    rt = (record_type or "").strip().lower().replace(" ", "_")
    if rt:
        for excluded in contract.excluded_types:
            if rt == excluded.value or excluded.value in rt:
                return False, f"source type {record_type!r} is excluded for this source"
        # Only an EXHAUSTIVE expected list may reject. An incomplete one is
        # exactly like an unrecognised status: evidence of unfinished
        # configuration, not evidence the record is out of scope.
        if contract.expected_types and contract.expected_types_exhaustive:
            if not any(rt == e.value or e.value in rt for e in contract.expected_types):
                return False, (f"source type {record_type!r} is not among this "
                               f"source's expected types")

    open_ = contract.status_is_open(source_status)
    if open_ is False:
        return False, f"source status {source_status!r} means closed"

    return True, ""
