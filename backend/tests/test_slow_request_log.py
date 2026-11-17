"""Slow API requests are logged so a slowdown can be traced."""
from __future__ import annotations

import logging


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_a_slow_api_request_is_logged_without_its_query_string(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    # Other tests reload the config module; use the objects app.main holds.
    app, settings = main_mod.app, main_mod.settings
    monkeypatch.setattr(settings, "slow_request_seconds", 0.0)
    perf = logging.getLogger("performance")
    handler = _Collect()
    perf.addHandler(handler)
    old_level, old_disabled = perf.level, perf.disabled
    perf.setLevel(logging.DEBUG)
    perf.disabled = False
    try:
        TestClient(app).get(f"{settings.api_prefix}/config?search=secret-term")
    finally:
        perf.removeHandler(handler)
        perf.setLevel(old_level)
        perf.disabled = old_disabled
    assert any("slow request" in l and f"{settings.api_prefix}/config" in l
               for l in handler.lines), handler.lines
    assert not any("secret-term" in l for l in handler.lines)
