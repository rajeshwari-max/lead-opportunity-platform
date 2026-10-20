from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services.source_health_report import build_report


def _entry(name: str, outcome: str, state: str = "ok", **kw):
    return SimpleNamespace(
        display_name=name,
        last_outcome=outcome,
        state=state,
        last_http_status=kw.get("last_http_status"),
        last_error_message=kw.get("last_error_message", ""),
        note=kw.get("note", ""),
    )


def test_report_distinguishes_three_zero_result_cases():
    body = build_report([
        _entry("No response", "no_fetch", "failing", last_http_status=503),
        _entry("Changed markup", "structure_changed", "failing"),
        _entry("No calls", "confirmed_empty", "confirmed_empty"),
    ], generated_at=datetime(2026, 10, 20, 9, 30))
    assert "NO PAGE FETCHED / ACCESS FAILURE: 1" in body
    assert "PAGE FETCHED BUT NO RESULTS / STRUCTURE CHECK NEEDED: 1" in body
    assert "NO ACTIVE OPPORTUNITIES (source confirmed empty): 1" in body
    assert "No response: HTTP 503" in body


def test_admin_recipient_query_excludes_non_admins():
    from app.services.source_health_report import admin_recipients

    class _Scalars:
        def all(self):
            return ["Admin@Example.org", "admin@example.org"]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Db:
        def execute(self, statement):
            # The SQL expression itself must include the admin-only predicate.
            assert "workspace_credentials.is_admin" in str(statement)
            return _Result()

    assert admin_recipients(_Db()) == ["admin@example.org"]
