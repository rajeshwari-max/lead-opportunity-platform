from __future__ import annotations

from threading import Event

from app.scrapers.developmentaid import DevelopmentAidScraper


class _Page:
    def __init__(self, clears_after: int = 2):
        self.checks = 0
        self.clears_after = clears_after
        self.reloads = 0

    def title(self):
        self.checks += 1
        return "Just a moment..." if self.checks <= self.clears_after else "Search"

    def wait_for_timeout(self, _milliseconds):
        pass

    def reload(self, **_kwargs):
        self.reloads += 1


def test_devaid_waits_until_verification_clears():
    page = _Page(clears_after=3)
    assert DevelopmentAidScraper()._wait_out_challenge(
        page, "grants", timeout_ms=20_000
    )
    assert page.checks >= 4


def test_devaid_verification_wait_obeys_stop_request():
    stop = Event()
    stop.set()
    page = _Page(clears_after=999)
    assert not DevelopmentAidScraper()._wait_out_challenge(
        page, "grants", timeout_ms=20_000, stop_flag=stop
    )
    assert page.checks == 0


def test_devaid_verification_emits_watchdog_heartbeats():
    scraper = DevelopmentAidScraper()
    beats = []
    scraper._challenge_heartbeat = lambda slug, elapsed: beats.append((slug, elapsed))
    page = _Page(clears_after=35)
    assert scraper._wait_out_challenge(page, "tenders", timeout_ms=80_000)
    assert beats and beats[0] == ("tenders", 60)
