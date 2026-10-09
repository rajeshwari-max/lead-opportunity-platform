"""A run that finds nothing must not be recorded as "Stopped by a user".

_run_source's finally sets `source_stop` on every exit path (so a worker
thread always learns the run is over). The cancelled flag used to be read
AFTER that, so every run that extracted 0 rows was classified CANCELLED and
the dashboard said "Stopped by a user." for sources nobody had touched —
hiding whether the site blocked us, the parser broke, or the list was empty.
"""
from __future__ import annotations

import asyncio

from app.services.scrape_outcome import Outcome
from app.services.scraper_manager import ScraperManager


class _Scraper:
    name = "fake"
    display_name = "Fake source"
    requires_js = False
    curated = False
    stale_page_streak_override = None

    def __init__(self, pages: int):
        self.pages = pages

    async def crawl(self, stop, pause, on_progress):
        for page in range(1, self.pages + 1):
            await on_progress("page_done", {"page": page, "found": 0})
        if False:                       # an async generator that yields nothing
            yield []


def _run(monkeypatch, scraper, *, user_stop: bool = False):
    manager = ScraperManager()
    captured = {}
    monkeypatch.setattr(ScraperManager, "_open_run", staticmethod(lambda *a, **k: 1))
    monkeypatch.setattr(ScraperManager, "_close_run",
                        staticmethod(lambda run_id, prog, ev=None: captured.update(ev=ev)))
    monkeypatch.setattr(ScraperManager, "_last_good_signature", staticmethod(lambda s: ""))
    manager.progress = {scraper.name: {
        "display_name": scraper.display_name, "pages": 0, "found": 0, "saved": 0,
        "skipped_expired": 0, "duplicates": 0, "off_vertical": 0, "spam": 0,
        "errors": 0, "status": "queued"}}

    async def go():
        if user_stop:
            manager._stop.set()
        await manager._run_source(scraper)

    asyncio.run(go())
    return captured["ev"]


def test_zero_rows_without_a_stop_is_not_cancelled(monkeypatch):
    from app.services.scrape_outcome import classify
    ev = _run(monkeypatch, _Scraper(pages=1))
    assert ev.cancelled is False
    assert classify(ev)[0] is not Outcome.CANCELLED


def test_a_real_stop_is_still_cancelled(monkeypatch):
    ev = _run(monkeypatch, _Scraper(pages=0), user_stop=True)
    assert ev.cancelled is True
