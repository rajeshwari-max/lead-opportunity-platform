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
    expected_types: tuple[RecordType, ...] = ()
    excluded_types: tuple[RecordType, ...] = ()
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
        listing_url="https://search.worldbank.org/api/v2/procnotices",
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
            "Every row is stored with the listing url "
            "https://www.devnetjobsindia.org/rfp_assignments.aspx rather than a "
            "per-RFP link, because that one .aspx page IS the list. 86 rows in "
            "the 2026-08-29 database share it, so their dashboard links do not "
            "open the opportunity they name. Detail-link extraction is the fix."
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
    "fundsforngos": _c(
        key="fundsforngos", display_name="FundsForNGOs",
        expected_types=(RecordType.GRANT, RecordType.CALL_FOR_PROPOSALS),
        excluded_types=(RecordType.NEWS, RecordType.JOB),
        scope_status=ScopeStatus.CONFIRMED,
        owner_note=(
            "48,350 of 105,297 records saved — 45% of the whole database. Worth "
            "confirming the scope is grants and calls rather than the site's "
            "articles about grants."
        ),
    ),
}


DEFAULT_CONTRACT_NOTE = (
    "No one has stated what this source should yield. It is scraping, and its "
    "output is judged only by the generic opportunity gate — which reads titles "
    "and URLs, not the source's own type or status fields."
)


def contract_for(key: str, display_name: str = "") -> SourceContract:
    """The manifest for a source, or an honest placeholder saying there is none."""
    found = MANIFESTS.get(key)
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
        if contract.expected_types:
            if not any(rt == e.value or e.value in rt for e in contract.expected_types):
                return False, (f"source type {record_type!r} is not among this "
                               f"source's expected types")

    open_ = contract.status_is_open(source_status)
    if open_ is False:
        return False, f"source status {source_status!r} means closed"

    return True, ""
