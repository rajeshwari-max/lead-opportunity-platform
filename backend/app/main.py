"""FastAPI application entrypoint.

Run:  uvicorn app.main:app --reload --port 8000

In production (Docker), the built frontend lands in ./static next to this
package and is served directly by this app — one process, one port, one URL.
In local dev, ./static doesn't exist (frontend runs separately via `npm run
dev` + the Vite proxy), so the mount below is skipped automatically.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import app.scrapers  # noqa: F401  (imports register all scraper plugins)
from app.api.routes import router
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.database.db import init_db
from app.services.scheduler import scheduler
from app.services.scraper_manager import manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    init_db()
    # Email newly-scraped matches as soon as a scrape finishes. The hook itself
    # checks the dashboard's send_on_scrape switch, so this wiring is permanent
    # and the behaviour is controlled from the UI rather than by commenting out
    # a line here.
    from app.services import dispatch_service

    manager.on_complete = dispatch_service.post_scrape_hook

    import asyncio

    # ------------------------------------------------------ startup recovery
    # BEFORE the scheduler, and before anything can start a scrape.
    #
    # The database held 106 runs stuck in `running`, and the dashboard reported
    # them as live scrapes. Closing them out is cheap (two indexed statements)
    # and it has to happen first: a scheduled or manual run that begins while
    # those records are inconsistent inherits the confusion, and the lease
    # cannot be reasoned about while a dead process appears to hold it.
    from app.services.run_recovery import run_startup_recovery

    try:
        await asyncio.to_thread(run_startup_recovery)
    except Exception:                                           # noqa: BLE001
        # Recovery failing must not prevent the app from serving. It logs its
        # own detail; the run-state it could not fix stays visible as stuck,
        # which is the honest outcome.
        import logging

        logging.getLogger("scraper").exception("startup recovery failed")

    scheduler.start()  # restores any persisted daily/weekly/monthly/yearly schedule

    # ------------------------------------------------- background maintenance
    # These eight passes each scan or rewrite the whole opportunities table.
    # They used to be launched as eight concurrent tasks, so every boot put
    # eight full-table workloads on one SQLite file at once — on a 177 MB
    # database on a small EC2 box that is minutes of contention, competing with
    # the API's own queries and, before the change above, with a catch-up scrape
    # starting in parallel.
    #
    # Running them in sequence in ONE worker thread costs nothing in total work
    # and removes the pile-up: SQLite serialises the writes anyway, so the
    # concurrency was buying contention rather than speed.
    from app.services.amounts import backfill_amounts
    from app.services.geography import backfill_geography
    from app.services.organization import backfill_organizations
    from app.services.verticals import backfill_verticals
    from app.services.deadline_audit import audit_deadlines
    from app.services.links import repair_links
    from app.services.study_type import backfill_study_types
    from app.services.work_type import backfill_work_types

    def _startup_maintenance() -> None:
        """Idempotent repair passes, one at a time, in priority order.

        Deadlines first: it is the one whose absence is visible to users — the
        baseline had 1,481 Active rows whose deadline had passed. The rest fill
        in fields on existing rows and can wait a few seconds.
        """
        import logging

        slog = logging.getLogger("scraper")
        for name, fn in (
            ("deadline audit", audit_deadlines),          # Active/Expired drift
            ("verticals", backfill_verticals),            # routing labels
            ("links", repair_links),                      # homepage-only links
            ("geography", backfill_geography),
            ("organisation", backfill_organizations),
            ("amounts", backfill_amounts),
            ("work type", backfill_work_types),
            ("study type", backfill_study_types),
        ):
            try:
                result = fn()
                slog.info("[startup] %s: %s", name, result)
            except Exception:                                   # noqa: BLE001
                # One failing pass must not cancel the seven after it.
                slog.exception("[startup] %s failed", name)

    maintenance_task = asyncio.create_task(asyncio.to_thread(_startup_maintenance))

    yield

    maintenance_task.cancel()
    scheduler.shutdown()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------- auth gate
# A middleware rather than a route dependency: it has to cover the static
# frontend as well as the API, and a dependency on the router would leave the
# dashboard HTML itself readable by anyone.
@app.middleware("http")
async def _require_password(request, call_next):
    from fastapi.responses import JSONResponse

    from app.core.auth import COOKIE_NAME, auth_required, read_session

    if auth_required():
        path = request.url.path
        # Exempt: the login flow itself, the capability probe the frontend needs
        # before it can show a login form, and the signed one-click approval
        # links from digest emails (whose HMAC is stronger proof than this
        # password, and whose recipients are in their inbox, not the dashboard).
        exempt = (
            path.startswith(f"{settings.api_prefix}/login")
            or path.startswith(f"{settings.api_prefix}/config")
            or path.startswith(f"{settings.api_prefix}/approve/")
        )
        if not exempt and not read_session(request.cookies.get(COOKIE_NAME)):
            if path.startswith(settings.api_prefix):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            # Not an API call — let the SPA load so it can show its login form.
    return await call_next(request)


app.include_router(router, prefix=settings.api_prefix)

# Serve the built dashboard (frontend/dist, copied to ./static in the Docker
# image) at "/". Registered after the API router so /api/* always wins over
# the catch-all. html=True makes "/" resolve to static/index.html.
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")
