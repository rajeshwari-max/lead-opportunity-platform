"""What actually happened on a scrape run — decided from evidence, never guessed.

The problem this solves
-----------------------
The baseline of 2026-08-29 found 916 recorded runs:

    completed  792
    running    106
    stopped     18
    failed       0

Not one run has ever been marked failed. Sixteen sources have fetched zero
pages and saved zero rows across 127 attempts, and all 127 are stored as
"completed" — because `completed` was only ever set at the end of the crawl
loop, so it means "the function returned", not "the source was read".

That single fact is why nobody could tell from the dashboard that Devex, Gates
Foundation, Open Society, Laudes, Nippon and eleven others had been dead for
weeks.

The rule
--------
A zero is never reported without a reason, and a reason is never inferred from
the absence of data. Concretely:

  * zero pages fetched            -> NO_FETCH, plus the transport-level reason
  * pages fetched, zero extracted -> PARSE_ZERO — the parser's problem, NOT
                                     "the source is empty"
  * "the source is empty"         -> CONFIRMED_EMPTY, and ONLY on positive
                                     evidence: the API said total 0, or the
                                     page said so in words, or every notice
                                     fetched carried a closed status
  * a parser that used to work    -> STRUCTURE_CHANGED, and only when the
                                     structure signature actually differs from
                                     the last successful run

CONFIRMED_EMPTY is the dangerous one and has the strictest bar. A source that
is genuinely empty today must keep being checked cheaply, because tomorrow it
may not be; a source wrongly marked empty stops being investigated. So the
classifier will return PARSE_ZERO — "we do not know" — rather than promote a
guess, and `Evidence.empty_proof` must name what was actually observed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    """Terminal states a run can end in. Stored in ScrapeRun.outcome."""

    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_NO_NEW = "success_no_new"          # extracted rows, all already stored
    CONFIRMED_EMPTY = "confirmed_empty"        # proven no open calls right now
    NO_FETCH = "no_fetch"                      # never got a usable page/response
    PARSE_ZERO = "parse_zero"                  # page loaded, parser found nothing
    STRUCTURE_CHANGED = "structure_changed"    # positive evidence of parser drift
    BLOCKED = "blocked"                        # bot wall / WAF / 403 by policy
    AUTH_REQUIRED = "auth_required"
    SESSION_EXPIRED = "session_expired"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"                    # a person pressed Stop
    CRASHED = "crashed"                        # unhandled exception
    STALE_RUN_RECOVERED = "stale_run_recovered"  # assigned by startup reconciliation

    @property
    def is_healthy(self) -> bool:
        """Did this run read the source successfully, whatever it found?"""
        return self in (
            Outcome.SUCCESS_WITH_RESULTS,
            Outcome.SUCCESS_NO_NEW,
            Outcome.CONFIRMED_EMPTY,
        )

    @property
    def is_actionable(self) -> bool:
        """Does a human need to do something? CONFIRMED_EMPTY deliberately not:
        an empty source is working correctly and must stay enabled."""
        return not self.is_healthy and self not in (Outcome.CANCELLED,)

    @property
    def next_action(self) -> str:
        """What the dashboard should tell someone to do. One line, imperative."""
        return _NEXT_ACTION[self]


_NEXT_ACTION: dict[Outcome, str] = {
    Outcome.SUCCESS_WITH_RESULTS: "Nothing — working.",
    Outcome.SUCCESS_NO_NEW: "Nothing — read the source, everything was already stored.",
    Outcome.CONFIRMED_EMPTY: "Nothing — no open calls right now. Keep checking on schedule.",
    Outcome.NO_FETCH: "Check the URL, network and status code in this run's error detail.",
    Outcome.PARSE_ZERO: "Open the saved debug capture; the page loaded but the parser read nothing.",
    Outcome.STRUCTURE_CHANGED: "The site's markup changed. Compare the fixture against the last "
                               "good signature and update the parser or source config.",
    Outcome.BLOCKED: "The site refused automated access. Consider an official API or feed.",
    Outcome.AUTH_REQUIRED: "This source needs a login that is not configured.",
    Outcome.SESSION_EXPIRED: "Reconnect the account, then re-run.",
    Outcome.TIMED_OUT: "Raise the per-source timeout or narrow the search; check for a hang.",
    Outcome.CANCELLED: "Nothing — a person stopped this run.",
    Outcome.CRASHED: "Read the traceback in this run's error detail.",
    Outcome.STALE_RUN_RECOVERED: "Nothing — the process died and startup tidied the record.",
}


# Transport-level reasons for NO_FETCH. The outcome says a page was never read;
# the code says why, because "blocked" and "DNS failed" need different people.
class ErrorCode(str, Enum):
    DNS = "dns"
    TLS = "tls"
    TIMEOUT = "timeout"
    HTTP_4XX = "http_4xx"
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    CHALLENGE = "challenge"            # Cloudflare / bot interstitial
    LOGIN_WALL = "login_wall"
    BROWSER_LAUNCH = "browser_launch"
    NAVIGATION = "navigation"
    UNKNOWN = "unknown"


# Text a site shows when it is genuinely empty. Deliberately narrow: this is the
# gate on CONFIRMED_EMPTY, and a loose pattern here turns a broken parser into
# "the source has nothing", which is the exact failure being designed out.
_EMPTY_PHRASES = re.compile(
    r"\b("
    r"no\s+(current|open|active|available|matching)\s+"
    r"(opportunit\w+|tenders?|notices?|calls?|grants?|results?|vacanc\w+)|"
    r"there\s+are\s+(currently\s+)?no\s+\w+|"
    r"no\s+results?\s+(were\s+)?found|"
    r"0\s+(opportunit\w+|tenders?|notices?|results?)\s+found"
    r")\b",
    re.IGNORECASE,
)

# A challenge/bot wall. Same list base_scraper and grantwatch already use.
_CHALLENGE_MARKERS = (
    "just a moment", "attention required", "checking your browser",
    "cf-chl", "challenge-platform", "turnstile", "cf_chl_opt",
    "sorry, you have been blocked", "enable javascript and cookies",
)


@dataclass
class Evidence:
    """Everything a run observed. The classifier reads only this.

    Nothing here is optional-because-convenient: each field exists because
    without it some outcome would have to be guessed. `pages_fetched == 0` and
    `extracted == 0` look identical in the old schema, and they are the
    difference between "the site refused us" and "our parser is broken".
    """

    pages_fetched: int = 0
    extracted: int = 0                 # candidate rows the parser produced
    saved: int = 0                     # new rows written
    duplicates: int = 0                # already stored
    rejected: int = 0                  # failed the opportunity gate
    expired: int = 0                   # past deadline at ingest

    first_http_status: int | None = None
    last_http_status: int | None = None
    final_url: str = ""
    page_title: str = ""
    response_bytes: int = 0
    body_sample: str = ""              # first few KB, lowercased by the caller
    attempts: int = 0
    fetch_mode: str = ""               # "http" | "browser"

    # Positive proof of emptiness. NEVER inferred — set only when the source
    # itself said so. e.g. "API total=0 with status filter applied".
    empty_proof: str = ""
    # Every notice we did fetch carried a closed/awarded/cancelled status.
    all_notices_closed: bool = False

    # Parser-drift detection: a hash of the shape the parser relied on.
    structure_signature: str = ""
    last_good_signature: str = ""
    expected_container_present: bool | None = None

    cancelled: bool = False
    timed_out: bool = False
    exception: str = ""
    auth_required: bool = False
    session_expired: bool = False

    notes: list[str] = field(default_factory=list)


def _challenge_seen(ev: Evidence) -> bool:
    blob = f"{ev.page_title} {ev.body_sample}".lower()
    return any(m in blob for m in _CHALLENGE_MARKERS)


def classify(ev: Evidence) -> tuple[Outcome, ErrorCode | None, str]:
    """(outcome, error_code, human message). Pure — no I/O, no globals.

    Order matters and is deliberate. Cancellation and crashes describe how the
    run ENDED and outrank anything about content. Transport comes next, because
    "we never got a page" makes every content question moot. Only then is the
    parser asked anything.
    """
    # ---- how the run ended -------------------------------------------------
    if ev.cancelled:
        return Outcome.CANCELLED, None, "Stopped by a user."
    if ev.timed_out:
        return (Outcome.TIMED_OUT, ErrorCode.TIMEOUT,
                f"Timed out after {ev.pages_fetched} page(s).")
    if ev.exception:
        return Outcome.CRASHED, ErrorCode.UNKNOWN, ev.exception[:500]

    # ---- did we ever read a page? -----------------------------------------
    if ev.pages_fetched <= 0:
        if ev.session_expired:
            return (Outcome.SESSION_EXPIRED, ErrorCode.LOGIN_WALL,
                    "The saved session is no longer signed in.")
        if ev.auth_required:
            return (Outcome.AUTH_REQUIRED, ErrorCode.LOGIN_WALL,
                    "This source requires a login that is not configured.")
        if _challenge_seen(ev):
            return (Outcome.BLOCKED, ErrorCode.CHALLENGE,
                    f"Bot check, not a listing (title {ev.page_title!r}).")
        status = ev.last_http_status
        if status == 403:
            return (Outcome.BLOCKED, ErrorCode.HTTP_403,
                    f"HTTP 403 from {ev.final_url or 'the listing URL'}.")
        if status == 429:
            return (Outcome.BLOCKED, ErrorCode.HTTP_429, "HTTP 429 — rate limited.")
        if status is not None and 500 <= status < 600:
            return (Outcome.NO_FETCH, ErrorCode.HTTP_5XX, f"HTTP {status} from the source.")
        if status is not None and 400 <= status < 500:
            return (Outcome.NO_FETCH, ErrorCode.HTTP_4XX, f"HTTP {status} from the source.")
        return (Outcome.NO_FETCH, ErrorCode.UNKNOWN,
                f"No listing page was retrieved after {ev.attempts or 1} attempt(s).")

    # ---- we read a page. Did the parser find anything? --------------------
    if ev.extracted <= 0:
        # Drift beats a bare parse failure, but only on positive evidence: a
        # signature that actually differs from the last known-good one, or a
        # container the parser requires that is no longer in the document.
        drifted = bool(
            ev.structure_signature and ev.last_good_signature
            and ev.structure_signature != ev.last_good_signature
        ) or ev.expected_container_present is False
        if drifted:
            return (Outcome.STRUCTURE_CHANGED, None,
                    "The page loaded but its structure no longer matches the parser "
                    f"(signature {ev.last_good_signature or '?'} -> "
                    f"{ev.structure_signature or '?'}).")

        # CONFIRMED_EMPTY — the strict gate. Positive proof only.
        if ev.empty_proof:
            return (Outcome.CONFIRMED_EMPTY, None,
                    f"No open calls right now — {ev.empty_proof}")
        if ev.all_notices_closed:
            return (Outcome.CONFIRMED_EMPTY, None,
                    "Every notice fetched is closed, awarded or cancelled.")
        if _EMPTY_PHRASES.search(ev.body_sample or ""):
            return (Outcome.CONFIRMED_EMPTY, None,
                    "The listing page states it currently has none.")

        # Otherwise we do not know, and we say so rather than promoting a guess.
        return (Outcome.PARSE_ZERO, None,
                f"{ev.pages_fetched} page(s) fetched ({ev.response_bytes:,} bytes) but the "
                "parser produced no candidates. This is NOT evidence the source is empty.")

    # ---- the parser produced rows -----------------------------------------
    if ev.saved > 0:
        return (Outcome.SUCCESS_WITH_RESULTS, None,
                f"{ev.saved} new of {ev.extracted} extracted.")
    return (Outcome.SUCCESS_NO_NEW, None,
            f"{ev.extracted} extracted, all already stored "
            f"({ev.duplicates} duplicate, {ev.rejected} rejected, {ev.expired} expired).")


def reconcile_stale(status: str, has_finished_at: bool) -> tuple[Outcome, str]:
    """Terminal state for a run left in `running` by a process that is gone.

    The baseline found 106 of these, and they are not one population:

      * 30 have a finished_at. `_close_run` stamps finished_at and then copies
        prog["status"], which is only set to "completed" AFTER the crawl loop —
        so these are sources that raised inside the loop. The record is
        complete; only the status was never updated. They were crashes.

      * 76 have none. `_close_run` never ran at all, so the process died or was
        killed mid-source.

    Marking all 106 the same way would be exactly the guess this module exists
    to prevent, so they get different states.
    """
    if status != "running":
        return Outcome(status) if status in Outcome._value2member_map_ else Outcome.CRASHED, ""
    if has_finished_at:
        return (Outcome.CRASHED,
                "Recovered at startup: the run recorded a finish time but never a "
                "terminal status, so the source raised inside the crawl loop.")
    return (Outcome.STALE_RUN_RECOVERED,
            "Recovered at startup: no finish time and no heartbeat, so the worker "
            "process disappeared before it could close this run.")
