"""The verification script's measuring half, driven by fixtures.

The crawl cannot run here — it needs the live sites — but everything AFTER the
crawl is pure and is the part that decides pass or fail. Shipping that untested
would mean the first time anyone learns whether the report is right is the night
they are relying on it.

The gates it calls are the real ingest gates, imported rather than
re-implemented. A test that models the pipeline would verify the model.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def vs():
    """Load scripts/verify_source.py as a module — it is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "verify_source", BACKEND / "scripts" / "verify_source.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows_from_worldbank():
    import app.scrapers  # noqa: F401

    from app.scrapers.worldbank import WorldBankScraper
    raw = (FIXTURES / "worldbank_procnotice.json").read_text(encoding="utf-8")
    scraper = WorldBankScraper()
    return scraper, scraper.parse_listing(raw, "")


def rows_from_bond():
    import app.scrapers  # noqa: F401

    from app.scrapers.bond import BondScraper
    html = (FIXTURES / "bond_opportunity_card.html").read_text(encoding="utf-8")
    s = BondScraper()
    return s, s.parse_listing(html, "https://www.bond.org.uk/funding-opportunities/")


# ------------------------------------------------------------ it runs at all

def test_the_script_imports_and_exposes_what_the_runbook_calls(vs):
    for fn in ("crawl", "measure", "show", "browser_count", "verify_one", "main"):
        assert hasattr(vs, fn), fn


# ------------------------------------------------------ counts are separate

def test_extracted_unique_saved_and_excluded_are_four_different_numbers(vs):
    """"found 300, saved 12" is unreadable without knowing whether the other
    288 were repeats, closed calls, or rows the parser mangled."""
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, pages_fetched=1, seconds=3.0,
                   official_total=None, before=0, after=0, use_db=False)
    assert r.extracted == len(items)
    assert r.unique <= r.extracted
    assert r.saved <= r.unique
    assert r.duplicates == r.extracted - r.unique


def test_exclusions_are_counted_under_the_reason_the_pipeline_gave(vs):
    """Not "1 excluded" — the source's own words, so an inverted ratio later is
    a visible signal that its vocabulary changed."""
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, None, 0, 0, use_db=False)
    assert r.excluded, "the fixture contains a contract award; it must be excluded"
    assert any("excluded" in reason or "closed" in reason
               for reason in r.excluded), r.excluded
    assert r.excluded_total == sum(r.excluded.values())


def test_a_run_with_rows_measures_every_completeness_dimension(vs):
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, None, 0, 0, use_db=False)
    for pct in (r.deep_link_pct, r.deadline_present_pct,
                r.deadline_parse_pct, r.organization_pct):
        assert 0.0 <= pct <= 100.0


def test_deadline_parsing_is_measured_against_the_rows_that_carry_a_string(vs):
    """A source that prints no dates is a fact about the source. A date string
    we cannot read is a defect in us, and only the second belongs in this
    percentage — dividing by every row would blame us for their silence."""
    scraper, items = rows_from_bond()
    r = vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False)
    # Both Bond cards carry a string ("31 January 2027" and "Ongoing") and both
    # resolve — one to a date, one to rolling.
    assert r.deadline_present_pct == 100.0
    assert r.deadline_parse_pct == 100.0


def test_bond_s_index_anchor_row_shows_up_in_the_deep_link_percentage(vs):
    """This is the measurement that makes the defect actionable rather than
    anecdotal: one of two cards links to the index, so 50%, against a 90% bar."""
    scraper, items = rows_from_bond()
    r = vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False)
    assert r.deep_link_pct == pytest.approx(50.0)
    assert any(f.check == "links" for f in r.evaluate())
    assert not r.passed()


# --------------------------------------------------------------- coverage

def test_no_official_total_means_coverage_stays_unproven(vs):
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, official_total=None,
                   before=0, after=0, use_db=False)
    assert r.coverage_pct is None
    assert r.as_dict()["coverage_pct"] == "unproven"


def test_an_official_total_produces_a_real_coverage_figure(vs):
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, official_total=10,
                   before=0, after=0, use_db=False)
    assert r.coverage_pct == pytest.approx(100.0 * r.unique / 10)


# ------------------------------------------------------- empty and browsers

