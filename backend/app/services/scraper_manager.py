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

from sqlalchemy import select

from app.core.config import settings
from app.database.db import session_scope
from app.database.models import Opportunity, ScrapeRun, Status
from app.schemas.opportunity import RawOpportunity
from app.scrapers.base_scraper import BaseScraper
from app.scrapers.registry import get_scrapers
from app.services.classification import Classifier, KeywordClassifier
from app.services.deadline_parser import DeadlineParser
from app.services.deduplication import make_unique_id
from app.services.verticals import VERTICALS as ALL_VERTICALS
from app.services.verticals import classify_verticals, verticals_to_str

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

        self.state: str = "idle"  # idle | running | paused | stopping
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
        scrapers = get_scrapers(sources or None)
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
                     "skipped_expired": 0, "duplicates": 0, "off_vertical": 0,
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
        try:
            await asyncio.gather(*(self._run_source(s) for s in scrapers))
        finally:
            self.state = "idle"
            self._log("Scrape job completed")
            if self.on_complete is not None and not self._stop.is_set():
                try:
                    await self.on_complete()
                except Exception:
                    log.exception("post-scrape hook failed")

    async def _run_source(self, scraper: BaseScraper) -> None:
        prog = self.progress[scraper.name]
        prog["status"] = "running"
        run_id = self._open_run(scraper.display_name)

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

        stale_streak = 0
        try:
            async for batch in scraper.crawl(source_stop, self._pause, on_progress):
                prog["found"] += len(batch)
                saved, expired, dupes = await asyncio.to_thread(self._ingest, batch)
                prog["saved"] += saved
                prog["skipped_expired"] += expired
                prog["duplicates"] += dupes
                if expired:
                    self._log(f"[{scraper.display_name}] skipped {expired} expired listing(s)")
                if saved:
                    self._log(f"[{scraper.display_name}] saved {saved} record(s)")

                # Listings are newest-first: N pages in a row with nothing new
                # means everything deeper is older (expired or already saved).
                stale_streak = stale_streak + 1 if (saved == 0 and batch) else 0
                if stale_streak >= settings.stale_page_streak:
                    self._log(
                        f"[{scraper.display_name}] {stale_streak} consecutive pages with "
                        f"nothing new — stopping this source (only older content ahead)"
                    )
                    source_stop.set()
            prog["status"] = "stopped" if self._stop.is_set() else "completed"
        except Exception:
            log.exception("[%s] source crashed", scraper.name)
            prog["status"] = "failed"
            prog["errors"] += 1
        finally:
            mirror.cancel()
            self._close_run(run_id, prog)
            self._log(
                f"[{scraper.display_name}] {prog['status']} — {prog['found']} found = "
                f"{prog['saved']} new saved + {prog['skipped_expired']} expired skipped + "
                f"{prog['duplicates']} already in database"
            )

    # -------------------------------------------------------------- pipeline
    def _ingest(self, batch: list[RawOpportunity]) -> tuple[int, int, int]:
        """Normalize deadline → drop expired → classify (category + verticals) →
        vertical-filter → dedupe → upsert."""
        saved = expired = dupes = 0
        expired_samples: list[str] = []
        today = date.today()
        batch_uids: set[str] = set()  # catch duplicates within the same batch too
        with session_scope() as db:
            for raw in batch:
                deadline = self.deadline_parser.parse(raw.deadline_raw, dayfirst=raw.dayfirst)
                ongoing = deadline is None and (
                    self.deadline_parser.is_ongoing(raw.deadline_raw) or raw.assume_active
                )
                if not ongoing and not self.deadline_parser.is_active(deadline, today):
                    expired += 1
                    if len(expired_samples) < 3:
                        expired_samples.append(
                            f"{raw.title[:48]!r} (deadline: {deadline or raw.deadline_raw or 'none'})"
                        )
                    continue

                category = self.classifier.classify(raw.title, raw.summary, raw.category_hint)
                vertical_body = " ".join(filter(None, [raw.summary, raw.vertical, raw.eligibility]))
                vertical_tags = classify_verticals(raw.title, vertical_body)
                if self.vertical_filter and not (set(vertical_tags) & self.vertical_filter):
                    self._count_off_vertical(raw.source_website)
                    continue
                uid = make_unique_id(raw.title, raw.organization, deadline, raw.opportunity_url)

                exists = db.execute(
                    select(Opportunity.id).where(Opportunity.unique_id == uid)
                ).scalar_one_or_none()
                if uid in batch_uids or exists is not None:
                    dupes += 1
                    continue
                batch_uids.add(uid)

                db.add(Opportunity(
                    unique_id=uid,
                    title=raw.title.strip(),
                    organization=raw.organization.strip(),
                    country=raw.country,
                    region=raw.region,
                    funding_type=raw.funding_type,
                    vertical=raw.vertical,
                    verticals=verticals_to_str(vertical_tags),
                    category=category,
                    deadline=deadline,
                    website=raw.website,
                    opportunity_url=raw.opportunity_url,
                    summary=raw.summary,
                    location=raw.location,
                    eligibility=raw.eligibility,
                    funding_amount=raw.funding_amount,
                    status=Status.ACTIVE,
                    source_website=raw.source_website,
                ))
                saved += 1
        if expired_samples:
            self._log(f"  ↳ expired examples: {'; '.join(expired_samples)}")
        return saved, expired, dupes

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
    def _open_run(source: str) -> int:
        with session_scope() as db:
            run = ScrapeRun(source_website=source, started_at=datetime.now(timezone.utc))
            db.add(run)
            db.flush()
            return run.id

    @staticmethod
    def _close_run(run_id: int, prog: dict[str, Any]) -> None:
        with session_scope() as db:
            run = db.get(ScrapeRun, run_id)
            if run:
                run.finished_at = datetime.now(timezone.utc)
                run.pages_scraped = prog["pages"]
                run.found = prog["found"]
                run.saved = prog["saved"]
                run.skipped_expired = prog["skipped_expired"]
                run.errors = prog["errors"]
                run.status = prog["status"]


manager = ScraperManager()  # module-level singleton, injected into routes
