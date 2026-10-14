import asyncio
from types import SimpleNamespace

from app.services import scraper_manager as mod


def _sources(n):
    return [SimpleNamespace(name=str(i), display_name=str(i)) for i in range(n)]


def test_full_scans_are_bounded_by_the_nightly_window():
    # Every page is walked, so the run must be ended by the window instead.
    fields = type(mod.settings).model_fields
    assert fields["full_source_scan"].default is True
    assert fields["scrape_hard_stop_at"].default == "08:00"
    assert fields["scrape_health_check_at"].default == "06:00"
    assert fields["run_timeout_s"].default < 6 * 3600


def test_sources_run_five_at_a_time_rolling(monkeypatch):
    monkeypatch.setattr(mod.settings, 'full_source_scan', False)
    monkeypatch.setattr(mod.settings, 'max_concurrent_sources', 5)
    monkeypatch.setattr(mod.settings, 'run_maintenance_after_scrape', False)
    manager = mod.ScraperManager()
    sources = _sources(12)
    manager.progress = {s.name: {'status': 'queued', 'errors': 0} for s in sources}
    started, finished, active, peak = [], [], set(), []

    async def run(source):
        started.append(source.name)
        active.add(source.name)
        peak.append(len(active))
        # Source 0 is slow; the rest are quick.
        await asyncio.sleep(0.3 if source.name == '0' else 0.01)
        active.remove(source.name)
        finished.append(source.name)

    monkeypatch.setattr(manager, '_run_source', run)
    asyncio.run(manager._run(sources))
    assert len(finished) == 12
    assert max(peak) == 5
    # Rolling: later sources did not wait for the slow one to finish.
    assert finished[-1] == '0'


def test_bounded_run_has_a_whole_run_deadline(monkeypatch):
    monkeypatch.setattr(mod.settings, 'full_source_scan', False)
    monkeypatch.setattr(mod.settings, 'run_maintenance_after_scrape', False)
    manager = mod.ScraperManager()
    sources = _sources(3)
    manager.progress = {s.name: {'status': 'queued', 'errors': 0} for s in sources}
    deadlines = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(aw, timeout):
        deadlines.append(timeout)
        return await real_wait_for(aw, timeout)

    async def run(source):
        await asyncio.sleep(0)

    monkeypatch.setattr(manager, '_run_source', run)
    monkeypatch.setattr(mod.asyncio, 'wait_for', recording_wait_for)
    asyncio.run(manager._run(sources))
    assert max(deadlines) == max(300, mod.settings.run_timeout_s)


def test_full_scan_still_has_no_elapsed_cutoff(monkeypatch):
    monkeypatch.setattr(mod.settings, 'full_source_scan', True)
    monkeypatch.setattr(mod.settings, 'max_concurrent_sources', 5)
    monkeypatch.setattr(mod.settings, 'run_maintenance_after_scrape', False)
    manager = mod.ScraperManager()
    sources = _sources(7)
    manager.progress = {s.name: {'status': 'queued', 'errors': 0} for s in sources}

    async def run(source):
        await asyncio.sleep(0)

    async def forbidden_timeout(*args, **kwargs):
        raise AssertionError('Full scan must not have a whole-source/run timeout')

    monkeypatch.setattr(manager, '_run_source', run)
    monkeypatch.setattr(asyncio, 'wait_for', forbidden_timeout)
    asyncio.run(manager._run(sources))


def test_duplicate_pages_do_not_end_full_scan(monkeypatch):
    monkeypatch.setattr(mod.settings, 'full_source_scan', True)
    manager = mod.ScraperManager()
    manager.progress = {'test': dict(status='queued', pages=0, found=0, saved=0,
        skipped_expired=0, duplicates=0, spam=0, errors=0)}
    monkeypatch.setattr(manager, '_open_run', lambda *args: 1)
    monkeypatch.setattr(manager, '_close_run', lambda *args: None)
    monkeypatch.setattr(manager, '_last_good_signature', lambda *args: '')
    monkeypatch.setattr(manager, '_ingest', lambda *args: (0, 0, 1, 0, 0))

    class Source:
        name = display_name = 'test'
        stale_page_streak_override = 1
        curated = True
        requires_js = False
        async def crawl(self, stop, pause, progress):
            for page in range(1, 9):
                assert not stop.is_set()
                await progress('page_done', {'page': page, 'found': 1})
                yield [object()]

    asyncio.run(manager._run_source(Source()))
    assert manager.progress['test']['pages'] == 8
    assert manager.progress['test']['duplicates'] == 8
