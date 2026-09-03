"""ScraperManager — orchestrates concurrent scrape jobs with pause/resume/stop,
live logs, progress %, ETA, and the normalize→filter→classify→dedupe→save pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update

from app.core.config import settings
from app.database.db import session_scope
from app.database.models import Opportunity, ScrapeRun, Status
from app.services import run_lock
from app.services.scrape_outcome import Evidence, Outcome, classify
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import get_scrapers
from app.services.classification import Classifier, KeywordClassifier
from app.services.deadline_parser import DeadlineParser
from app.services.deduplication import make_unique_id
from app.services.amounts import clean_amount, extract_amount
from app.services.geography import normalize_geo
from app.services.links import canonical_link, is_usable_link
from app.services.actionable import (
    DeadlineState,
    classify_deadline,
)
from app.services.opportunity_gate import is_opportunity
from app.services.source_manifest import (
    contract_for,
    disabled_sources,
    record_is_in_scope,
)
from app.services.deadline_audit import is_sentinel
from app.services.spam import is_spam
from app.services.organization import extract_organization, tidy_organization
from app.services.verticals import VERTICALS as ALL_VERTICALS
from app.services.verticals import classify_verticals, verticals_to_str
from app.services.study_type import classify_study_type
from app.services.work_type import classify_work_type

log = logging.getLogger("scraper")


class ScraperManager:
    """Singleton coordinating one scrape job at a time (parallel across sources).

    Dependency-injected engines make each stage swappable (e.g. an ML classifier)
    without touching orchestration code.
    """

    def __init__(
        self,
        classifier: Classifier | None = None,
        deadline_parser: DeadlineParser | None = None,
    ) -> None:
        self.classifier = classifier or KeywordClassifier()
        self.deadline_parser = deadline_parser or DeadlineParser()

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._pause = asyncio.Event()
        self._pause.set()  # set == running, cleared == paused
        self._lock = threading.Lock()

        # idle | running | paused | stopping | finalizing
        #
        # "finalizing" is new and it is not cosmetic. _run() used to hold
        # state == "running" through _maintenance(), which is a full-database
        # deadline audit, link repair and junk purge. On a 177 MB database that
        # is minutes of work AFTER every source has finished, during which the
        # dashboard said "scraping" and the scheduler's completion poll
        # (`while manager.state != "idle"`) kept waiting. Two different things
        # were sharing one word.
        self.state: str = "idle"
        self._lease: str | None = None       # worker_id while we hold the lease
        self.vertical_filter: set[str] = set()  # vertical-aware scraping (empty = keep all)
        self.on_complete = None   # optional async callback (set at app startup)
        self.logs: deque[str] = deque(maxlen=500)
        self.progress: dict[str, dict[str, Any]] = {}
        self.started_at: float | None = None

    # ------------------------------------------------------------- lifecycle
    async def start(
        self, sources: list[str] | None = None, verticals: list[str] | None = None
    ) -> None:
        if self._task and not self._task.done():
            raise RuntimeError("A scrape job is already running")

        # The in-process guard above only sees THIS process. Manual starts and
        # scheduled starts both come through here, so this is the one place a
        # cross-process lease has to be taken — see services/run_lock.py for
        # why max_instances=1 is not sufficient.
        scrapers = get_scrapers(sources or None)
        # A source marked production_enabled=False must not run in a scheduled
        # all-source scrape.
        #
        # That field, and disabled_sources() beside it, were written in the
        # previous round and then read by NOTHING — a grep across the whole
        # backend found no caller outside the module that defines them. So
        # Devex, whose manifest says in as many words "Disabled until the access
        # question is answered", ran on every scheduled scrape exactly as
        # before, failed to fetch a single page exactly as before, and recorded
        # 'completed' exactly as before. A disable switch nobody reads is worse
        # than none: it makes people believe the source is off.
        #
        # Naming a source explicitly still runs it. An operator testing a fix
        # for the very defect that disabled it must not be blocked by the flag,
        # and `sources` is only ever non-empty because a person typed it.
        if not sources:
            blocked = disabled_sources(s.name for s in scrapers)
            if blocked:
                scrapers = [s for s in scrapers if s.name not in blocked]
                for key, why in sorted(blocked.items()):
                    self._log(f"Skipping {key} — held out of production: "
                              f"{why.splitlines()[0][:160]}")
        if not scrapers:
            raise RuntimeError(
                "Every requested source is held out of production. Name one "
                "explicitly to override, or clear production_enabled in "
                "services/source_manifest.py.")
        try:
            self._lease = run_lock.acquire(
                label=f"{len(scrapers)} source(s)"
            )
        except run_lock.LeaseNotAcquired as exc:
            self._log(f"Scrape refused — {exc}")
            raise RuntimeError(str(exc)) from exc
        # Vertical-aware scraping: when set, only opportunities classified into a
        # selected vertical are persisted (everything else is filtered in-pipeline,
        # reducing unnecessary storage and downstream noise).
        self.vertical_filter: set[str] = {s for s in (verticals or []) if s in ALL_VERTICALS}
        self._stop.clear()
        self._pause.set()
        self.state = "running"
        self.logs.clear()
        self.started_at = time.monotonic()
        self.progress = {
            s.name: {"display_name": s.display_name, "pages": 0, "found": 0, "saved": 0,
                     "skipped_expired": 0, "duplicates": 0, "off_vertical": 0, "spam": 0,
                     "errors": 0, "status": "queued"}
            for s in scrapers
        }
        self._log(f"Starting scrape: {', '.join(s.display_name for s in scrapers)}")
        if self.vertical_filter:
            self._log(f"Vertical-aware mode: keeping only {', '.join(sorted(self.vertical_filter))}")
        self._task = asyncio.create_task(self._run(scrapers))

    def pause(self) -> None:
        if self.state == "running":
            self._pause.clear()
            self.state = "paused"
            self._log("Scraping paused")

    def resume(self) -> None:
        if self.state == "paused":
            self._pause.set()
            self.state = "running"
            self._log("Scraping resumed")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self.state = "stopping"
            self._log("Stop requested — finishing current pages…")
            self._stop.set()
            self._pause.set()  # unblock paused crawlers so they can exit

    # ------------------------------------------------------------- execution
    async def _run(self, scrapers: list[BaseScraper]) -> None:
        # Cap how many sources run at once. With ~80 configured sources, starting
        # them all together opened hundreds of simultaneous requests and browser
        # threads, which starved the event loop — /progress stopped responding and
        # the dashboard sat on "idle" while the scrape was plainly running.
        gate = asyncio.Semaphore(max(1, settings.max_concurrent_sources))

        async def _guarded(scraper: BaseScraper) -> None:
            async with gate:
                if self._stop.is_set():
                    self.progress[scraper.name]["status"] = "stopped"
                    return
                # A source gets a bounded slot, not an open-ended one.
                #
                # The baseline found 106 runs stuck in "running" — a source
                # that hangs holds its semaphore slot forever, and with
                # max_concurrent_sources slots that is how an entire night's
                # scrape stops after the first few sources wedge. The timeout
                # is what turns "never finishes" into "finishes, badly, and
                # says so".
                #
                # asyncio.wait_for cancels the awaiting coroutine at the next
                # await point. Playwright work runs in a worker thread and
                # `asyncio.to_thread` is NOT cancellable, so the thread may
                # still be finishing its current page when this returns. That
                # is why the stop flag is set as well: the crawl loop checks it
                # between pages and exits on its own, and the timeout only
                # stops US waiting.
                timeout = max(60, settings.source_timeout_s)
                try:
                    await asyncio.wait_for(self._run_source(scraper), timeout)
                except asyncio.TimeoutError:
                    prog = self.progress.get(scraper.name, {})
                    prog["status"] = "failed"
                    prog["timed_out"] = True
                    self._log(
                        f"[{scraper.display_name}] stopped after "
                        f"{timeout}s — the per-source time limit. Its worker "
                        f"may still be finishing the page it was on; the run "
                        f"is recorded as timed out either way, which is the "
                        f"honest answer and is not the same as 'completed'."
                    )

        heart = asyncio.create_task(self._heartbeat_loop())
        try:
            # And a ceiling on the whole run. Per-source timeouts alone do not
            # bound it: 85 sources times the per-source limit is days. This is
            # the one that guarantees a nightly scrape has ended before the
            # next one is due — the scheduler's max_instances=1 would otherwise
            # skip every subsequent run while the first is still going.
            whole_run = max(300, settings.run_timeout_s)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(_guarded(s) for s in scrapers)), whole_run)
            except asyncio.TimeoutError:
                self._stop.set()          # let the crawl loops unwind
                self._log(
                    f"Scrape stopped after {whole_run}s — the whole-run time "
                    f"limit. Sources that had not started are left untouched "
                    f"and will run next time; raise LOP_RUN_TIMEOUT_S if the "
                    f"full set legitimately needs longer."
                )
        finally:
            heart.cancel()
            if settings.run_maintenance_after_scrape:
                # Distinct from "running": every source is done and no page is
                # being fetched. What remains is whole-database maintenance, and
                # calling that "scraping" is what made the dashboard sit on
                # "running" for minutes after the work finished.
                self.state = "finalizing"
                self._log("All sources finished — running post-scrape maintenance")
                try:
                    await asyncio.to_thread(self._maintenance)
                except Exception:
                    log.exception("post-scrape maintenance failed")
            if self._lease:
                run_lock.release(self._lease)
                self._lease = None
            self.state = "idle"
            self._log("Scrape job completed")
            if self.on_complete is not None and not self._stop.is_set():
                try:
                    await self.on_complete()
                except Exception:
                    log.exception("post-scrape hook failed")

    async def _heartbeat_loop(self) -> None:
        """Keep the lease alive, and stop if we ever lose it.

        Losing the lease means something concluded this run was dead and gave it
        to another process. Carrying on would produce exactly the concurrent
        scrape the lease exists to prevent, so this stops the run rather than
        logging and hoping.
        """
        while True:
            await asyncio.sleep(run_lock.HEARTBEAT_S)
            if not self._lease:
                return
            try:
                alive = await asyncio.to_thread(run_lock.heartbeat, self._lease)
            except Exception:                                   # noqa: BLE001
                log.exception("[run-lock] heartbeat failed")
                continue        # a transient DB error is not proof we lost it
            if not alive:
                self._log(
                    "Lost the scrape lease — another process has taken it over. "
                    "Stopping this run to avoid two scrapes on one database."
                )
                self._lease = None
                self._stop.set()
                self._pause.set()
                return

    def _maintenance(self) -> None:
        """Repair passes that run after every scrape.

        audit_deadlines(), repair_links() and junk_rows() were all written,
        tested and documented — and never called by anything in the application.
        Three of the dashboard's visible faults were simply these three passes
        never running: closed calls staying live, links opening the wrong page,
        and rows titled "Skip to main content" sitting in the list. Wiring them
        in here is the fix; the functions themselves were already correct.
        """
        from app.services.deadline_audit import audit_deadlines
        from app.services.links import purge_junk_rows, repair_links

        deadlines = audit_deadlines()
        if deadlines.get("stale_ongoing"):
            self._log(
                f"Maintenance: retired {deadlines['stale_ongoing']} undated listing(s) "
                f"no source has shown for {settings.ongoing_max_age_days} days"
            )
        if deadlines.get("expired"):
            self._log(f"Maintenance: marked {deadlines['expired']} passed-deadline row(s) expired")

        links = repair_links()
        if links.get("cleared") or links.get("rewritten"):
            self._log(
                f"Maintenance: cleared {links['cleared']} unusable link(s), "
                f"rewrote {links['rewritten']}"
            )

        junk = purge_junk_rows()
        if junk:
            self._log(f"Maintenance: removed {junk} row(s) that were page furniture, not opportunities")

    async def _run_source(self, scraper: BaseScraper) -> None:
        prog = self.progress[scraper.name]
        prog["status"] = "running"
        run_id = self._open_run(scraper.display_name, scraper.name)

        # Per-source stop: lets one source finish early (stale pages) without
        # stopping the others. Mirrors the global stop event.
        source_stop = asyncio.Event()

        async def _mirror_global_stop() -> None:
            await self._stop.wait()
            source_stop.set()

        mirror = asyncio.create_task(_mirror_global_stop())

        async def on_progress(event: str, payload: dict) -> None:
            if event == "page_start":
                self._log(f"[{scraper.display_name}] scraping page {payload['page']}…")
            elif event == "page_done":
                prog["pages"] = payload["page"]
                self._log(f"[{scraper.display_name}] page {payload['page']}: found {payload['found']} listings")
            elif event == "pages_end":
                self._log(
                    f"[{scraper.display_name}] pagination exhausted at page {payload['page']} — "
                    f"every available page was scraped"
                )
            elif event == "page_error":
                prog["errors"] += 1
                self._log(f"[{scraper.display_name}] page {payload['page']} failed — skipped")

        crash = ""
        stale_streak = 0
        # None = use the global default; 0 = never stop early (walk to the end
        # of pagination). `or` would have treated 0 as "unset", so test for None.
        override = scraper.stale_page_streak_override
        stale_limit = settings.stale_page_streak if override is None else override
        if stale_limit == 0:
            self._log(
                f"[{scraper.display_name}] full-archive mode — walking every page "
                f"(already-seen pages don't stop the crawl; this takes a while)"
            )
        try:
            async for batch in scraper.crawl(source_stop, self._pause, on_progress):
                prog["found"] += len(batch)
                saved, expired, dupes, spam, rejected = await asyncio.to_thread(
                    self._ingest, batch, scraper.curated, scraper.name)
                prog["saved"] += saved
                prog["skipped_expired"] += expired
                prog["duplicates"] += dupes
                prog["spam"] += spam
                prog["rejected"] = prog.get("rejected", 0) + rejected
                # "found 30, saved 0" is unreadable without this. Every dropped
                # row now has a stated reason, so a run that looks like it did
                # nothing can be told apart from one that found only repeats.
                if not saved and batch:
                    self._log(
                        f"[{scraper.display_name}] page yielded {len(batch)}, saved 0 "
                        f"(duplicates {dupes}, expired {expired}, spam {spam}, "
                        f"not an opportunity {rejected}, "
                        f"off-vertical {prog.get('off_vertical', 0)})"
                    )
                if rejected:
                    self._log(
                        f"[{scraper.display_name}] rejected {rejected} row(s) that are "
                        f"not opportunities or have no link to one"
                    )
                if expired:
                    verb = "archived" if settings.keep_expired else "skipped"
                    self._log(f"[{scraper.display_name}] {verb} {expired} closed listing(s)")
                if saved:
                    self._log(f"[{scraper.display_name}] saved {saved} record(s)")

                # Listings are newest-first: N pages in a row with nothing new
                # means everything deeper is older (expired or already saved).
                stale_streak = stale_streak + 1 if (saved == 0 and batch) else 0
                if stale_limit and stale_streak >= stale_limit:
                    self._log(
                        f"[{scraper.display_name}] {stale_streak} consecutive pages with "
                        f"nothing new — stopping this source (only older content ahead)"
                    )
                    source_stop.set()
            prog["status"] = "stopped" if self._stop.is_set() else "completed"
        except Exception as exc:                                # noqa: BLE001
            log.exception("[%s] source crashed", scraper.name)
            prog["status"] = "failed"
            prog["errors"] += 1
            crash = f"{type(exc).__name__}: {exc}"
        finally:
            # THE ONE LINE THAT STOPS AN ORPHANED WORKER THREAD.
            #
            # Four scrapers do their fetching in a daemon thread that feeds an
            # unbounded Queue, and the only way to ask that thread to stop is
            # this Event — every one of them checks `stop_event.is_set()`
            # between pages.
            #
            # `_guarded` wraps this coroutine in
            # `asyncio.wait_for(..., source_timeout_s)`. When that fires the
            # coroutine is cancelled and this `finally` runs, but until now it
            # only cancelled the mirror task. The Event was never set, so the
            # thread never learned the run was over: it carried on fetching and
            # parsing, and carried on `queue.put()`-ing into a queue nobody was
            # draining any more.
            #
            # That is one thread's worth of CPU and an unbounded queue of page
            # payloads, per timed-out source, held until the process restarts.
            # It is the reason a worker that starts at ~150 MB reaches 1.6 GB
            # while `ps` shows no browser — the JSON sources need no Chromium.
            #
            # Setting it here is safe on every path. On normal completion the
            # crawl has already finished and this is a no-op; on timeout or
            # crash it is what lets the thread reach its own `finally`, close
            # its browser or HTTP client, and exit.
            source_stop.set()
            mirror.cancel()
            # What the run actually observed. Scrapers that record transport
            # detail expose it as `last_probe`; the ones that do not yet leave
            # those fields None, and the classifier degrades to the page/extract
            # counts rather than inventing a status code.
            probe = getattr(scraper, "last_probe", None) or {}
            evidence = Evidence(
                pages_fetched=prog["pages"],
                extracted=prog["found"],
                saved=prog["saved"],
                duplicates=prog.get("duplicates", 0),
                rejected=prog.get("rejected", 0),
                expired=prog["skipped_expired"],
                first_http_status=probe.get("first_http_status"),
                last_http_status=probe.get("last_http_status"),
                final_url=probe.get("final_url", ""),
                page_title=probe.get("page_title", ""),
                response_bytes=probe.get("response_bytes", 0),
                body_sample=probe.get("body_sample", ""),
                attempts=probe.get("attempts", 0),
                fetch_mode=probe.get("fetch_mode",
                                     "browser" if scraper.requires_js else "http"),
                empty_proof=probe.get("empty_proof", ""),
                all_notices_closed=probe.get("all_notices_closed", False),
                structure_signature=probe.get("structure_signature", ""),
                last_good_signature=self._last_good_signature(scraper),
                expected_container_present=probe.get("expected_container_present"),
                cancelled=self._stop.is_set() or source_stop.is_set() and not prog["found"],
                exception=crash,
            )
            self._close_run(run_id, prog, evidence)
            # When closed listings are archived they are *included* in `saved`,
            # so reporting them as a separate addend made the totals look wrong
            # ("3536 found = 3024 saved + 3277 expired + 512 dupes").
            closed_note = (
                f" (of which {prog['skipped_expired']} already closed)"
                if settings.keep_expired else
                f" + {prog['skipped_expired']} expired skipped"
            )
            self._log(
                f"[{scraper.display_name}] {prog['status']} — {prog['found']} found = "
                f"{prog['saved']} new saved{closed_note} + "
                f"{prog['duplicates']} already in database"
            )

    # -------------------------------------------------------------- pipeline
    def _ingest(self, batch: list[RawOpportunity],
                curated: bool = False,
                source_key: str = "") -> tuple[int, int, int, int, int]:
        """One page of scraped rows -> rows in the database.

        normalise deadline -> mark expired -> classify (category, verticals,
        work type, study type) -> optional vertical filter -> normalise
        geography/organisation/amount -> reject spam -> fingerprint -> dedupe
        -> insert.

        Returns (saved, expired, duplicates, spam, rejected) so the caller can
        say *why* a page produced no new rows. "found 30, saved 0" is unreadable
        without those numbers — repeats, closed calls and junk are different
        situations and only some of them are a problem.

        `curated` comes from the scraper (BaseScraper.curated) and says whether
        this source's page contains opportunities and nothing else. It relaxes
        the vocabulary half of the opportunity gate, never the furniture or
        page-type half.

        `source_key` is the registry name. It selects this source's contract,
        which is what lets a record be judged on the source's OWN fields
        (record_type, source_status) before any of its prose is read.
        """
        saved = expired = dupes = spam = rejected = 0
        undated = rolling_rows = unassessed = 0
        # Rows that were already stored WITHOUT a deadline and gained one on
        # this pass. Counted and logged because it is otherwise invisible: the
        # run reports "0 new saved" either way, and a fix that repairs 1,274
        # rows would look exactly like a fix that repaired none.
        dated_late = 0
        out_of_scope = 0
        expired_samples: list[str] = []
        today = date.today()
        batch_uids: set[str] = set()  # catch duplicates within the same batch too
        contract = contract_for(source_key or "",
                                batch[0].source_website if batch else "")
        with session_scope() as db:
            for raw in batch:
                deadline = self.deadline_parser.parse(raw.deadline_raw, dayfirst=raw.dayfirst)
                # 9999-12-31 is DevelopmentAid's "no closing date". Parsed
                # literally it produced deadlines in the year 9999 and a
                # countdown of 2.9 million days. Treat it as no deadline, which
                # is what the source meant, and let the ongoing logic below
                # handle it from there.
                if is_sentinel(deadline):
                    deadline = None
                # Three states, not two. Conflating the last two is what put
                # closed calls on the dashboard as permanent "Ongoing" rows.
                #
                #   dated    a date we could read  -> Active or Expired, decided
                #                                    by the date and nothing else
                #   rolling  the source SAYS there is no closing date  -> Active
                #   unknown  no date, and no such statement            -> Active
                #                                    for now, retired by
                #                                    audit_deadlines() once the
                #                                    source stops listing it
                #
                # "unknown" must not be treated as expired: that would hide every
                # row from a funder page that simply does not print dates. And it
                # must not be treated as rolling either: that is immortality, and
                # it is the bug being fixed. It is a lease, renewed every time a
                # scrape sees the row again (last_seen) and allowed to lapse
                # after LOP_ONGOING_MAX_AGE_DAYS.
                rolling = deadline is None and (
                    self.deadline_parser.is_ongoing(raw.deadline_raw) or raw.assume_active
                )
                # Undated rows are tallied at INSERT, below — see the note
                # there. Counting them at this point included rows the gates
                # went on to drop, so the log reported "kept live" about rows
                # that were never written.
                # Record the state as evidence, not as a guess. "the source
                # said rolling" and "we could not read a date" are different
                # claims and get different confidence markers, so a later audit
                # can tell which rows rest on the source's word and which rest
                # on our parser giving up.
                #
                # classify_deadline lives in services/actionable.py alongside
                # the rule that READS these values. Writing a second copy here
                # is how the two drift until a row is stored in a state the
                # rule does not expect.
                d_state, d_confidence = classify_deadline(
                    raw.deadline_raw, deadline, rolling)
                is_expired = deadline is not None and deadline < today
                if is_expired:
                    expired += 1
                    if len(expired_samples) < 3:
                        expired_samples.append(
                            f"{raw.title[:48]!r} (deadline: {deadline or raw.deadline_raw or 'none'})"
                        )
                    # Keep them unless configured otherwise. Dropping closed calls
                    # threw away thousands of records per run and made "found" vastly
                    # exceed "saved"; they're stored with status=Expired instead, so
                    # the dashboard's Active view is unchanged but nothing is lost.
                    if not settings.keep_expired:
                        continue

                category = self.classifier.classify(raw.title, raw.summary, raw.category_hint)
                vertical_body = " ".join(filter(None, [raw.summary, raw.vertical, raw.eligibility]))
                vertical_tags = classify_verticals(raw.title, vertical_body)
                if self.vertical_filter and not (set(vertical_tags) & self.vertical_filter):
                    self._count_off_vertical(raw.source_website)
                    continue
                clean_country, clean_region = normalize_geo(raw.country, raw.region)
                # Sources that publish a funder field win; for the ones that
                # don't (FundsForNGOs names it only in prose) recover it from
                # the text rather than leaving the column blank.
                organization = tidy_organization(raw.organization)
                if not organization:
                    organization = extract_organization(raw.summary, raw.title)
                # Same pattern for the amount: use the source's figure when it
                # gave one (stripped of page furniture), else read it out of the
                # listing text, which is where most sources state it.
                amount = clean_amount(raw.funding_amount)
                if not amount:
                    amount = extract_amount(raw.summary, raw.title)
                # Public tender boards accept submissions, and some of what is
                # submitted is advertising. Dropped here rather than filtered in
                # the UI, so it never reaches the database, the digests or the
                # counts.
                if is_spam(raw.title, raw.summary):
                    spam += 1
                    log.debug("[%s] spam listing skipped: %s",
                              raw.source_website, (raw.title or "")[:60])
                    continue

                # A link that opens the call itself is not a nice-to-have, it is
                # the product. A row without one used to be stored anyway with an
                # empty opportunity_url, and services/links.resolve_link then
                # handed the reader a web search — so the dashboard was full of
                # entries that opened a search page instead of an opportunity.
                # There is nothing useful to keep in such a row: the title alone
                # is not a lead.
                if settings.require_usable_link and not is_usable_link(
                        raw.opportunity_url, raw.website):
                    rejected += 1
                    log.debug("[%s] no usable link, row dropped: %s",
                              raw.source_website, (raw.title or "")[:60])
                    continue

                # What does the SOURCE say this record is?
                #
                # This runs before the prose gate below because it is the check
                # that gate structurally cannot make. World Bank's feed is
                # mostly contract awards, and an award reads exactly like an
                # open tender — "Award of Contract for Supervision Services" is
                # a real notice title on both. No amount of title reading
                # separates them; the record's own notice_type does, instantly.
                #
                # It is a no-op unless the scraper supplied record_type or
                # source_status, and an unrecognised status is UNKNOWN rather
                # than closed. A source that says nothing must not have silence
                # read as a reason to discard its rows. See
                # services/source_manifest.py.
                keep_scope, scope_why = record_is_in_scope(
                    contract, raw.record_type, raw.source_status)
                if not keep_scope:
                    out_of_scope += 1
                    rejected += 1
                    log.debug("[%s] out of scope (%s): %s", raw.source_website,
                              scope_why, (raw.title or "")[:60])
                    continue

                # Is this actually an opportunity? Most sources are scraped by
                # harvesting every link on a page, so without this a funder's
                # news post, programme page or "our grantees" card is stored as
                # a fundable call. See services/opportunity_gate.py — curated
                # boards skip the vocabulary test but not the rest.
                if settings.strict_opportunity_gate:
                    keep, why = is_opportunity(
                        raw.title, raw.summary, raw.opportunity_url,
                        str(getattr(category, "value", category) or ""), curated)
                    if not keep:
                        rejected += 1
                        log.debug("[%s] rejected (%s): %s", raw.source_website,
                                  why, (raw.title or "")[:60])
                        continue

                uid = make_unique_id(raw.title, raw.organization, deadline, raw.opportunity_url)

                existing = db.execute(
                    select(Opportunity.id, Opportunity.deadline)
                    .where(Opportunity.unique_id == uid)
                ).one_or_none()
                exists = existing[0] if existing else None
                if uid in batch_uids or exists is not None:
                    dupes += 1
                    if exists is not None:
                        # A duplicate is not nothing: it is proof the source is
                        # still publishing this call today. Recording that is
                        # what lets an undated "Ongoing" row be retired later,
                        # once the source stops returning it. Before this, a
                        # duplicate was simply dropped and no row ever aged.
                        fields = {"last_seen": datetime.now(timezone.utc)}

                        # A newly-readable deadline is the same kind of fact,
                        # and without this it was thrown away.
                        #
                        # The deadline is deliberately not part of unique_id —
                        # it is an attribute, not identity — so a row whose date
                        # we could not read before and can read now arrives here
                        # as a duplicate, gets its last_seen bumped, and loses
                        # the date. UNDP Procurement made that concrete: fixing
                        # extraction took its rows from 0.4% dated to 100%, and
                        # not one of the 1,274 already stored would have gained
                        # a deadline, because every one of them comes back as a
                        # duplicate. The dashboard would have looked identical
                        # after the fix.
                        #
                        # ONLY fills a gap. It never overwrites a date already
                        # stored, because the reverse — a source that stops
                        # printing a date, or a page that renders it late —
                        # would silently erase a good deadline, and a row whose
                        # deadline disappears becomes immortal. Correcting an
                        # existing date is a different decision with a different
                        # risk, and it is not this one.
                        if existing[1] is None and deadline is not None:
                            fields.update(
                                deadline=deadline,
                                deadline_state=d_state.value,
                                deadline_raw=(raw.deadline_raw or "")[:256],
                                deadline_confidence=d_confidence,
                                deadline_convention=("dayfirst" if raw.dayfirst
                                                     else "monthfirst"),
                                deadline_checked_at=datetime.now(timezone.utc),
                            )
                            dated_late += 1
                        db.execute(
                            update(Opportunity)
                            .where(Opportunity.id == exists)
                            .values(**fields)
                        )
                    continue
                batch_uids.add(uid)

                db.add(Opportunity(
                    unique_id=uid,
                    title=raw.title.strip(),
                    organization=organization,
                    # Keeps the two columns cleanly separated: region names and
                    # title artifacts never land in country, aliases collapse,
                    # and region is inferred from a known country.
                    country=clean_country,
                    region=clean_region,
                    funding_type=raw.funding_type,
                    vertical=raw.vertical,
                    verticals=verticals_to_str(vertical_tags),
                    # Routing axis: research assignments and delivery work go to
                    # different teams even when both are filed as "RFP".
                    work_type=classify_work_type(raw.title, vertical_body),
                    study_type=classify_study_type(raw.title, vertical_body),
                    category=category,
                    deadline=deadline,
                    # Phase 4 added these columns and backfilled the existing
                    # rows, but nothing wrote them on INSERT — so every row
                    # scraped since then stored deadline_state NULL. For a row
                    # WITH a date that is harmless (the rule infers DATED from
                    # the date). For an undated row it is not: NULL state plus
                    # NULL deadline reads as UNKNOWN, which is not actionable,
                    # and the row vanishes from every dashboard view. New
                    # rolling rows were being hidden by the fix meant to stop
                    # closed rows being shown.
                    deadline_state=d_state.value,
                    # The source's own words, kept verbatim. This is what makes
                    # a confirmed day/month inversion re-parseable instead of
                    # re-scrapeable — see scripts/deadline_convention_audit.py.
                    deadline_raw=(raw.deadline_raw or "")[:256],
                    deadline_confidence=d_confidence,
                    # Which convention the parser was told to use. Without it,
                    # a later correction cannot tell which rows were affected.
                    deadline_convention="dayfirst" if raw.dayfirst else "monthfirst",
                    deadline_checked_at=datetime.now(timezone.utc),
                    website=raw.website,
                    # Store a link only if it actually points at this call. A
                    # bare slug, a mailto:, or a bare domain sends the reader to
                    # a homepage and costs more trust than an absent link does.
                    opportunity_url=(
                        canonical_link(raw.opportunity_url)
                        if is_usable_link(raw.opportunity_url, raw.website) else ""
                    ),
                    summary=raw.summary,
                    location=raw.location,
                    eligibility=raw.eligibility,
                    funding_amount=amount,
                    status=Status.EXPIRED if is_expired else Status.ACTIVE,
                    source_website=raw.source_website,
                ))
                saved += 1
                # Counted HERE, not where the state is computed. A row rejected
                # by the scope check or the opportunity gate never reaches the
                # database, and reporting it as "stored as unassessed" sends
                # someone looking for rows that were never written. The same
                # applies to the undated tallies: every one of these describes
                # what is now IN the database.
                if deadline is None:
                    undated += 1
                    rolling_rows += 1 if rolling else 0
                if d_state is DeadlineState.UNKNOWN:
                    unassessed += 1
        if expired_samples:
            self._log(f"  ↳ expired examples: {'; '.join(expired_samples)}")
        if undated:
            # Visible on purpose. "Rolling" is a claim the source made; "no date
            # found" is our parser failing. Reporting one number for both is how
            # the permanent-Ongoing bug stayed invisible for so long — the fix is
            # only trustworthy if the split can be watched.
            self._log(
                f"  ↳ {undated} undated row(s) stored: {rolling_rows} where the "
                f"source states no closing date (shown as live, retired after "
                f"{settings.ongoing_max_age_days} days unseen), "
                f"{undated - rolling_rows} where we could not read one"
            )
        if unassessed:
            # Said out loud because these rows are stored ACTIVE but are NOT
            # actionable, so they appear in no dashboard view until the review
            # queue exists. A number in the run log is the only thing standing
            # between "held for review" and "silently lost".
            self._log(
                f"  ↳ {unassessed} row(s) stored as UNASSESSED — a closing date "
                f"could not be determined. They are held for review, not shown "
                f"as live and not archived."
            )
        if dated_late:
            # The only place this is visible. A run that repairs rows reports
            # "0 new saved" exactly like a run that repairs none, so without
            # this line a fix that gave 1,274 stored notices their closing
            # dates would leave no trace that anything had happened.
            self._log(
                f"  ↳ {dated_late} row(s) already stored WITHOUT a deadline "
                f"gained one from this scrape — they were held out of every "
                f"dashboard view until now"
            )
        if out_of_scope:
            # Counted separately from the prose gate. "the source says this is
            # a contract award" and "this does not read like an opportunity"
            # are different rejections, and merging them would hide a scraper
            # whose source vocabulary has changed.
            self._log(
                f"  ↳ {out_of_scope} row(s) rejected by the source's own type "
                f"or status fields, before any title was read"
            )
        return saved, expired, dupes, spam, rejected

    # ------------------------------------------------------------- reporting
    def snapshot(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        pages = sum(p["pages"] for p in self.progress.values())
        done = [p for p in self.progress.values() if p["status"] in {"completed", "failed", "stopped"}]
        pct = round(100 * len(done) / len(self.progress), 1) if self.progress else 0.0
        rate = pages / elapsed if elapsed > 0 and pages else 0
        eta = round((pages / rate) * (100 - pct) / max(pct, 1)) if rate and pct else None
        return {
            "state": self.state,
            "progress_percent": pct if self.state != "idle" or done else 0,
            "elapsed_seconds": round(elapsed),
            "eta_seconds": eta,
            "sources": self.progress,
            "logs": list(self.logs)[-100:],
        }

    def _count_off_vertical(self, source_website: str) -> None:
        """Attribute a vertical-filtered (dropped) listing to its source's counters."""
        for prog in self.progress.values():
            if prog["display_name"] == source_website:
                prog["off_vertical"] = prog.get("off_vertical", 0) + 1
                return

    def _log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.logs.append(f"{stamp}  {message}")
        log.info(message)

    @staticmethod
    def _last_good_signature(scraper) -> str:
        """The structure signature from this source's last run that produced rows.

        Without it, drift and "found nothing" are indistinguishable — which is
        why STRUCTURE_CHANGED requires a difference against a KNOWN GOOD run,
        not merely a signature that exists. Keyed on source_key so a rename does
        not lose the history.
        """
        try:
            with session_scope() as db:
                row = db.execute(
                    select(ScrapeRun.structure_signature)
                    .where(ScrapeRun.source_key == scraper.name,
                           ScrapeRun.saved > 0,
                           ScrapeRun.structure_signature.is_not(None))
                    .order_by(ScrapeRun.started_at.desc())
                    .limit(1)
                ).scalar()
                return row or ""
        except Exception:                                       # noqa: BLE001
            return ""

    @staticmethod
    def _open_run(source: str, source_key: str = "") -> int:
        with session_scope() as db:
            run = ScrapeRun(
                source_website=source,
                # The registry key, which survives a display-name change. The
                # baseline found 91 distinct display names for 85 sources
                # because renames split each source's history in two, which
                # breaks every "last successful run" and consecutive-failure
                # count keyed on the name.
                source_key=source_key or source,
                started_at=datetime.now(timezone.utc),
                worker_id=run_lock.worker_id(),
                heartbeat_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.flush()
            return run.id

    @staticmethod
    def _close_run(run_id: int, prog: dict[str, Any],
                   evidence: Evidence | None = None) -> None:
        """Write the terminal record, with a stated reason for whatever happened.

        The old version copied `prog["status"]` and nothing else. `prog["status"]`
        is set to "completed" after the crawl loop, so it meant "the function
        returned" — which is why all 916 runs in the baseline said `completed`
        or `running` and none ever said why a source produced nothing.
        """
        started = None
        with session_scope() as db:
            run = db.get(ScrapeRun, run_id)
            if not run:
                return
            finished = datetime.now(timezone.utc)
            started = run.started_at
            run.finished_at = finished
            run.pages_scraped = prog["pages"]
            run.found = prog["found"]
            run.saved = prog["saved"]
            run.skipped_expired = prog["skipped_expired"]
            run.errors = prog["errors"]
            run.duplicates = prog.get("duplicates", 0)
            run.rejected = prog.get("rejected", 0)
            run.status = prog["status"]
            if started is not None:
                st = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
                run.duration_s = (finished - st).total_seconds()

            if evidence is not None:
                outcome, code, message = classify(evidence)
                run.outcome = outcome.value
                run.error_code = code.value if code else None
                run.error_message = message
                run.first_http_status = evidence.first_http_status
                run.last_http_status = evidence.last_http_status
                run.final_url = (evidence.final_url or "")[:1024] or None
                run.fetch_mode = evidence.fetch_mode or None
                run.attempts = evidence.attempts
                run.structure_signature = evidence.structure_signature or None
                # Say it once, loudly, at the moment it happens — the brief's
                # "log a clear warning immediately when a source returns
                # nothing". Waiting for someone to open the dashboard is how a
                # source stays dead for 127 runs.
                if not outcome.is_healthy:
                    log.warning("[%s] %s — %s | next: %s",
                                run.source_website, outcome.value.upper(),
                                message, outcome.next_action)
                elif outcome is Outcome.CONFIRMED_EMPTY:
                    log.info("[%s] CONFIRMED_EMPTY — %s", run.source_website, message)


manager = ScraperManager()  # module-level singleton, injected into routes
