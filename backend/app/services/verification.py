"""What "this source is working correctly" means, stated per source.

The problem
-----------
`check_scraper.py` already prints good numbers for one source. What it cannot do
is say whether those numbers are ACCEPTABLE, because nothing anywhere states
what acceptable is. So every run ends in a human squinting at percentages and
deciding on the spot — which means the answer changes with the reader, and
"DevelopmentAid is fine now" is an opinion rather than a check that passed.

This module is the missing half: a per-source contract with thresholds, and an
evaluator that turns one run's measurements into a pass or a list of named
failures. It is deliberately separate from `source_manifest.py`, which says what
a source is FOR. This says what a good run of it looks like.

The coverage rule, which is the important one
---------------------------------------------
Coverage is `unique / official_total`, and `official_total` means a number the
SOURCE stated — an API `total`, a "N results" line, or a manually verified
complete listing. Not our own count of what we found, which would make coverage
100% by construction and is exactly the reassuring lie this is written to
prevent.

When no official total is available, `coverage_pct` is None and stays None. A
source in that state cannot pass a coverage threshold; it reports
`coverage: unproven` and names what would prove it. That is a worse-looking
report and a truer one.

Three failure kinds, kept apart
-------------------------------
  * BLOCKING   the run did not produce trustworthy data (no pages, auth wall,
               parser found nothing). Nothing downstream should be believed.
  * QUALITY    data arrived but misses a stated threshold (deadlines not
               parsing, links pointing at the index).
  * UNPROVEN   a claim could not be checked at all. Never silently a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    BLOCKING = "blocking"
    QUALITY = "quality"
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    check: str
    detail: str

    def __str__(self) -> str:            # pragma: no cover - display only
        return f"[{self.severity.value.upper():<8}] {self.check}: {self.detail}"


@dataclass(frozen=True)
class VerificationContract:
    """The bar one source has to clear, and why it is where it is."""

    key: str
    display_name: str = ""

    # --- quality thresholds, as percentages of the rows produced -------------
    # A row whose link opens the index instead of the call is not a lead; the
    # reader clicks and lands where they started.
    #
    # Measured over the rows that would be STORED, not over everything
    # extracted. The two differ whenever a scraper drops its own bad rows, and
    # DevNetJobsIndia is built to do exactly that: it returns an empty link when
    # no job_id can be recovered so the row is dropped rather than shipped
    # pointing at the index. Measuring over `extracted` scored it 65.6% against
    # a 100% bar and called it a failure — for behaving correctly. The dropped
    # rows are still counted, as `link_loss_pct`, because losing a row and
    # shipping a bad one are different problems and both deserve a number.
    min_deep_link_pct: float = 80.0
    # How much of the source's output is lost because no usable link could be
    # built. Not a defect in itself — dropping is the right call — but a high
    # figure means the source is publishing calls we cannot link to.
    max_link_loss_pct: float = 20.0
    # Of the rows that carry a deadline STRING, how many must parse into a
    # date. Not "how many rows have deadlines" — a source that prints no dates
    # is a fact about the source; a date string we cannot read is a defect in
    # us. Those are different problems and only the second is ours.
    min_deadline_parse_pct: float = 95.0
    # Naming who is funding or procuring. Low for sources that genuinely do not
    # publish it in the listing; raised per source where they do.
    min_organization_pct: float = 50.0
    # Repeats within one run's output. A few are normal on a paginated site
    # whose ordering shifts mid-walk; a lot means pagination is re-serving the
    # same page, which is the failure that looks like success.
    max_duplicate_pct: float = 5.0
    # A percentage on its own is unusable at small sample sizes: one repeat in
    # 87 rows is 1.1%, which failed World Bank's 1% bar on the strength of a
    # single row. One duplicate is not evidence of anything — a listing whose
    # order shifted between two requests produces it — so a run has to exceed
    # BOTH the percentage and this count before the finding is raised.
    min_duplicates_before_failing: int = 3
    # Navigation furniture stored as opportunities. Zero, always: one is a
    # parser reading the wrong element, and there is no acceptable amount.
    max_furniture: int = 0

    # --- coverage -----------------------------------------------------------
    # How the source's own total can be obtained, in words. Empty means nobody
    # has found a way, and coverage for this source is structurally unprovable
    # until someone does.
    official_total_source: str = ""
    # The bar, when a total IS available. None means "report it, do not gate on
    # it" — right for a source walked as a deliberately bounded recent window,
    # where low coverage of the full archive is the design and not a defect.
    min_coverage_pct: float | None = None

    # --- operational --------------------------------------------------------
    # Seconds. Above the largest legitimate run, not near the average.
    max_runtime_s: float = 2700.0
    # Browsers still alive after the run, over the pre-run baseline. Always 0.
    max_leaked_browsers: int = 0
    # Stated, not discovered at 3am: what this source will not let us have.
    access_limitations: str = ""
    # A run of this source cannot be verified without these. Named so a report
    # says "unproven because no session" rather than showing a confident zero.
    requires: tuple[str, ...] = ()


# --------------------------------------------------------------- the contracts
#
# Every threshold below is set from what the source actually publishes, and the
# reason is on the line. A number with no reason is a number nobody can argue
# with later, which is how thresholds become folklore.

CONTRACTS: dict[str, VerificationContract] = {
    "world_bank": VerificationContract(
        key="world_bank", display_name="World Bank",
        # The canonical page's first-party data response carries a notice id,
        # so a constructed detail URL is available when no direct link is given.
        min_deep_link_pct=99.0,
        # ISO dates from an API. Anything that fails to parse is our bug.
        min_deadline_parse_pct=100.0,
        # `borrower`/`project_name` is present on essentially every record.
        min_organization_pct=95.0,
        max_duplicate_pct=1.0,
        official_total_source=(
            "the total field in the first-party procnotices response observed "
            "from the canonical Business Opportunities page"),
        # Deliberately NOT gated. The total is 416,361 — the entire historical
        # archive — and this source walks a bounded 60-page window of the newest
        # notices on purpose. Coverage against that total is ~1.4% by design,
        # and a threshold here would fail a correct scraper every night.
        min_coverage_pct=None,
        max_runtime_s=900.0,
        access_limitations=(
            "Public JavaScript page; requires Chromium. Data endpoints are used "
            "only when the canonical page itself requests them."),
    ),
    "adb_tenders": VerificationContract(
        key="adb_tenders", display_name="ADB Tenders",
        min_deep_link_pct=95.0,
        min_deadline_parse_pct=98.0,
        min_organization_pct=80.0,
        official_total_source=(
            "the result-count line the SearchStax listing renders above the "
            "first result (\"N results\")"),
        min_coverage_pct=90.0,
        max_runtime_s=1800.0,
        access_limitations="Client-rendered listing; needs a browser. No login.",
        requires=("a working Chromium",),
    ),
    "un_partner_portal": VerificationContract(
        key="un_partner_portal", display_name="UN Partner Portal",
        min_deep_link_pct=99.0,
        min_deadline_parse_pct=100.0,     # ISO dates from a JSON API
        min_organization_pct=90.0,
        official_total_source=(
            "the `count` field in the /api/projects/open/ response"),
        # This one IS gated: the API publishes an exact total of OPEN calls and
        # the walk is supposed to reach all of them. Anything short is a
        # pagination defect, not a design choice.
        min_coverage_pct=98.0,
        max_runtime_s=1800.0,
        access_limitations=(
            "Requires a signed-in session. Without one the API answers 403 and "
            "the correct outcome is AUTH_REQUIRED, never an empty source."),
        requires=("a connected UNPP session",),
    ),
    "developmentaid": VerificationContract(
        key="developmentaid", display_name="DevelopmentAid",
        min_deep_link_pct=95.0,
        min_deadline_parse_pct=98.0,
        min_organization_pct=70.0,
        # The search-partition walk revisits records across slices by design —
        # the same tender legitimately matches several searches. Deduplication
        # is the mechanism, so a high in-run duplicate rate here is expected
        # and only the UNIQUE count means anything.
        max_duplicate_pct=60.0,
        official_total_source=(
            "the result count the open-tenders search reports for statuses=3 "
            "with no keyword — read from the page by a signed-in person, since "
            "the count is behind the same session as the data"),
        # Not gated, and this is the honest part: the walk is bounded by
        # walk_budget (searches, duration, records), so it is not TRYING to
        # reach the total. Report the fraction; do not pretend the cap is a
        # coverage failure or that reaching it would be a success.
        min_coverage_pct=None,
        max_runtime_s=1800.0,
        access_limitations=(
            "Login is reCAPTCHA-protected and their terms restrict scripted "
            "sign-in. The session is established by a person in a real browser "
            "and reused; it is never scripted, and credentials never enter the "
            "codebase. See docs/DEVELOPMENTAID_SESSION.md."),
        requires=("a person-established DevelopmentAid session",),
    ),
    "devnet": VerificationContract(
        key="devnet", display_name="DevNetJobsIndia",
        # The postback-only rows are dropped rather than pointed at the index,
        # so every row that survives must have a real job_id link.
        min_deep_link_pct=100.0,
        min_deadline_parse_pct=95.0,
        min_organization_pct=60.0,
        official_total_source=(
            "the ASP.NET pager's last page number x the grid's page size, read "
            "off rfp_assignments.aspx"),
        min_coverage_pct=90.0,
        max_runtime_s=900.0,
        access_limitations="None. Public ASP.NET WebForms site; pagination is a POST.",
    ),
    "fundsforngos": VerificationContract(
        key="fundsforngos", display_name="FundsForNGOs",
        min_deep_link_pct=99.0,           # every post has a permalink
        # LOWER on purpose, and this is a known-unknown rather than a low bar:
        # the site's date convention has never been established, so a date that
        # "parses" may still be parsing to the wrong day. See the manifest note.
        min_deadline_parse_pct=90.0,
        # The funder is named in prose, not in a field. Recovering it from text
        # works often, not always.
        min_organization_pct=40.0,
        official_total_source=(
            "the X-WP-Total response header on any /wp-json/wp/v2/posts request"),
        # The WordPress total counts every post ever published, most of them
        # long-closed articles. Same shape as World Bank: report, do not gate.
        min_coverage_pct=None,
        max_runtime_s=2700.0,
        access_limitations=(
            "The HTML site shows bot-check interstitials; the open WordPress "
            "REST API is used instead. No login."),
    ),
    "ngobox": VerificationContract(
        key="ngobox", display_name="NGOBOX",
        min_deep_link_pct=99.0,
        min_deadline_parse_pct=95.0,
        min_organization_pct=85.0,        # p.p_balck carries it on every card
        official_total_source=(
            "the pager's last page number x the cards per page on "
            "grant_announcement_listing.php"),
        min_coverage_pct=90.0,
        max_runtime_s=900.0,
        access_limitations=(
            "Serves stale cached pages to plain HTTP clients, so it is rendered "
            "through a browser. No login."),
        requires=("a working Chromium",),
    ),
    "bond": VerificationContract(
        key="bond", display_name="Bond UK",
        min_deep_link_pct=90.0,
        min_deadline_parse_pct=90.0,
        min_organization_pct=50.0,
        official_total_source=(
            "the listing's own result count on /funding-opportunities/"),
        min_coverage_pct=90.0,
        max_runtime_s=900.0,
        access_limitations="None. Public listing.",
    ),
    "undp_procurement": VerificationContract(
        key="undp_procurement", display_name="UNDP Procurement",
        min_deep_link_pct=90.0,
        min_deadline_parse_pct=90.0,
        min_organization_pct=60.0,
        official_total_source=(
            "the notice count procurement-notices.undp.org prints above the "
            "table"),
        min_coverage_pct=90.0,
        max_runtime_s=1800.0,
        access_limitations=(
            "Served by the GENERIC heuristic scraper, not a documented "
            "endpoint. Whether that is reliable enough is an open question — "
            "see the manifest note."),
    ),
    "clean_air_fund": VerificationContract(
        key="clean_air_fund", display_name="Clean Air Fund",
        min_deep_link_pct=90.0,
        # A grants page, not a tender board: many entries are described without
        # a closing date at all. That is the source, not a defect.
        min_deadline_parse_pct=90.0,
        min_organization_pct=40.0,
        official_total_source=(
            "a manual count of the entries on /what-we-do/our-grants/ — the "
            "page publishes no total"),
        min_coverage_pct=90.0,
        max_runtime_s=600.0,
        access_limitations=(
            "A grants PAGE rather than an opportunity board. Much of what it "
            "lists is already-awarded grants, which are not open calls."),
    ),
    "devex": VerificationContract(
        key="devex", display_name="Devex",
        min_deep_link_pct=90.0,
        min_deadline_parse_pct=95.0,
        min_organization_pct=80.0,
        official_total_source="",          # nothing reachable without a subscription
        min_coverage_pct=None,
        max_runtime_s=900.0,
        access_limitations=(
            "PAYWALLED. 11 runs, 0 pages fetched, 0 rows — all recorded as "
            "'completed'. Nothing can be verified until access is resolved: a "
            "subscription, an official feed, or dropping the source."),
        requires=("a Devex subscription or licensed feed",),
    ),
}

# The eleven the brief names, in its order. Exported so a report cannot quietly
# cover ten of them.
PRIORITY_SOURCES: tuple[str, ...] = (
    "developmentaid", "devex", "devnet", "fundsforngos", "clean_air_fund",
    "world_bank", "adb_tenders", "undp_procurement", "un_partner_portal",
    "ngobox", "bond",
)


def contract_for(key: str) -> VerificationContract:
    """The bar for one source, or a permissive default that says it has none."""
    found = CONTRACTS.get(key)
    if found is not None:
        return found
    return VerificationContract(
        key=key, display_name=key,
        access_limitations="No verification contract has been written for this source.",
    )


# ------------------------------------------------------------------- results

@dataclass
class SourceVerification:
    """One run of one source, measured against its contract.

    Every count is separate on purpose. "found 300, saved 12" is unreadable
    without knowing whether the other 288 were duplicates, closed calls, or
    rows the parser mangled — and those are three different situations, only
    some of which are a problem.
    """

    key: str
    display_name: str = ""

    # --- counts -------------------------------------------------------------
    official_total: int | None = None      # what the SOURCE says exists
    accessible: int | None = None          # what it will show us (auth, filters)
    extracted: int = 0                     # rows the parser produced
    unique: int = 0                        # distinct after in-run dedupe
    duplicates: int = 0                    # extracted - unique
    saved: int = 0                         # rows that would reach the database
    excluded: dict[str, int] = field(default_factory=dict)   # reason -> count

    # --- pagination ---------------------------------------------------------
    pages_expected: int | None = None
    pages_fetched: int = 0

    # --- completeness, as percentages of `extracted` ------------------------
    # Of the rows that would REACH THE DASHBOARD. This is the number a reader
    # experiences: how often clicking a row opens the call rather than the
    # index it was scraped from.
    deep_link_pct: float = 0.0
    # Of everything extracted, including rows the scraper drops. Kept apart
    # from the line above because a scraper that drops its own unlinkable rows
    # is behaving correctly and must not be scored as if it shipped them.
    deep_link_extracted_pct: float = 0.0
    # Rows the source published that were dropped for want of a usable link.
    link_loss_pct: float = 0.0
    deadline_present_pct: float = 0.0
    deadline_parse_pct: float = 0.0        # of those that carry a string
    organization_pct: float = 0.0
    furniture_rows: int = 0

    # --- operational --------------------------------------------------------
    runtime_s: float = 0.0
    browsers_before: int | None = None
    browsers_after: int | None = None
    health_state: str = "unknown"
    outcome: str = ""

    # --- provenance ---------------------------------------------------------
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- derived
    @property
    def duplicate_pct(self) -> float:
        return 100.0 * self.duplicates / self.extracted if self.extracted else 0.0

    @property
    def leaked_browsers(self) -> int | None:
        """Browsers still alive over the pre-run baseline, or None if unmeasured.

        None is not zero. An unmeasured leak check reported as "0 leaked" is
        the reassuring lie that lets a leak run for weeks.
        """
        if self.browsers_before is None or self.browsers_after is None:
            return None
        return max(0, self.browsers_after - self.browsers_before)

    @property
    def coverage_pct(self) -> float | None:
        """unique / official_total, or None when there is no official total.

        None is the whole point. Dividing by our own count would make this
        100% for a scraper that reached one page of nine hundred.
        """
        if not self.official_total or self.official_total <= 0:
            return None
        return 100.0 * self.unique / self.official_total

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    # ------------------------------------------------------------ evaluation
    def evaluate(self, contract: VerificationContract | None = None) -> list[Finding]:
        """Measurements against the bar. Empty list means the source passed."""
        c = contract or contract_for(self.key)
        out: list[Finding] = []

        def add(sev: Severity, check: str, detail: str) -> None:
            out.append(Finding(sev, check, detail))

        # --- blocking: is there anything here to judge at all? ---------------
        if self.pages_fetched == 0:
            add(Severity.BLOCKING, "fetch",
                f"no page was fetched (outcome: {self.outcome or 'unrecorded'}). "
                f"Nothing below is measurable.")
            return out                     # everything else would be noise
        if self.extracted == 0:
            add(Severity.BLOCKING, "parse",
                f"{self.pages_fetched} page(s) fetched and the parser produced "
                f"no rows. Compare the saved capture against tests/fixtures/.")
            return out

        # --- quality ---------------------------------------------------------
        if self.deep_link_pct < c.min_deep_link_pct:
            add(Severity.QUALITY, "links",
                f"{self.deep_link_pct:.1f}% of the rows that would be STORED "
                f"link to a specific opportunity, below the "
                f"{c.min_deep_link_pct:.0f}% this source is held to. The rest "
                f"open a listing, so the reader lands where they started.")
        if self.link_loss_pct > c.max_link_loss_pct:
            # Separate finding, separate meaning: these rows are dropped rather
            # than shipped, which is correct behaviour — but a source losing a
            # fifth of its output is publishing calls we cannot link to, and
            # that is a coverage problem wearing a quality problem's clothes.
            add(Severity.QUALITY, "link loss",
                f"{self.link_loss_pct:.1f}% of extracted rows were dropped for "
                f"want of a usable link, above {c.max_link_loss_pct:.0f}%. They "
                f"are not bad rows on the dashboard — they are calls the source "
                f"published that never reach it.")
        if self.deadline_parse_pct < c.min_deadline_parse_pct:
            add(Severity.QUALITY, "deadlines",
                f"{self.deadline_parse_pct:.1f}% of the deadline strings parse "
                f"into a date, below {c.min_deadline_parse_pct:.0f}%. Rows whose "
                f"date never parses cannot expire.")
        if self.organization_pct < c.min_organization_pct:
            add(Severity.QUALITY, "organization",
                f"{self.organization_pct:.1f}% of rows name the funder or "
                f"procuring agency, below {c.min_organization_pct:.0f}%.")
        if (self.duplicate_pct > c.max_duplicate_pct
                and self.duplicates >= c.min_duplicates_before_failing):
            add(Severity.QUALITY, "duplicates",
                f"{self.duplicates} of {self.extracted} extracted rows repeat "
                f"within the run ({self.duplicate_pct:.1f}%, above "
                f"{c.max_duplicate_pct:.0f}%). Check that pagination advances "
                f"rather than re-serving a page.")
        if self.furniture_rows > c.max_furniture:
            add(Severity.QUALITY, "furniture",
                f"{self.furniture_rows} row(s) are navigation furniture, not "
                f"opportunities.")

        # --- pagination -------------------------------------------------------
        if self.pages_expected is not None and self.pages_fetched < self.pages_expected:
            add(Severity.QUALITY, "pagination",
                f"fetched {self.pages_fetched} of {self.pages_expected} expected "
                f"page(s); the walk stopped early.")

        # --- operational ------------------------------------------------------
        if self.runtime_s > c.max_runtime_s:
            add(Severity.QUALITY, "runtime",
                f"{self.runtime_s:.0f}s, over the {c.max_runtime_s:.0f}s this "
                f"source is allowed.")
        leaked = self.leaked_browsers
        if leaked is None:
            add(Severity.UNPROVEN, "browsers",
                "no before/after browser count was taken, so a leak would not "
                "have been noticed. Take the baseline before the run.")
        elif leaked > c.max_leaked_browsers:
            add(Severity.BLOCKING, "browsers",
                f"{leaked} browser process(es) survived the run. They hold "
                f"memory until the service is restarted.")

        # --- coverage, and the honesty rule -----------------------------------
        coverage = self.coverage_pct
        if coverage is None:
            add(Severity.UNPROVEN, "coverage",
                "no official total was supplied, so coverage is unproven and is "
                "NOT reported as a percentage. Obtain it from "
                + (c.official_total_source or
                   "— no method is known for this source, which is itself the "
                   "finding")
                + ".")
        elif c.min_coverage_pct is not None and coverage < c.min_coverage_pct:
            add(Severity.QUALITY, "coverage",
                f"{coverage:.1f}% of the {self.official_total:,} the source "
                f"reports, below {c.min_coverage_pct:.0f}%.")

        # --- preconditions the run has to have had ----------------------------
        # A source that needs a session and produced rows anyway is interesting
        # — but a report that does not RECORD which session was used cannot be
        # reproduced, and "it worked on my laptop" is the whole failure mode
        # this brief exists to close.
        for need in c.requires:
            if not any(need.lower() in n.lower() for n in self.notes):
                add(Severity.UNPROVEN, "precondition",
                    f"this source requires {need}, and the run recorded nothing "
                    f"about it. Add a note saying which one was used.")
        return out

    def passed(self, contract: VerificationContract | None = None) -> bool:
        """No blocking and no quality findings. UNPROVEN does not fail a run —
        it is recorded so nobody reads silence as proof."""
        return not [f for f in self.evaluate(contract)
                    if f.severity is not Severity.UNPROVEN]

    # ---------------------------------------------------------------- output
    def as_dict(self) -> dict:
        c = contract_for(self.key)
        findings = self.evaluate(c)
        return {
            "source_key": self.key,
            "display_name": self.display_name or self.key,
            "counts": {
                "official_total": self.official_total,
                "accessible": self.accessible,
                "extracted": self.extracted,
                "unique": self.unique,
                "duplicates": self.duplicates,
                "saved": self.saved,
                "excluded": dict(self.excluded),
                "excluded_total": self.excluded_total,
            },
            "pagination": {
                "expected": self.pages_expected,
                "fetched": self.pages_fetched,
            },
            "completeness_pct": {
                "deep_link": round(self.deep_link_pct, 1),
                "deep_link_of_extracted": round(self.deep_link_extracted_pct, 1),
                "link_loss": round(self.link_loss_pct, 1),
                "deadline_present": round(self.deadline_present_pct, 1),
                "deadline_parses": round(self.deadline_parse_pct, 1),
                "organization": round(self.organization_pct, 1),
            },
            # A string, not a number, when it is unproven — so no dashboard,
            # spreadsheet or summary can average it into a made-up figure.
            "coverage_pct": (round(self.coverage_pct, 1)
                             if self.coverage_pct is not None else "unproven"),
            "coverage_basis": c.official_total_source or "no known method",
            "operational": {
                "runtime_s": round(self.runtime_s, 1),
                "browsers_before": self.browsers_before,
                "browsers_after": self.browsers_after,
                "leaked_browsers": self.leaked_browsers,
                "health_state": self.health_state,
                "outcome": self.outcome,
            },
            "access_limitations": c.access_limitations,
            "findings": [{"severity": f.severity.value, "check": f.check,
                          "detail": f.detail} for f in findings],
            "passed": self.passed(c),
            "notes": list(self.notes),
        }


def summarize(results) -> dict:
    """Fleet view across several sources, with nothing rounded up.

    `verified` counts sources that passed. `unproven_coverage` is reported
    beside it rather than folded into it, because a source that passed every
    measurable check but cannot prove coverage is not the same as one that
    proved it.
    """
    results = list(results)
    return {
        "sources": len(results),
        "passed": sum(1 for r in results if r.passed()),
        "failed": sum(1 for r in results if not r.passed()),
        "unproven_coverage": sum(1 for r in results if r.coverage_pct is None),
        "blocking": sum(
            1 for r in results
            for f in r.evaluate() if f.severity is Severity.BLOCKING),
    }
