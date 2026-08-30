"""Each source's date convention, tested against that source's own format.

The brief asked for this by name: "Investigate deadline parsing carefully.
Existing logs show suspicious date clusters such as 2026-01-09, 2026-02-09 and
2026-03-09, which may indicate day/month inversion. Preserve raw deadlines and
add source-specific date-format tests."

09/01/2026 is 9 January or 1 September depending on who wrote it, and a wrong
answer here is not a display bug — it moves a bid deadline by eight months.

Two things are checked, and the second is the one that had actually broken:
the convention each source DECLARES, and whether anything makes that
declaration true.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.scrapers.registry import SCRAPER_REGISTRY
from app.services.deadline_parser import DeadlineParser
from app.services.source_manifest import KEY_ALIASES, MANIFESTS, contract_for

parser = DeadlineParser()


# ------------------------------------------- the ambiguous case, both ways

def test_the_same_string_is_two_different_days():
    """The whole reason a per-source convention exists."""
    assert parser.parse("09/01/2026", dayfirst=True) == date(2026, 1, 9)
    assert parser.parse("09/01/2026", dayfirst=False) == date(2026, 9, 1)


def test_the_cluster_the_brief_flagged_is_reproducible():
    """2026-01-09, 2026-02-09, 2026-03-09 — the day pinned at 09 while months
    walk. Under dayfirst that is what 09/01, 09/02, 09/03 produce, and under
    monthfirst those same strings are the 1st, 2nd and 3rd of September."""
    dayfirst = [parser.parse(f"09/0{m}/2026", dayfirst=True) for m in (1, 2, 3)]
    assert [d.day for d in dayfirst] == [9, 9, 9]
    assert [d.month for d in dayfirst] == [1, 2, 3]

    monthfirst = [parser.parse(f"09/0{m}/2026", dayfirst=False) for m in (1, 2, 3)]
    assert [d.month for d in monthfirst] == [9, 9, 9]
    assert [d.day for d in monthfirst] == [1, 2, 3]


# ------------------------------------------------- per-source real formats

@pytest.mark.parametrize("key,raw,expected", [
    # Indian sources write 31/07/2026 and 31.07.2026.
    ("devnet", "31/07/2026", date(2026, 7, 31)),
    ("devnet", "Last date - 31.07.2026", date(2026, 7, 31)),
    ("devnet", "09/01/2026", date(2026, 1, 9)),        # ambiguous, dayfirst
    ("ngobox", "15/08/2026", date(2026, 8, 15)),
    ("ngobox", "Deadline: 09/02/2026", date(2026, 2, 9)),
])
def test_a_dayfirst_source_reads_dayfirst(key, raw, expected):
    assert MANIFESTS[key].deadline_format == "dayfirst"
    assert parser.parse(raw, dayfirst=True) == expected


@pytest.mark.parametrize("key,raw,expected", [
    # These return ISO from an API, which is unambiguous either way — that is
    # exactly why they are declared iso rather than left to a guess.
    ("worldbank", "2026-07-31", date(2026, 7, 31)),
    ("worldbank", "2026-01-09", date(2026, 1, 9)),
    ("unpp", "2026-09-01", date(2026, 9, 1)),
])
def test_an_iso_source_is_unambiguous(key, raw, expected):
    assert MANIFESTS[key].deadline_format == "iso"
    assert parser.parse(raw, dayfirst=True) == expected
    assert parser.parse(raw, dayfirst=False) == expected, (
        "an ISO date must not depend on the dayfirst flag at all")


@pytest.mark.parametrize("raw,expected", [
    # GrantWatch is a US site: 09/18/26 is September 18, and the day-value 18
    # makes it unambiguous, which is the only reason it ever parsed correctly.
    ("09/18/26", date(2026, 9, 18)),
    ("12/25/2026", date(2026, 12, 25)),
])
def test_a_us_source_reads_monthfirst(raw, expected):
    assert parser.parse(raw, dayfirst=False) == expected


def test_a_us_date_under_the_wrong_convention_is_silently_wrong():
    """Not an error — a different, plausible date. That is what makes this
    class of bug survive review: nothing looks broken."""
    assert parser.parse("09/01/2026", dayfirst=True) != \
        parser.parse("09/01/2026", dayfirst=False)


# ------------------------------------------ unambiguous formats never move

@pytest.mark.parametrize("raw", [
    "31 July 2026", "31-Jul-2026", "July 31, 2026", "31st July 2026",
    "2026-07-31", "Apply by: 31 July 2026",
])
def test_a_named_month_means_the_same_day_under_either_convention(raw):
    """A day past 12 or a spelled month cannot be inverted. Only the
    genuinely ambiguous strings carry the risk, which is why the audit script
    measures those alone."""
    assert parser.parse(raw, dayfirst=True) == parser.parse(raw, dayfirst=False)
    assert parser.parse(raw, dayfirst=True) == date(2026, 7, 31)


def test_a_day_past_twelve_cannot_be_a_month():
    assert parser.parse("31/07/2026", dayfirst=True) == date(2026, 7, 31)
    # dateutil falls back to the only reading that is a real date.
    assert parser.parse("31/07/2026", dayfirst=False) == date(2026, 7, 31)


# ------------------------------- the declaration has to be reachable at all

def test_every_manifest_is_reachable_from_the_key_the_pipeline_uses():
    """This is the defect this file was written after.

    The manifests are keyed `worldbank`, `unpp`, `adb`; the scrapers register
    as `world_bank`, `un_partner_portal`, `adb_tenders`, and `_ingest` passes
    the REGISTRY key. So `contract_for(scraper.name)` fell through to the
    needs_review placeholder for the three sources whose contracts matter most.

    It never failed loudly: a placeholder has no expected types and no status
    vocabulary, and `record_is_in_scope` on an empty contract returns
    keep=True. The scope check was a no-op for those sources and looked
    exactly like a working one.
    """
    reachable = set()
    for registry_key in SCRAPER_REGISTRY:
        contract = contract_for(registry_key)
        if contract.key in MANIFESTS:
            reachable.add(contract.key)

    unreachable = sorted(set(MANIFESTS) - reachable)
    assert not unreachable, (
        f"these manifests can never be selected by the ingest path, which "
        f"passes the registry key: {unreachable}. Add an alias to "
        f"source_manifest.KEY_ALIASES.")


@pytest.mark.parametrize("registry_key,manifest_key", sorted(KEY_ALIASES.items()))
def test_each_alias_points_at_a_registered_scraper_and_a_real_manifest(
        registry_key, manifest_key):
    """An alias for a key nobody registers is dead configuration that looks
    like coverage."""
    assert registry_key in SCRAPER_REGISTRY, registry_key
    assert manifest_key in MANIFESTS, manifest_key


def test_the_world_bank_contract_is_actually_applied_at_ingest():
    """The specific consequence: World Bank's feed is mostly contract awards,
    and without the contract nothing excluded them."""
    contract = contract_for("world_bank")
    from app.services.source_manifest import record_is_in_scope

    keep, why = record_is_in_scope(contract, record_type="contract_award")
    assert not keep and why


def test_the_unpp_contract_is_actually_applied_at_ingest():
    contract = contract_for("un_partner_portal")
    from app.services.source_manifest import record_is_in_scope

    assert record_is_in_scope(contract, record_type="project")[0] is False
    assert record_is_in_scope(contract, record_type="eoi",
                              source_status="Open")[0] is True


# ------------------------------------ the convention is stated per source

@pytest.mark.parametrize("key", sorted(MANIFESTS))
def test_a_confirmed_source_states_its_date_convention(key):
    """A global default is how 09/01/2026 becomes the wrong day. Sources still
    under review are exempt — an unstated convention on an unreviewed source is
    an honest blank, not a claim."""
    from app.services.source_manifest import ScopeStatus

    contract = MANIFESTS[key]
    if contract.scope_status is not ScopeStatus.CONFIRMED:
        pytest.skip("scope not confirmed yet")
    if not contract.production_enabled:
        pytest.skip("not in production; a convention for a source nobody "
                    "scrapes would be a guess with nothing to check it against")
    if contract.deadline_format:
        assert contract.deadline_format in {"dayfirst", "monthfirst", "iso"}, (
            f"{key} states an unknown convention "
            f"{contract.deadline_format!r}")
        return
    # A blank is allowed only when the contract SAYS it is undetermined.
    # Silence and "we checked and it does not matter" look identical
    # otherwise, and one of them is a 48,350-row guess waiting to happen.
    assert "DATE CONVENTION NOT ESTABLISHED" in (contract.owner_note or ""), (
        f"{key} is confirmed and in production but states no date convention "
        f"and does not say the convention is undetermined")
