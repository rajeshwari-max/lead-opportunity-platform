"""The browser must not outlive the context — on any exit path.

Why these tests exist
---------------------
`site_auth.open_context()` has four return paths. Two launch a Browser and
return one of its contexts without keeping a reference to the owner:

    browser = _launch()
    return mask_headless(browser.new_context(...))     # browser is a local

Callers did `context.close()`, which closes the context and leaves the Chromium
process alive. It survived review because `base_scraper` held that
BrowserContext in a variable named `browser`, so `browser.close()` read as
correct cleanup, and because `with sync_playwright()` reaps the driver on
normal exit — hiding the leak except on stop, timeout and abandoned workers,
which is exactly when it matters.

These tests model both ownership shapes with a fake Playwright and assert the
process count returns to baseline. No real browser is launched, so they run in
CI on a box with no Chrome installed.
"""
from __future__ import annotations

import pytest

from app.scrapers import site_auth


class FakeProcessTable:
    """Stand-in for the OS process table. Every launch adds one, close removes."""

    def __init__(self) -> None:
        self.live: set[int] = set()
        self._next = 0

    def spawn(self) -> int:
        self._next += 1
        self.live.add(self._next)
        return self._next

    def kill(self, pid: int) -> None:
        self.live.discard(pid)


class FakePage:
    def __init__(self, ctx: "FakeContext") -> None:
        self._ctx = ctx
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """`browser` is None for a persistent context — matching Playwright."""

    def __init__(self, table: FakeProcessTable, owner: "FakeBrowser | None",
                 pid: int | None = None) -> None:
        self._table = table
        self.browser = owner                 # None => persistent
        self._pid = pid                      # set only when persistent
        self.pages: list[FakePage] = []
        self.closed = False

    def new_page(self) -> FakePage:
        p = FakePage(self)
        self.pages.append(p)
        return p

    def close(self) -> None:
        self.closed = True
        # A persistent context owns its process, so closing it ends the process.
        if self.browser is None and self._pid is not None:
            self._table.kill(self._pid)


class FakeBrowser:
    def __init__(self, table: FakeProcessTable) -> None:
        self._table = table
        self.pid = table.spawn()
        self.closed = False

    def new_context(self) -> FakeContext:
        return FakeContext(self._table, owner=self)

    def close(self) -> None:
        self.closed = True
        self._table.kill(self.pid)


def launched_context(table: FakeProcessTable) -> FakeContext:
    """The leaky shape: Browser launched, only the context handed back."""
    browser = FakeBrowser(table)             # local — goes out of scope
    return browser.new_context()


def persistent_context(table: FakeProcessTable) -> FakeContext:
    """launch_persistent_context: no separate Browser object."""
    return FakeContext(table, owner=None, pid=table.spawn())


# --------------------------------------------------------------------- tests

def test_launched_context_leaks_when_only_the_context_is_closed():
    """The regression itself. If this ever stops failing the bug is back."""
    table = FakeProcessTable()
    ctx = launched_context(table)
    ctx.close()                              # what every caller used to do
    assert len(table.live) == 1, (
        "expected the old teardown to leak exactly one browser process — "
        "if it no longer does, this test no longer models the bug"
    )


def test_close_owned_reaps_a_launched_browser():
    table = FakeProcessTable()
    ctx = launched_context(table)
    ctx.new_page()
    site_auth.close_owned(ctx)
    assert table.live == set(), "close_owned must close the owning Browser"
    assert ctx.closed
    assert all(p.closed for p in ctx.pages), "pages must be closed first"


def test_close_owned_handles_a_persistent_context():
    """A persistent context has no `.browser`; closing it must still reap."""
    table = FakeProcessTable()
    ctx = persistent_context(table)
    site_auth.close_owned(ctx)
    assert table.live == set()


def test_close_owned_tolerates_none():
    site_auth.close_owned(None)              # must not raise


def test_close_owned_still_closes_browser_when_context_close_raises():
    """Teardown runs in `finally`. A failure in one step must not abort the rest,
    or a half-dead context would strand the process it was meant to release."""
    table = FakeProcessTable()
    ctx = launched_context(table)

    def boom() -> None:
        raise RuntimeError("context is already closed")

    ctx.close = boom                          # type: ignore[method-assign]
    site_auth.close_owned(ctx)
    assert table.live == set(), "a failing context.close() must not strand the browser"


def test_close_owned_survives_a_page_that_will_not_close():
    table = FakeProcessTable()
    ctx = launched_context(table)
    page = ctx.new_page()

    def boom() -> None:
        raise RuntimeError("page is unresponsive")

    page.close = boom                         # type: ignore[method-assign]
    site_owned = ctx
    site_auth.close_owned(site_owned)
    assert table.live == set()


def test_close_owned_survives_an_inaccessible_browser_property():
    """Playwright raises on `.browser` once the connection is gone."""
    table = FakeProcessTable()
    ctx = launched_context(table)

    class Exploding(FakeContext):
        @property
        def browser(self):                    # type: ignore[override]
            raise RuntimeError("connection closed")

    ctx.__class__ = Exploding
    site_auth.close_owned(ctx)                # must not raise
    assert ctx.closed


@pytest.mark.parametrize("shape", ["launched", "persistent"])
def test_process_count_returns_to_baseline(shape: str):
    """The acceptance criterion, stated as a test: after teardown the process
    table is exactly what it was before, for both ownership models."""
    table = FakeProcessTable()
    baseline = len(table.live)
    ctx = launched_context(table) if shape == "launched" else persistent_context(table)
    assert len(table.live) == baseline + 1, "sanity: a process should exist mid-run"
    site_auth.close_owned(ctx)
    assert len(table.live) == baseline