def test_a_run_that_produced_nothing_returns_early_without_fake_percentages(vs):
    """0% of links being deep, printed for a run that fetched no rows, is a
    number with no referent — and it buries the one fact that matters."""
    scraper, _ = rows_from_worldbank()
    r = vs.measure(scraper, [], pages_fetched=0, seconds=1.0,
                   official_total=None, before=0, after=0, use_db=False)
    assert r.extracted == 0 and r.unique == 0 and r.saved == 0
    findings = r.evaluate()
    assert len(findings) == 1 and findings[0].check == "fetch"


def test_an_unmeasurable_browser_count_is_carried_through_as_none(vs):
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, None, before=None, after=None,
                   use_db=False)
    assert r.leaked_browsers is None
    assert any(f.check == "browsers" for f in r.evaluate())


def test_browser_count_returns_none_rather_than_zero_when_it_cannot_tell(vs):
    got = vs.browser_count()
    assert got is None or isinstance(got, int)


def test_interactive_chrome_is_not_mistaken_for_a_scraper_leak(vs):
    assert not vs._automation_browser(
        "chrome.exe", 'chrome.exe --type=renderer --user-data-dir="User Data"')
    assert vs._automation_browser(
        "chrome-headless-shell.exe", "chrome-headless-shell.exe --headless")


# ------------------------------------------------------------ dedupe by url

def test_rows_sharing_a_url_collapse_to_one_unique(vs):
    """The failure that looks like success: pagination re-serving a page, or a
    parser giving 86 rows the same link."""
    scraper, items = rows_from_bond()
    doubled = items + items
    r = vs.measure(scraper, doubled, 1, 1.0, None, 0, 0, use_db=False)
    assert r.extracted == 4 and r.unique == 2 and r.duplicates == 2
    assert r.duplicate_pct == pytest.approx(50.0)


def test_rows_with_no_url_do_not_collapse_into_each_other(vs):
    """Two different calls that both failed to yield a link are two problems,
    not one duplicate — counting them as one hides half the loss."""
    scraper, items = rows_from_bond()
    for i in items:
        i.opportunity_url = ""
    r = vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False)
    assert r.unique == 2


# ------------------------------------------------------- the report renders

def test_the_report_prints_without_a_measured_browser_count(vs, capsys):
    """The path most likely to be hit on a machine without psutil, and the one
    where a None slipping into a format string would crash the report after the
    crawl had already been paid for."""
    scraper, items = rows_from_worldbank()
    r = vs.measure(scraper, items, 1, 3.0, None, None, None, use_db=False)
    vs.show(r)
    out = capsys.readouterr().out
    assert "not measured" in out
    assert "unproven" in out
    assert "VERDICT" in out


def test_the_report_names_the_access_limitations(vs, capsys):
    scraper, items = rows_from_worldbank()
    vs.show(vs.measure(scraper, items, 1, 3.0, None, 0, 0, use_db=False))
    assert "ACCESS LIMITATIONS" in capsys.readouterr().out


def test_the_json_shape_is_the_contract_the_deliverables_are_built_from(vs):
    scraper, items = rows_from_worldbank()
    d = vs.measure(scraper, items, 1, 3.0, None, 0, 0, use_db=False).as_dict()
    assert json.dumps(d)          # serialisable, including the None fields
    for key in ("counts", "pagination", "completeness_pct", "coverage_pct",
                "coverage_basis", "operational", "access_limitations",
                "findings", "passed"):
        assert key in d, key


# ------------------------------------------------------------- preconditions

def test_a_note_clears_the_precondition_finding_it_names(vs):
    """--note is how "a person-established DevelopmentAid session was used"
    gets into the record. Without it the report says the precondition is
    unproven, which is correct and is the point."""
    from app.services.verification import SourceVerification

    silent = SourceVerification(key="developmentaid", pages_fetched=1,
                                extracted=1, unique=1, deep_link_pct=100.0,
                                deadline_parse_pct=100.0, organization_pct=100.0,
                                browsers_before=0, browsers_after=0)
    assert any(f.check == "precondition" for f in silent.evaluate())
    silent.notes.append("a person-established DevelopmentAid session was used")
    assert not [f for f in silent.evaluate() if f.check == "precondition"]


# ============================================================================
# Corrections after the first all-eleven run, where the report itself was wrong
# in two ways: every source read "outcome: unrecorded", and every source read
# "browsers: not measured".
# ============================================================================

