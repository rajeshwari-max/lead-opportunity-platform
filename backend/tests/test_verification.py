"""The per-source verification contract, and the honesty rules inside it.

The brief: each priority source must report official/accessible/extracted/
unique/saved/duplicate counts, exclusions with reasons, pages expected against
pages fetched, deadline/link/organization completeness, coverage, runtime, peak
browser count, health state and access limitations — and

    "Do not claim 100% coverage unless it is proven against an official
     source-reported count, API total, or a complete manually verified listing."

That last sentence is a rule about arithmetic, and most of this file tests it.
"""
from __future__ import annotations

import pytest

from app.services.verification import (
    CONTRACTS,
    PRIORITY_SOURCES,
    Severity,
    SourceVerification,
    VerificationContract,
    contract_for,
    summarize,
)


def good(**kw) -> SourceVerification:
    """A run that passes everything, so each test can break exactly one thing."""
    base = dict(
        key="ngobox", display_name="NGOBOX",
        official_total=100, extracted=100, unique=100, duplicates=0, saved=95,
        pages_expected=5, pages_fetched=5,
        deep_link_pct=100.0, deadline_present_pct=100.0,
        deadline_parse_pct=100.0, organization_pct=95.0, furniture_rows=0,
        runtime_s=60.0, browsers_before=0, browsers_after=0,
        notes=["a working Chromium was used"],
    )
    base.update(kw)
    return SourceVerification(**base)


# ------------------------------------------------------- the coverage rule

def test_coverage_is_none_without_an_official_total():
    """Not 0, not 100 — None. The absence of a total is not a measurement."""
    assert good(official_total=None).coverage_pct is None


def test_coverage_is_never_computed_from_our_own_count():
    """The reassuring lie this exists to prevent: dividing what we found by
    what we found, which is 100% for a scraper that reached one page of nine
    hundred."""
    r = good(official_total=None, extracted=34, unique=34)
    assert r.coverage_pct is None, "coverage must not fall back to unique/unique"


def test_an_unproven_coverage_is_reported_as_a_word_not_a_number():
    """A number can be averaged into a fleet figure by a spreadsheet that has
    no idea it was invented. A string cannot."""
    assert good(official_total=None).as_dict()["coverage_pct"] == "unproven"


def test_unproven_coverage_raises_a_finding_that_names_how_to_prove_it():
    findings = good(key="world_bank", official_total=None).evaluate()
    cov = [f for f in findings if f.check == "coverage"]
    assert len(cov) == 1
    assert cov[0].severity is Severity.UNPROVEN
    assert "procnotices" in cov[0].detail, "it must say where the total comes from"


def test_coverage_against_a_real_total_is_a_real_percentage():
    assert good(official_total=200, unique=100).coverage_pct == pytest.approx(50.0)


def test_short_coverage_fails_a_source_that_is_gated_on_it():
    """UN Partner Portal publishes an exact count of OPEN calls and the walk is
    meant to reach all of them, so falling short is a pagination defect."""
    r = good(key="un_partner_portal", official_total=1000, unique=500,
             notes=["a connected UNPP session was used"])
    assert not r.passed()
    assert any(f.check == "coverage" for f in r.evaluate())


def test_low_coverage_does_not_fail_a_deliberately_bounded_walk():
    """World Bank's total is the whole 416,361-notice archive and the scraper
    walks a 60-page window of the newest ones on purpose. Gating on that total
    would fail a correct scraper every night."""
    assert CONTRACTS["world_bank"].min_coverage_pct is None
    r = good(key="world_bank", official_total=416_361, unique=6_000)
    assert r.coverage_pct < 2.0
    assert not [f for f in r.evaluate() if f.check == "coverage"
                and f.severity is not Severity.UNPROVEN]


def test_unproven_coverage_alone_does_not_fail_a_run():
    """UNPROVEN records a gap in knowledge. Failing on it would mean every
    source without a published total is permanently broken, which is false and
    would train people to ignore the result."""
    assert good(official_total=None).passed()


# --------------------------------------------------- blocking beats noise

def test_a_run_that_fetched_nothing_reports_one_finding_not_twelve():
    """Zero pages makes every percentage below it meaningless. Printing
    '0% of links are deep' next to it buries the one fact that matters."""
    findings = good(pages_fetched=0, extracted=0, outcome="no_fetch").evaluate()
    assert len(findings) == 1
    assert findings[0].severity is Severity.BLOCKING
    assert findings[0].check == "fetch"
    assert "no_fetch" in findings[0].detail


def test_pages_fetched_but_nothing_parsed_is_a_different_finding():
    """The distinction the outcome taxonomy already makes, kept here: a fetch
    problem and a parser problem need different people."""
    findings = good(pages_fetched=3, extracted=0).evaluate()
    assert len(findings) == 1
    assert findings[0].check == "parse"
    assert "fixtures" in findings[0].detail


# ------------------------------------------------------------- browsers

def test_an_unmeasured_browser_check_is_unproven_not_zero():
    """"0 leaked" from a check nobody ran is how a leak survives for weeks."""
    r = good(browsers_before=None, browsers_after=None)
    assert r.leaked_browsers is None
    assert any(f.check == "browsers" and f.severity is Severity.UNPROVEN
               for f in r.evaluate())


