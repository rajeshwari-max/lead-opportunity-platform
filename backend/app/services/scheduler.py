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
from zoneinfo import ZoneInfo
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import BASE_DIR, settings
from app.schemas.opportunity import ScheduleRequest, ScheduleStatusOut
from app.services.scraper_manager import manager

log = logging.getLogger("scraper")

_JOB_ID = "scheduled-scrape"
_DIGEST_JOB_ID = "daily-digest"
_NIGHT_START_JOB_ID = "scrape-night-start"
_HEALTH_JOB_ID = "scrape-health-check"
_HARD_STOP_JOB_ID = "scrape-hard-stop"
_MONTHLY_HEALTH_JOB_ID = "monthly-source-health-report"

# APScheduler's defaults already give max_instances=1 and coalesce=True, but
# they are spelled out on every job below because "the default happens to be
# right" is not something the next person should have to look up — and because
# misfire_grace_time is the one default that is NOT right here.
#
# It defaults to ONE SECOND. If the event loop is busy when a job's fire time
# arrives — and this loop runs an 85-source scrape in-process — the job is
# silently skipped rather than run late. A missed digest that nothing reports
# looks exactly like a digest nobody wanted.
#
# max_instances=1  a scrape cannot start while the previous one is running.
#                  There is a cross-process database lease as well; this is the
#                  in-process half of the same rule.
# coalesce=True    several missed fire times collapse into one run, so a
#                  restart after downtime does not queue up a backlog.
_JOB_GUARDS = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
# A short dispatch grace allows normal timer jitter, never hours-late runs.
# Restarts do not catch up; the next configured slot is the next automatic start.
_SCRAPE_JOB_GUARDS = {
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 60,
}

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


def _hhmm(value: str) -> tuple[int, int] | None:
    """'06:00' -> (6, 0); blank or malformed -> None (job off)."""
    try:
        h, m = (int(x) for x in (value or "").strip().split(":"))
    except ValueError:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def _alert(subject: str, verdict: str, report: dict) -> None:
    """Email the scrape-window verdict to the alert address. Never raises."""
    counts = ", ".join(f"{n} {s}" for s, n in sorted(report["statuses"].items()))
    lines = [
        verdict, "",
        f"Running for {report['elapsed_min']:.0f} min; last page finished "
        f"{report['idle_min']} min ago.",
        f"Sources ({report['total']}): {counts}.",
        f"New opportunities saved so far: {report['saved']}.",
    ]
    if report["running"]:
        lines += ["", "In progress:"]
        lines += [f"  - {r['source']}: page {r['pages']}, {r['saved']} saved, "
                  f"idle {r['idle_min']} min" for r in report["running"]]
    try:
        from app.services import email_service
        email_service.send_alert(subject=subject, body="\n".join(lines))
    except Exception:                                           # noqa: BLE001
        log.exception("scrape window alert could not be sent")


def scrape_outcome(progress: dict) -> str:
    statuses = [p.get("status") for p in progress.values()]
    if not statuses:
        return "skipped"
    completed = statuses.count("completed")
    if completed == len(statuses):
        return "success"
    if completed:
        return "partial_success"
    return "failed" if "failed" in statuses else "stopped"


