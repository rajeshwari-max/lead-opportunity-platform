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
    scheduler.start()  # restores any persisted daily/weekly/monthly/yearly schedule

    # One-time vertical backfill for pre-existing rows (runs in the background so
    # startup stays instant; idempotent — only rows without canonical verticals).
    import asyncio

    from app.services.amounts import backfill_amounts
    from app.services.geography import backfill_geography
    from app.services.organization import backfill_organizations
    from app.services.verticals import backfill_verticals
    from app.services.deadline_audit import audit_deadlines
    from app.services.links import repair_links
    from app.services.study_type import backfill_study_types
    from app.services.work_type import backfill_work_types

    backfill_task = asyncio.create_task(asyncio.to_thread(backfill_verticals))
    # Same idea for country/region: cleans region names, aliases and title
    # artifacts out of the country column on rows saved before normalisation
    # existed. Idempotent — a clean row is left untouched.
    geo_task = asyncio.create_task(asyncio.to_thread(backfill_geography))
    # And for organisation: recovers the funder from summary prose on sources
    # that never provided it as a field. Only touches blank rows.
    org_task = asyncio.create_task(asyncio.to_thread(backfill_organizations))
    # And the amount: strips page furniture off stored values and recovers
    # figures stated in the listing text ("provides up to US$250,000").
    amt_task = asyncio.create_task(asyncio.to_thread(backfill_amounts))
    # Research vs Implementation on existing rows, so routing works from day one.
    wt_task = asyncio.create_task(asyncio.to_thread(backfill_work_types))
    # Which kind of study a research assignment is (Baseline / Endline / …).
    st_task = asyncio.create_task(asyncio.to_thread(backfill_study_types))
    # Clear links that resolve to a homepage rather than the opportunity.
    link_task = asyncio.create_task(asyncio.to_thread(repair_links))
    # Sentinel deadlines (9999-12-31 = "ongoing") and Active/Expired drift.
    dl_task = asyncio.create_task(asyncio.to_thread(audit_deadlines))
    yield
    dl_task.cancel()
    link_task.cancel()
    st_task.cancel()
    wt_task.cancel()
    amt_task.cancel()
    org_task.cancel()
    geo_task.cancel()
    backfill_task.cancel()
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
