"""`production_enabled` has to actually disable something.

The field, and `disabled_sources()` beside it, were written in the previous
round with a docstring explaining exactly when a source should be held out of
production — and then read by nothing. A grep across the whole backend found no
caller outside the module that defines them.

So Devex, whose manifest says "Disabled until the access question is answered:
a subscription, an official feed, or dropping the source", ran on every
scheduled scrape. A disable switch nobody reads is worse than no switch,
because people believe it.

These tests are the readers.
"""
from __future__ import annotations

import pytest

from app.services.source_manifest import (
    MANIFESTS,
    contract_for,
    disabled_sources,
)


# ------------------------------------------------- the flag has a consumer

def test_the_scrape_path_imports_the_disable_helper():
    """Named directly rather than checked by grep, so deleting the call breaks
    a test instead of quietly restoring the old behaviour."""
    from app.services import scraper_manager

    assert hasattr(scraper_manager, "disabled_sources")


def test_disabled_sources_reports_the_reason_not_just_the_name():
    """"Devex is off" sends someone to look for a config file. The manifest's
    own sentence sends them to the access question."""
    blocked = disabled_sources(["devex", "ngobox", "world_bank"])
    assert set(blocked) == {"devex"}
    assert "paywall" in blocked["devex"].lower()


def test_a_source_with_no_manifest_is_not_accidentally_disabled():
    """The default contract is production_enabled=True on purpose — see the
    module docstring. Defaulting the other way would switch off 71 sources on a
    judgement nobody made."""
    assert contract_for("some_source_nobody_wrote_a_manifest_for").production_enabled
    assert disabled_sources(["some_source_nobody_wrote_a_manifest_for"]) == {}


# --------------------------------------------------- what start() does now

class _Mgr:
    """The two lines of ScraperManager.start() that select sources, isolated.

    Driving the real start() would take the cross-process run lease, spawn an
    asyncio task and begin scraping — none of which this is about.
    """

    def __init__(self):
        self.logs: list[str] = []

    def _log(self, msg: str) -> None:
        self.logs.append(msg)

    def select(self, scrapers, sources):
        if not sources:
            blocked = disabled_sources(s.name for s in scrapers)
            if blocked:
                scrapers = [s for s in scrapers if s.name not in blocked]
                for key, why in sorted(blocked.items()):
                    self._log(f"Skipping {key} — held out of production: "
                              f"{why.splitlines()[0][:160]}")
        if not scrapers:
            raise RuntimeError("Every requested source is held out of production.")
        return scrapers


class _S:
    def __init__(self, name):
        self.name = name


def test_an_all_source_run_drops_a_disabled_source():
    mgr = _Mgr()
    kept = mgr.select([_S("devex"), _S("ngobox"), _S("bond")], sources=None)
    assert [s.name for s in kept] == ["ngobox", "bond"]


def test_the_skip_is_logged_with_its_reason():
    """Silently dropping a source is the same failure in the other direction:
    the run report would show 84 sources where yesterday it showed 85, and
    nothing would say why."""
    mgr = _Mgr()
    mgr.select([_S("devex"), _S("ngobox")], sources=None)
    assert len(mgr.logs) == 1
    assert "devex" in mgr.logs[0] and "held out of production" in mgr.logs[0]


def test_naming_a_disabled_source_explicitly_still_runs_it():
    """An operator testing a fix for the very defect that disabled the source
    must not be blocked by the flag. `sources` is non-empty only because a
    person typed it."""
    mgr = _Mgr()
    kept = mgr.select([_S("devex")], sources=["devex"])
    assert [s.name for s in kept] == ["devex"]
    assert mgr.logs == []


def test_an_all_source_run_of_only_disabled_sources_refuses_rather_than_no_ops():
    """Starting a scrape of nothing, reporting success and saving zero rows is
    indistinguishable from the failure this whole round is about."""
    mgr = _Mgr()
    with pytest.raises(RuntimeError, match="held out of production"):
        mgr.select([_S("devex")], sources=None)


# ------------------------------------------------ what the health page says

def test_a_disabled_source_is_reported_as_disabled_not_as_stale():
    """Otherwise the health page sends somebody to debug a scraper that is
    switched off on purpose, and 'why is Devex stale' has a one-word answer."""
    import inspect

    from app.services import scraper_health

    src = inspect.getsource(scraper_health.source_health)
    assert "production_enabled" in src
    # Before the other states, or a source with zero rows reports
    # never_produced and the disable is never seen.
    assert src.index("production_enabled") < src.index('state = "never_produced"')


def test_disabled_is_not_counted_as_needing_attention():
    from app.services.scraper_health import summary

    class E:
        def __init__(self, state):
            self.state = state

    got = summary([E("disabled"), E("ok"), E("stale")])
    assert got["needs_attention"] == 1
    assert got["by_state"]["disabled"] == 1


def test_disabled_does_not_raise_an_alert():
    from app.services.scraper_health import alerting_sources

    class E:
        state = "disabled"
        unhealthy_streak = 99

    assert alerting_sources([E()]) == []


# ------------------------------------------------------ the manifest itself

def test_devex_is_the_only_source_currently_held_back():
    """Recorded so that switching another one off is a visible decision rather
    than something that happens and is noticed a month later."""
    off = {k for k, c in MANIFESTS.items() if not c.production_enabled}
    assert off == {"devex"}


def test_every_disabled_source_states_why():
    """A flag with no reason cannot be re-enabled by anyone but its author."""
    for key, c in MANIFESTS.items():
        if not c.production_enabled:
            assert (c.known_defect or c.owner_note).strip(), key
