"""Digest and reminder emails: "Add to Wrike" where "Approve" used to be.

Requested 2026-10-06: remove the Approve button from mails and offer Add to
Wrike in its place. Approval was already retired from the emails; these tests
pin the replacement and, above all, that the button cannot create a task by
itself.
"""
from __future__ import annotations

import re

import pytest

from app.core.config import settings
from app.services.email_service import _digest_html, _reminder_html, wrike_url
from test_email_content import _member, _opportunity


@pytest.fixture
def wrike_on(monkeypatch):
    monkeypatch.setattr(settings, "wrike_enabled", True)
    monkeypatch.setattr(settings, "dashboard_url", "http://15.207.68.78")


def emails(opportunities):
    return (_digest_html(_member(), opportunities),
            _reminder_html(_member(), opportunities, 7))


def test_every_row_gets_an_add_to_wrike_button(wrike_on):
    rows = [_opportunity(id=i, unique_id=f"w-{i}", title=f"Fund {i}") for i in (3, 4, 5)]
    for html in emails(rows):
        assert html.count(">Add to Wrike</a>") == 3
        assert ">Action</th>" in html


def test_the_button_opens_that_row_in_the_dashboard(wrike_on):
    for html in emails([_opportunity(id=42)]):
        assert 'href="http://15.207.68.78/?view=user#wrike=42"' in html


def test_approve_is_gone_with_wrike_on_as_well(wrike_on):
    for approved in (False, True):
        for html in emails([_opportunity(approved=approved)]):
            assert "Approve" not in html
            assert "/approve/" not in html


def test_the_link_never_points_at_an_api_that_creates_a_task(wrike_on):
    """Mail clients and link scanners GET every URL in a message on delivery.
    A link that created the task would file one for every row of every digest
    without anyone clicking. The button only opens the dashboard dialog, which
    asks before creating anything."""
    for html in emails([_opportunity(id=7)]):
        hrefs = re.findall(r'href="([^"]+)"', html)
        assert not any("/api/" in h and "wrike" in h for h in hrefs)
        assert not any(h.rstrip("/").endswith("/tasks") for h in hrefs)


def test_no_button_and_no_empty_column_where_wrike_is_not_enabled(monkeypatch):
    """A button that lands on "Wrike is not enabled on this server" is worse
    than none."""
    monkeypatch.setattr(settings, "wrike_enabled", False)
    for html in emails([_opportunity()]):
        assert "Add to Wrike" not in html
        assert ">Action</th>" not in html


def test_the_more_rows_link_spans_every_column(wrike_on, monkeypatch):
    """The '+N more in <region>' row must stretch under the extra column."""
    rows = [_opportunity(id=i, unique_id=f"m-{i}", title=f"Fund {i}") for i in range(60)]
    html = _reminder_html(_member(), rows, 3)
    if "more in" in html:
        assert 'colspan="4"' in html


def test_wrike_url_shape(wrike_on, monkeypatch):
    assert wrike_url(9) == "http://15.207.68.78/?view=user#wrike=9"
    monkeypatch.setattr(settings, "dashboard_url", "http://15.207.68.78/")
    assert wrike_url(9) == "http://15.207.68.78/?view=user#wrike=9"