class ScrapeScheduler:
    def __init__(self) -> None:
        # Job defaults stated rather than inherited.
        #
        # APScheduler's defaults are max_instances=1, coalesce=False and
        # misfire_grace_time=1s. Two of those are wrong here and the third was
        # only right by accident:
        #
        #   max_instances=1  — correct, and now deliberate. Two overlapping
        #       scrapes would share one SQLite file and one browser budget.
        #       (It is not sufficient on its own: it is per-process, so it does
        #       nothing about a second application process. That needs the
        #       database lease in Phase 2b.)
        #
        #   coalesce=True    — if several slots were missed while the process
        #       was down, run the job ONCE on recovery, not once per missed
        #       slot. Without it a server down for three weeks would fire three
        #       weekly scrapes back to back.
        #
        #   misfire_grace_time — 1 second means a job whose event loop was busy
        #       at the scheduled instant is dropped silently. Eight concurrent
        #       full-table backfills run at startup, so "the loop was busy" is
        #       not hypothetical here.
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": settings.scheduler_misfire_grace_s,
            }
        )
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
        self._install_scrape_window()
        self._install_monthly_source_health_report()
        # Re-apply the persisted schedule so automatic runs resume after restart.
        if self.current.mode != "manual":
            try:
                self.configure(self.current, persist=False)
            except ValueError:
                log.warning("Persisted schedule invalid — falling back to manual")
                self.current = ScheduleRequest(mode="manual")
                return

            # Never launch a scrape on restart: only the configured clock slots.

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
                from app.services.data_integrity import maintain_database
                try:
                    maintain_database()
                finally:
                    result = audit_deadlines()
                log.info("Nightly deadline audit: %s", result)
            except Exception:
                log.exception("Nightly deadline audit failed")

        # 08:15, after the nightly scrape window (22:00-08:00) and before the
        # 09:00 digest. At 00:05 it ran a whole-database pass at the same time
        # as the nightly scrape, competing for the database write lock.
        self._scheduler.add_job(
            _run, CronTrigger(hour=8, minute=15, timezone="Asia/Kolkata"),
            id="deadline-audit", replace_existing=True,
            **{**_JOB_GUARDS, "misfire_grace_time": 60},
        )
        log.info("Scheduler: daily integrity and deadline audit at 08:15 Asia/Kolkata")

    # ------------------------------------------------------------ scrape window
    def _install_scrape_window(self) -> None:
        """Start (22:00), health check (06:00), hard stop (08:00)."""
        tz = settings.scrape_window_timezone
        for job_id, at, func in (
            (_NIGHT_START_JOB_ID, settings.scrape_window_start_at, self._window_start),
            (_HEALTH_JOB_ID, settings.scrape_health_check_at, self._window_health_check),
            (_HARD_STOP_JOB_ID, settings.scrape_hard_stop_at, self._window_hard_stop),
        ):
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
            hm = _hhmm(at)
            if hm is None:
                log.info("Scheduler: %s off", job_id)
                continue
            self._scheduler.add_job(
                func, CronTrigger(hour=hm[0], minute=hm[1], timezone=tz),
                id=job_id, replace_existing=True, **_JOB_GUARDS,
            )
            log.info("Scheduler: %s at %02d:%02d %s", job_id, hm[0], hm[1], tz)

    async def _window_start(self) -> None:
        """Open the fixed overnight window.

        Every automatic start still passes through ScraperManager's database
        lease, so a manual run or another worker cannot create a second batch
        of Chrome processes.
        """
        await self._scrape_all()

    def _install_monthly_source_health_report(self) -> None:
        try:
            self._scheduler.remove_job(_MONTHLY_HEALTH_JOB_ID)
        except Exception:
            pass
        hm = _hhmm(settings.source_health_report_at)
        if not settings.source_health_report_enabled or hm is None:
            log.info("Scheduler: monthly source-health report off")
            return
        day = min(28, max(1, int(settings.source_health_report_day)))
        from app.services.source_health_report import send_monthly_report
        self._scheduler.add_job(
            send_monthly_report,
            CronTrigger(
                day=day, hour=hm[0], minute=hm[1],
                timezone=settings.source_health_report_timezone,
            ),
            id=_MONTHLY_HEALTH_JOB_ID, replace_existing=True, **_JOB_GUARDS,
        )
        log.info(
            "Scheduler: admin-only monthly source-health report on day %s at "
            "%02d:%02d %s", day, hm[0], hm[1],
            settings.source_health_report_timezone,
        )

    async def _window_health_check(self) -> None:
        """Still scraping at the health-check time: carry on if it is making
        progress, stop it now if it is stuck. An email says which."""
        if manager.state not in ("running", "paused"):
            log.info("Scrape health check: no scrape running (last outcome: %s)",
                     self.last_status)
            return
        report = manager.health_report(stall_s=settings.scrape_stall_minutes * 60)
        stop_at = settings.scrape_hard_stop_at or "the next manual stop"
        if report["stalled"]:
            verdict = (f"STUCK — no page has finished for {report['idle_min']} min. "
                       "Stopped now.")
            log.error("Scrape health check: %s", verdict)
            await manager.stop()
        else:
            verdict = f"Healthy — still making progress. It continues until {stop_at}."
            log.info("Scrape health check: %s", verdict)
        await asyncio.to_thread(
            _alert, f"Nightly scrape still running at {settings.scrape_health_check_at}: "
            + verdict.split(" — ")[0], verdict, report)

    async def _window_hard_stop(self) -> None:
        """End of the scrape window: stop whatever is still scraping."""
        if manager.state not in ("running", "paused"):
            return
        report = manager.health_report(stall_s=settings.scrape_stall_minutes * 60)
        log.warning("Scrape window closed at %s — stopping the scrape",
                    settings.scrape_hard_stop_at)
        await manager.stop()
        await asyncio.to_thread(
            _alert, f"Nightly scrape stopped at {settings.scrape_hard_stop_at} (window closed)",
            "The scrape was still running when the window closed, so it was "
            "stopped. Sources that were not reached run first tomorrow night.",
            report)

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
            CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute, timezone=cfg.digest_timezone),
            id=_DIGEST_JOB_ID, replace_existing=True, **_JOB_GUARDS,
        )
        log.info("Scheduler: daily digest + reminders at %02d:%02d %s (reminders at %s days)",
                 cfg.digest_hour, cfg.digest_minute, cfg.digest_timezone,
                 ", ".join(str(d) for d in cfg.reminder_days))

    def next_digest_run(self) -> datetime | None:
        job = self._scheduler.get_job(_DIGEST_JOB_ID)
        return getattr(job, "next_run_time", None) if job else None

    def _missed_run_today(self) -> bool:
        """Best-effort check — good enough to decide 'catch up or not', not
        exact-to-the-second. cron mode is too generic to safely reason about
        here, so it's excluded (falls back to waiting for its next real fire).

        Schedule wall-clock times use the saved IANA timezone. Run history
        remains UTC; compare actual instants to handle midnight consistently.
        """
        req = self.current
        now = datetime.now(timezone.utc).astimezone(ZoneInfo(req.timezone))
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
        return self.last_run is None or self.last_run.replace(tzinfo=timezone.utc) < scheduled.astimezone(timezone.utc)

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
        if req.mode != "manual":
            triggers = {
                "daily": CronTrigger(hour=req.hour, minute=req.minute, timezone=req.timezone),
                "weekly": CronTrigger(day_of_week="mon", hour=req.hour, minute=req.minute, timezone=req.timezone),
                "monthly": CronTrigger(day=1, hour=req.hour, minute=req.minute, timezone=req.timezone),
                "yearly": CronTrigger(month=1, day=1, hour=req.hour, minute=req.minute, timezone=req.timezone),
            }
            if req.mode == "cron":
                if not req.cron:
                    raise ValueError("cron expression required when mode='cron'")
                trigger = CronTrigger.from_crontab(req.cron, timezone=req.timezone)
            else:
                trigger = triggers.get(req.mode)
                if trigger is None:
                    raise ValueError(f"Unknown schedule mode: {req.mode}")
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

        self._scheduler.add_job(self._scrape_all, trigger, id=_JOB_ID,
                                replace_existing=True, **_SCRAPE_JOB_GUARDS)
        log.info("Scheduler: %s scrape configured (%s, timezone=%s)", req.mode,
                 req.cron if req.mode == "cron" else f"{req.hour:02d}:{req.minute:02d}", req.timezone)
        if persist:
            self._save_state()

    # ---------------------------------------------------------------- status
    def status(self) -> ScheduleStatusOut:
        next_run: datetime | None = None
        job = self._scheduler.get_job(_JOB_ID) if self._scheduler.running else None
        if job is None and self._scheduler.running:
            # The fixed overnight job is always the canonical automatic run;
            # a dashboard-configured schedule is optional and may be manual.
            job = self._scheduler.get_job(_NIGHT_START_JOB_ID)
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
        # Two automatic starts can now coincide: the fixed 22:00 window and a
        # schedule still saved in the dashboard (e.g. the earlier daily 00:00).
        # The second one must not record itself as the latest run, or the
        # panel would show "skipped" while the real run is still going.
        if manager.state != "idle":
            log.info("Scheduled scrape not started — a scrape is already running "
                     "(state=%s); its status is left unchanged", manager.state)
            return
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            await manager.start()
        except RuntimeError:
            log.warning("Scheduled scrape skipped — a job is already running")
            self.last_run = started_at
            self.last_status = "skipped"
            self._save_state()
            return
        self.last_run = started_at
        self.last_status = "running"
        self._save_state()
        # Wait for the job to finish so the outcome can be recorded.
        try:
            while manager.state != "idle":
                await asyncio.sleep(2)
            self.last_status = scrape_outcome(manager.progress)
            if self.last_status == "success":
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