def test_a_surviving_browser_is_blocking():
    r = good(browsers_before=0, browsers_after=2)
    assert r.leaked_browsers == 2
    assert any(f.check == "browsers" and f.severity is Severity.BLOCKING
               for f in r.evaluate())
    assert not r.passed()


def test_browsers_that_were_already_running_are_not_counted_as_a_leak():
    """The baseline is taken before the run for exactly this reason."""
    assert good(browsers_before=3, browsers_after=3).leaked_browsers == 0


# ------------------------------------------------------- quality thresholds

@pytest.mark.parametrize("field,value,check", [
    ("deep_link_pct", 10.0, "links"),
    ("deadline_parse_pct", 10.0, "deadlines"),
    ("organization_pct", 1.0, "organization"),
    ("furniture_rows", 4, "furniture"),
])
def test_each_quality_dimension_fails_on_its_own(field, value, check):
    r = good(**{field: value})
    assert any(f.check == check for f in r.evaluate())
    assert not r.passed()


def test_duplicates_are_measured_against_what_was_extracted():
    r = good(extracted=100, unique=50, duplicates=50)
    assert r.duplicate_pct == pytest.approx(50.0)
    assert any(f.check == "duplicates" for f in r.evaluate())


def test_developmentaid_is_allowed_the_duplicates_its_design_produces():
    """Its walk partitions the catalogue into overlapping searches, so the same
    tender legitimately arrives several times. Only the unique count means
    anything, and a 5% bar would fail every correct run."""
    r = good(key="developmentaid", extracted=1000, unique=500, duplicates=500,
             official_total=None,
             notes=["a person-established DevelopmentAid session was used"])
    assert not [f for f in r.evaluate() if f.check == "duplicates"]


def test_stopping_short_of_the_expected_pages_is_reported():
    r = good(pages_expected=10, pages_fetched=3)
    assert any(f.check == "pagination" and "stopped early" in f.detail
               for f in r.evaluate())


def test_no_expected_page_count_means_no_pagination_claim():
    """Unknown is not "fine". It must not silently pass as if it were checked."""
    assert not [f for f in good(pages_expected=None).evaluate()
                if f.check == "pagination"]


def test_runtime_over_the_cap_is_reported():
    assert any(f.check == "runtime" for f in good(runtime_s=100_000).evaluate())


# ------------------------------------------------------------ preconditions

def test_a_source_that_needs_a_session_says_so_when_the_run_is_silent():
    r = good(key="developmentaid", official_total=None, notes=[])
    pre = [f for f in r.evaluate() if f.check == "precondition"]
    assert pre and pre[0].severity is Severity.UNPROVEN
    assert "session" in pre[0].detail


def test_recording_the_precondition_clears_it():
    r = good(key="developmentaid", official_total=None,
             notes=["a person-established DevelopmentAid session was used"])
    assert not [f for f in r.evaluate() if f.check == "precondition"]


# ------------------------------------------------- exclusions carry reasons

def test_exclusions_are_kept_per_reason_not_as_one_number():
    """"288 excluded" is not actionable. "241 contract awards, 47 closed" is —
    and if the ratio ever inverts, that is the signal the vocabulary changed."""
    r = good(excluded={"contract_award": 241, "status closed": 47})
    assert r.excluded_total == 288
    assert r.as_dict()["counts"]["excluded"]["contract_award"] == 241


# --------------------------------------------------- the eleven, and the bar

def test_every_priority_source_has_a_contract():
    missing = [k for k in PRIORITY_SOURCES if k not in CONTRACTS]
    assert not missing, f"no verification contract for: {missing}"


def test_there_are_exactly_eleven_priority_sources():
    assert len(PRIORITY_SOURCES) == 11
    assert len(set(PRIORITY_SOURCES)) == 11


def test_every_priority_contract_states_its_access_limitations():
    """"None" is an answer; blank is an omission, and the blank ones are the
    sources somebody discovers at 3am."""
    for key in PRIORITY_SOURCES:
        assert CONTRACTS[key].access_limitations.strip(), key


def test_every_priority_key_is_a_registered_scraper():
    """A contract keyed to a name nothing registers is a check that never runs
    — the same silent miss that made contract_for('world_bank') fall through to
    a placeholder for months."""
    import app.scrapers  # noqa: F401 — importing registers every plugin

    from app.scrapers.registry import SCRAPER_REGISTRY
    missing = [k for k in PRIORITY_SOURCES if k not in SCRAPER_REGISTRY]
    assert not missing, f"not registered scrapers: {missing}"


def test_every_priority_source_also_has_a_scope_manifest():
    """The two files answer different questions — what a source is FOR, and
    what a good run of it looks like — and a source needs both."""
    from app.services.source_manifest import MANIFESTS, KEY_ALIASES

    missing = [k for k in PRIORITY_SOURCES
               if k not in MANIFESTS and KEY_ALIASES.get(k, "") not in MANIFESTS]
    assert not missing, f"no scope manifest for: {missing}"


