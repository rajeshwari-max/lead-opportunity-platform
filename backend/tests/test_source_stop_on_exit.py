"""A timed-out source must stop its worker thread, not abandon it.

The defect this pins
--------------------
Four scrapers do their fetching in a daemon thread that feeds an unbounded
Queue. The only way to ask that thread to stop is the `source_stop` Event —
each of them checks `stop_event.is_set()` between pages.

`_guarded` wraps `_run_source` in `asyncio.wait_for(..., source_timeout_s)`.
When that fired, the coroutine was cancelled and `_run_source`'s `finally` ran
— but it only cancelled the mirror task. The Event was never set, so the thread
never learned the run was over. It kept fetching, kept parsing, and kept
`queue.put()`-ing into a queue nobody was draining.

One orphaned thread per timed-out source, each holding a browser or HTTP client
and an unbounded queue of page payloads, until the process restarts. That is
both the 75-80% CPU and the 1.6 GB of RSS, and it explains why `ps` showed no
Chromium: the JSON sources need no browser.

The code comment two functions up predicted exactly this — "the timeout only
stops US waiting" — but the remedy it names is the GLOBAL stop, which the
per-source timeout does not set.
"""
from __future__ import annotations

import asyncio
import inspect
import threading

import pytest


# ------------------------------------------------- the fix is actually there

def test_run_source_signals_its_worker_on_every_exit_path():
    from app.services.scraper_manager import ScraperManager

    src = inspect.getsource(ScraperManager._run_source)
    finally_block = src.split("finally:", 1)[1]
    code = "\n".join(l for l in finally_block.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "source_stop.set()" in code, (
        "the per-source stop Event must be set in the finally, or a cancelled "
        "coroutine leaves its worker thread running forever")


def test_the_stop_is_set_before_the_run_is_recorded():
    """Ordering matters a little: the thread gets its signal as early as
    possible, rather than after the evidence-gathering and database write."""
    from app.services.scraper_manager import ScraperManager

    block = inspect.getsource(ScraperManager._run_source).split("finally:", 1)[1]
    assert block.index("source_stop.set()") < block.index("mirror.cancel()")


# --------------------------------- the scrapers do respond to that signal

@pytest.mark.parametrize("module", ["adb", "unpp", "worldbank", "developmentaid"])
def test_every_threaded_scraper_checks_the_stop_event(module):
    """Setting the Event only helps if the walker reads it."""
    mod = __import__(f"app.scrapers.{module}", fromlist=["x"])
    assert "stop_event.is_set()" in inspect.getsource(mod), module


# --------------------------- the behaviour itself, with a real thread

def test_a_cancelled_run_lets_a_real_worker_thread_exit():
    """The mechanism end to end, without the scraper machinery.

    A daemon thread loops until an Event is set — exactly the shape of
    `_walk`. The consumer is cancelled the way `wait_for` cancels
    `_run_source`. Without the `finally` setting the Event the thread runs on;
    with it, the thread exits.
    """
    stop = asyncio.Event()
    ran = threading.Event()
    exited = threading.Event()

    def worker(loop):
        ran.set()
        while not stop.is_set():          # the crawl's between-pages check
            if exited.wait(0.01):
                return
        exited.set()

    async def scenario():
        loop = asyncio.get_running_loop()
        t = threading.Thread(target=worker, args=(loop,), daemon=True)
        t.start()
        ran.wait(2)

        async def run_source():
            try:
                await asyncio.sleep(30)          # the crawl
            finally:
                stop.set()                       # THE FIX

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run_source(), 0.05)

        t.join(timeout=3)
        return not t.is_alive()

    assert asyncio.run(scenario()), "the worker thread outlived its cancelled run"


# ------------------------------------------------ scheduler guarantees

def test_every_job_is_explicit_about_overlap_and_misfires():
    from app.services.scheduler import _JOB_GUARDS

    assert _JOB_GUARDS["max_instances"] == 1, "a scrape must not overlap itself"
    assert _JOB_GUARDS["coalesce"] is True, (
        "missed fire times must collapse into one run, not queue a backlog")
    assert _JOB_GUARDS["misfire_grace_time"] >= 60, (
        "APScheduler's 1-second default silently SKIPS a job when the loop is "
        "busy — and this loop runs an 85-source scrape in-process")


def test_all_three_jobs_carry_the_guards():
    import inspect as _i

    from app.services import scheduler as s

    src = _i.getsource(s)
    assert src.count("**_JOB_GUARDS") == 3, (
        "the scrape, the digest and the deadline audit all need them")


def test_jobs_still_replace_rather_than_duplicate():
    """Re-installing a schedule must not leave the old job registered — that is
    how one digest becomes two."""
    import inspect as _i

    from app.services import scheduler as s

    assert _i.getsource(s).count("replace_existing=True") >= 3