def test_a_run_that_fetched_nothing_is_given_a_named_outcome(vs):
    """Both BLOCKING findings in the first run read "no page was fetched
    (outcome: unrecorded)" — which is exactly the uninformative state the
    outcome taxonomy exists to replace. Devex behind a paywall and Clean Air
    Fund's URL failing outright are different problems needing different
    people, and 'unrecorded' says neither."""
    from app.services.scrape_outcome import Outcome

    scraper, _ = rows_from_worldbank()
    outcome, _code, message = vs.classify_run(scraper, [], 0, False, "")
    assert outcome and outcome != ""
    assert outcome != "unrecorded"
    assert outcome in {o.value for o in Outcome}
    assert message


def test_a_login_walled_source_says_auth_required_not_empty(vs):
    """The single most misleading state in the platform: a source that cannot
    be reached at all, reported as one with nothing to offer."""
    from app.services.scrape_outcome import Outcome

    class Walled:
        name = "devex"
        display_name = "Devex"
        requires_js = False
        requires_login = True

    outcome, code, _ = vs.classify_run(Walled(), [], 0, False, "")
    assert outcome == Outcome.AUTH_REQUIRED.value
    assert code == "login_wall"


def test_a_source_with_no_login_that_fetched_nothing_is_not_called_auth(vs):
    """Clean Air Fund fetched 0 pages in 3.7s and needs no login, so calling it
    an auth problem would send somebody to look for a password that does not
    exist."""
    from app.services.scrape_outcome import Outcome

    scraper, _ = rows_from_worldbank()          # requires_login is falsey
    outcome, _, _ = vs.classify_run(scraper, [], 0, False, "")
    assert outcome != Outcome.AUTH_REQUIRED.value


def test_a_timeout_is_reported_as_a_timeout(vs):
    from app.services.scrape_outcome import Outcome

    scraper, _ = rows_from_worldbank()
    outcome, _, _ = vs.classify_run(scraper, [], 1, True, "")
    assert outcome == Outcome.TIMED_OUT.value


def test_a_crash_carries_its_exception(vs):
    from app.services.scrape_outcome import Outcome

    scraper, _ = rows_from_worldbank()
    outcome, _, message = vs.classify_run(
        scraper, [], 0, False, "RuntimeError: chromium would not start")
    assert outcome == Outcome.CRASHED.value
    assert "chromium" in message


def test_a_successful_run_is_not_labelled_a_failure(vs):
    scraper, items = rows_from_worldbank()
    outcome, _, _ = vs.classify_run(scraper, items, 3, False, "")
    assert outcome.startswith("success") or outcome == "confirmed_empty"


# ------------------------------------------------------------- browsers

def test_the_browser_count_falls_back_when_psutil_is_missing(vs, monkeypatch):
    """The first run reported "not measured" for all eleven sources because
    psutil is not in the venv — an UNPROVEN on every row, which is the reading
    that trains people to skip the section."""
    import builtins

    real_import = builtins.__import__

    def no_psutil(name, *a, **kw):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_psutil)
    got = vs.browser_count()
    assert isinstance(got, int), "the platform process list is the fallback"
    assert got >= 0


# ------------------------------------------------- the three link numbers

def test_dropped_rows_are_loss_and_shipped_bad_rows_are_quality(vs):
    """DevNetJobsIndia returns an empty link when no job_id can be recovered,
    so the row is dropped rather than shipped pointing at the index. Those two
    outcomes must not land in the same percentage."""
    scraper, items = rows_from_bond()
    items[1].opportunity_url = ""              # unlinkable: dropped, not shipped
    r = vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False)
    assert r.deep_link_pct == 100.0, "the one row that ships opens the call"
    assert r.deep_link_extracted_pct == pytest.approx(50.0)
    assert r.link_loss_pct == pytest.approx(50.0)


def test_bond_s_anchor_rows_count_against_quality_because_they_do_ship(vs):
    """is_usable_link accepts start_url#post-NNN, so the row lands in front of
    a reader. The distinction has to cut both ways."""
    scraper, items = rows_from_bond()
    r = vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False)
    assert r.link_loss_pct == 0.0
    assert r.deep_link_pct == pytest.approx(50.0)


def test_the_report_prints_all_three_link_numbers(vs, capsys):
    scraper, items = rows_from_bond()
    vs.show(vs.measure(scraper, items, 1, 1.0, None, 0, 0, use_db=False))
    out = capsys.readouterr().out
    assert "stored rows opening the call" in out
    assert "of everything extracted" in out
    assert "dropped for want of a link" in out