def test_a_source_with_no_contract_gets_one_that_admits_it():
    c = contract_for("some_source_nobody_wrote_a_contract_for")
    assert "No verification contract" in c.access_limitations


def test_devex_declares_that_nothing_about_it_can_be_verified():
    """It is paywalled and has fetched zero pages in 11 runs. A contract that
    quietly held it to 90% coverage would report a failure every night and say
    nothing about the actual reason."""
    c = CONTRACTS["devex"]
    assert c.official_total_source == ""
    assert c.min_coverage_pct is None
    assert "PAYWALLED" in c.access_limitations


# ------------------------------------------------------------------ summary

def test_the_fleet_summary_reports_unproven_beside_passed_not_inside_it():
    results = [
        good(),                                    # passes, coverage proven
        good(official_total=None),                 # passes, coverage unproven
        good(deep_link_pct=1.0),                   # fails
    ]
    s = summarize(results)
    assert s == {"sources": 3, "passed": 2, "failed": 1,
                 "unproven_coverage": 1, "blocking": 0}


def test_a_default_contract_does_not_invent_thresholds_it_cannot_justify():
    c = VerificationContract(key="x")
    assert c.min_coverage_pct is None
    assert c.official_total_source == ""


# ============================================================================
# Corrections made after the 2026-08-30 verification run of all eleven sources.
# Two of the eight failures it reported were defects in this file rather than
# in a scraper, which is its own kind of finding: a bar set in the wrong place
# fails correct code and sends someone to fix something that is not broken.
# ============================================================================

def test_one_duplicate_in_a_large_sample_is_not_evidence_of_anything():
    """World Bank: 1 repeat in 87 rows = 1.1%, against a 1% bar, reported as a
    pagination defect. A listing whose order shifts between two requests
    produces exactly that. A run has to exceed the percentage AND a floor."""
    r = good(key="world_bank", extracted=87, unique=86, duplicates=1,
             official_total=None)
    assert r.duplicate_pct > CONTRACTS["world_bank"].max_duplicate_pct
    assert not [f for f in r.evaluate() if f.check == "duplicates"]


def test_the_floor_does_not_rescue_a_source_whose_pagination_is_broken():
    """ADB: 36 extracted, 12 unique across three pages — the same twelve rows
    three times. That must still fail, and loudly."""
    r = good(key="adb_tenders", extracted=36, unique=12, duplicates=24,
             official_total=None, notes=["a working Chromium was used"])
    dup = [f for f in r.evaluate() if f.check == "duplicates"]
    assert dup, "24 duplicates in 36 rows has to be reported"
    assert "24 of 36" in dup[0].detail, "the counts belong in the message"


def test_the_finding_names_counts_not_only_a_percentage():
    """"1.1%" sends nobody anywhere. "1 of 87" ends the investigation."""
    r = good(extracted=100, unique=50, duplicates=50)
    dup = [f for f in r.evaluate() if f.check == "duplicates"][0]
    assert "50 of 100" in dup.detail


# ------------------------------------------------- links: three questions

def test_deep_links_are_measured_over_what_would_be_stored():
    """DevNetJobsIndia drops rows whose job_id cannot be recovered, ON PURPOSE
    — returning an empty link so the row is never shipped pointing at the
    index. Measuring over everything extracted scored that 65.6% against a 100%
    bar and called correct behaviour a failure."""
    r = good(key="devnet", deep_link_pct=100.0, deep_link_extracted_pct=65.6,
             link_loss_pct=34.4, official_total=None)
    assert not [f for f in r.evaluate() if f.check == "links"]


def test_dropping_a_third_of_the_source_is_still_reported_just_not_as_badness():
    """The rows are not bad rows on the dashboard — they are calls the source
    published that never reach it. That is a coverage problem, and it gets its
    own finding rather than being folded into the link quality figure."""
    r = good(key="devnet", deep_link_pct=100.0, link_loss_pct=34.4,
             official_total=None)
    loss = [f for f in r.evaluate() if f.check == "link loss"]
    assert loss, "a third of the output vanishing must be reported"
    assert "never reach" in loss[0].detail


def test_a_source_losing_nothing_raises_no_loss_finding():
    assert not [f for f in good(link_loss_pct=0.0).evaluate()
                if f.check == "link loss"]


def test_the_two_link_numbers_are_both_published():
    d = good(deep_link_pct=100.0, deep_link_extracted_pct=65.6,
             link_loss_pct=34.4).as_dict()["completeness_pct"]
    assert d["deep_link"] == 100.0
    assert d["deep_link_of_extracted"] == 65.6
    assert d["link_loss"] == 34.4


def test_bond_still_fails_on_links_because_its_rows_do_reach_the_dashboard():
    """The distinction has to cut both ways. Bond's index-anchor rows are NOT
    dropped — is_usable_link accepts them — so they land in front of a reader
    and count against it."""
    r = good(key="bond", deep_link_pct=50.9, deep_link_extracted_pct=50.9,
             link_loss_pct=0.0, official_total=None)
    assert any(f.check == "links" for f in r.evaluate())
    assert not r.passed()
