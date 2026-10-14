import asyncio
from types import SimpleNamespace

from app.services import scraper_manager as mod


def test_full_scan_runs_five_at_a_time_without_elapsed_cutoff(monkeypatch):
    monkeypatch.setattr(mod.settings, 'full_source_scan', True)
    monkeypatch.setattr(mod.settings, 'max_concurrent_sources', 5)
    monkeypatch.setattr(mod.settings, 'run_maintenance_after_scrape', False)
    manager = mod.ScraperManager()
    sources = [SimpleNamespace(name=str(i), display_name=str(i)) for i in range(12)]
    manager.progress = {s.name: {'status': 'queued', 'errors': 0} for s in sources}
    finished, active, peak = [], set(), []

    async def run(source):
        # Every preceding batch must finish before the next batch starts.
        batch_start = int(source.name) // 5 * 5
        assert all(str(i) in finished for i in range(batch_start))
        active.add(source.name)
        peak.append(len(active))
        await asyncio.sleep(0.01)
        active.remove(source.name)
        finished.append(source.name)

    async def forbidden_timeout(*args, **kwargs):
        raise AssertionError('Full scan must not have a whole-source/run timeout')

    monkeypatch.setattr(manager, '_run_source', run)
    monkeypatch.setattr(asyncio, 'wait_for', forbidden_timeout)
    asyncio.run(manager._run(sources))
    assert len(finished) == 12
    assert max(peak) == 5


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
