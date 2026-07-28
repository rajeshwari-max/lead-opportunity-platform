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
    # Auto-digest after scrapes is disabled — emails are sent only via the
    # manual per-member Send button (POST /team/{id}/send).
    scheduler.start()  # restores any persisted daily/weekly/monthly/yearly schedule

    # One-time vertical backfill for pre-existing rows (runs in the background so
    # startup stays instant; idempotent — only rows without canonical verticals).
    import asyncio

    from app.services.verticals import backfill_verticals

    backfill_task = asyncio.create_task(asyncio.to_thread(backfill_verticals))
    yield
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

app.include_router(router, prefix=settings.api_prefix)

# Serve the built dashboard (frontend/dist, copied to ./static in the Docker
# image) at "/". Registered after the API router so /api/* always wins over
# the catch-all. html=True makes "/" resolve to static/index.html.
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="dashboard")
