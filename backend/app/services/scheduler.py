"""Scheduler — manual / daily / weekly / monthly / yearly / cron scrapes.

Production-ready: the configuration and run history persist to
backend/data/schedule.json, so the schedule survives restarts and page
refreshes, and the UI can always show Next Run / Last Run / Status /
Last Successful Execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import BASE_DIR
from app.schemas.opportunity import ScheduleRequest, ScheduleStatusOut
from app.services.scraper_manager import manager

log = logging.getLogger("scraper")

_JOB_ID = "scheduled-scrape"
_STATE_FILE: Path = BASE_DIR / "data" / "schedule.json"


class ScrapeScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self.current: ScheduleRequest = ScheduleRequest(mode="manual")
        # Run history (persisted): ISO timestamps + last outcome.
        self.last_run: datetime | None = None
        self.last_status: str | None = None
        self.last_success: datetime | None = None
        self._load_state()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        # Re-apply the persisted schedule so automatic runs resume after restart.
        if self.current.mode != "manual":
            try:
                self.configure(self.current, persist=False)
            except ValueError:
                log.warning("Persisted schedule invalid — falling back to manual")
                self.current = ScheduleRequest(mode="manual")
                return

            # This scheduler only fires while the server process is actually
            # alive at the scheduled instant — it's in-process (APScheduler),
            # not an OS-level task. A dev server that restarts frequently (or a
            # desktop app that isn't left running overnight) can easily miss
            # every single 2 AM window forever, leaving last_run stuck at null
            # even though the schedule is configured correctly. Catch up here:
            # if today's slot has already passed and we haven't run since, run
            # it now instead of silently waiting for tomorrow's exact instant.
            if self._missed_run_today():
                log.info(
                    "Scheduler: missed today's %s run (server wasn't running "
                    "at %02d:%02d) — catching up now",
                    self.current.mode, self.current.hour, self.current.minute,
                )
                asyncio.create_task(self._scrape_all())

    def _missed_run_today(self) -> bool:
        """Best-effort check — good enough to decide 'catch up or not', not
        exact-to-the-second. cron mode is too generic to safely reason about
        here, so it's excluded (falls back to waiting for its next real fire)."""
        req = self.current
        now = datetime.now()
        if req.mode == "daily":
            due_today = True
        elif req.mode == "weekly":
            due_today = now.weekday() == 0   # Monday
        elif req.mode == "monthly":
            due_today = now.day == 1
        elif req.mode == "yearly":
            due_today = now.month == 1 and now.day == 1
        else:
            return False
        if not due_today:
            return False
        scheduled = now.replace(hour=req.hour, minute=req.minute, second=0, microsecond=0)
        if now < scheduled:
            return False
        return self.last_run is None or self.last_run.date() != now.date()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ----------------------------------------------------------- configuration
    def configure(self, req: ScheduleRequest, persist: bool = True) -> None:
        """Replace the active schedule. mode=manual removes any schedule."""
        self._scheduler.remove_all_jobs()
        self.current = req
        if req.mode == "manual":
            log.info("Scheduler: manual mode (no automatic scrapes)")
            if persist:
                self._save_state()
            return

        triggers = {
            "daily": CronTrigger(hour=req.hour, minute=req.minute),
            "weekly": CronTrigger(day_of_week="mon", hour=req.hour, minute=req.minute),
            "monthly": CronTrigger(day=1, hour=req.hour, minute=req.minute),
            "yearly": CronTrigger(month=1, day=1, hour=req.hour, minute=req.minute),
        }
        if req.mode == "cron":
            if not req.cron:
                raise ValueError("cron expression required when mode='cron'")
            trigger = CronTrigger.from_crontab(req.cron)
        else:
            trigger = triggers.get(req.mode)
            if trigger is None:
                raise ValueError(f"Unknown schedule mode: {req.mode}")

        self._scheduler.add_job(self._scrape_all, trigger, id=_JOB_ID, replace_existing=True)
        log.info("Scheduler: %s scrape configured (%02d:%02d)", req.mode, req.hour, req.minute)
        if persist:
            self._save_state()

    # ---------------------------------------------------------------- status
    def status(self) -> ScheduleStatusOut:
        next_run: datetime | None = None
        job = self._scheduler.get_job(_JOB_ID) if self._scheduler.running else None
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.astimezone(timezone.utc).replace(tzinfo=None)
        return ScheduleStatusOut(
            **self.current.model_dump(),
            next_run=next_run,
            last_run=self.last_run,
            last_status=self.last_status,
            last_success=self.last_success,
        )

    # --------------------------------------------------------------- execution
    async def _scrape_all(self) -> None:
        self.last_run = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            await manager.start()
        except RuntimeError:
            log.warning("Scheduled scrape skipped — a job is already running")
            self.last_status = "skipped"
            self._save_state()
            return
        # Wait for the job to finish so the outcome can be recorded.
        try:
            while manager.state != "idle":
                await asyncio.sleep(2)
            failed = any(
                p.get("status") == "failed" for p in manager.progress.values()
            )
            self.last_status = "failed" if failed else "success"
            if not failed:
                self.last_success = datetime.now(timezone.utc).replace(tzinfo=None)
            log.info("Scheduled scrape finished: %s", self.last_status)
        except Exception:
            log.exception("Scheduled scrape monitoring failed")
            self.last_status = "failed"
        self._save_state()

    # -------------------------------------------------------------- persistence
    def _save_state(self) -> None:
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(
                json.dumps(
                    {
                        "config": self.current.model_dump(),
                        "last_run": self.last_run.isoformat() if self.last_run else None,
                        "last_status": self.last_status,
                        "last_success": self.last_success.isoformat() if self.last_success else None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            log.exception("Could not persist schedule state")

    def _load_state(self) -> None:
        if not _STATE_FILE.exists():
            return
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            self.current = ScheduleRequest(**data.get("config", {}))
            for attr in ("last_run", "last_success"):
                raw = data.get(attr)
                setattr(self, attr, datetime.fromisoformat(raw) if raw else None)
            self.last_status = data.get("last_status")
        except (ValueError, TypeError, json.JSONDecodeError):
            log.exception("Could not load persisted schedule state — using defaults")


scheduler = ScrapeScheduler()
