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

from app.core.config import BASE_DIR, settings
from app.schemas.opportunity import ScheduleRequest, ScheduleStatusOut
from app.services.scraper_manager import manager

log = logging.getLogger("scraper")

_JOB_ID = "scheduled-scrape"
_DIGEST_JOB_ID = "daily-digest"
_STATE_FILE: Path = BASE_DIR / "data" / "schedule.json"


async def _daily_digest_and_reminders() -> None:
    """Send each member their new matches, then any deadline reminders due.

    Runs on its own clock rather than after every scrape: a scrape can finish
    several times a day, and nobody wants that many emails. Reminders go out in
    the same pass so a member gets at most one new-matches email and one
    reminder email per day.
    """
    from app.services import dispatch_service
    from app.services.reminder_service import send_due_reminders

    try:
        results = await asyncio.to_thread(dispatch_service.send_to_all_active)
        total = sum(r.get("sent", 0) for r in results)
        log.info("Daily digest: %s new opportunity(ies) across %s member(s)",
                 total, len(results))
    except Exception:
        log.exception("Daily digest failed")
    try:
        n = await asyncio.to_thread(send_due_reminders)
        if n:
            log.info("Daily reminders: %s deadline reminder(s) sent", n)
    except Exception:
        log.exception("Deadline reminders failed")


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
        # Digest + reminders run on their own daily schedule, independent of how
        # often scraping happens.
        self.apply_email_settings()
        self._install_deadline_audit()
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

    def _install_deadline_audit(self) -> None:
        """Re-check Active/Expired every night, not only at startup.

        `status` is decided when a row is INGESTED and then never changes by
        itself. A call scraped with a deadline three days out is stored Active
        and stays Active after that date passes. The dashboard's list query also
        filters on `deadline >= today`, so the stale status is normally hidden —
        but any view that does not apply that filter (the Approved-only view,
        which deliberately ignores deadlines) shows it, and the counts drift.

        Running this only at startup meant a server left up for a week went a
        week without a correction. Just after midnight, every night.
        """
        from app.services.deadline_audit import audit_deadlines

        def _run() -> None:
            try:
                result = audit_deadlines()
                log.info("Nightly deadline audit: %s", result)
            except Exception:
                log.exception("Nightly deadline audit failed")

        self._scheduler.add_job(
            _run, CronTrigger(hour=0, minute=15),
            id="deadline-audit", replace_existing=True,
        )
        log.info("Scheduler: nightly deadline audit at 00:15")

    def apply_email_settings(self) -> None:
        """(Re)install the daily digest job from the dashboard's settings.

        Called at startup and again whenever the settings are saved, so a new
        send time takes effect immediately rather than at the next restart.
        """
        from app.services.email_settings import load

        cfg = load()
        if not cfg.digest_enabled:
            try:
                self._scheduler.remove_job(_DIGEST_JOB_ID)
                log.info("Scheduler: automatic daily email disabled")
            except Exception:
                pass                     # not scheduled — nothing to remove
            return

        self._scheduler.add_job(
            _daily_digest_and_reminders,
            CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute),
            id=_DIGEST_JOB_ID, replace_existing=True,
        )
        log.info("Scheduler: daily digest + reminders at %02d:%02d (reminders at %s days)",
                 cfg.digest_hour, cfg.digest_minute,
                 ", ".join(str(d) for d in cfg.reminder_days))

    def next_digest_run(self) -> datetime | None:
        job = self._scheduler.get_job(_DIGEST_JOB_ID)
        return getattr(job, "next_run_time", None) if job else None

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
        """Replace the active schedule. mode=manual removes any schedule.

        Only the scrape job is removed. remove_all_jobs() was used here and it
        also deleted the daily digest job, which start() had just installed —
        so restoring a saved scrape schedule at boot silently destroyed the
        automatic email, and no digest ever went out on any instance that had
        a scrape schedule set. Nothing surfaced the loss: the schedule card
        showed the scrape times correctly and the digest simply never fired.
        """
        try:
            self._scheduler.remove_job(_JOB_ID)
        except Exception:
            pass                    # no scrape job yet — first configure()
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
