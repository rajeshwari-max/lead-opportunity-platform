"""The Social Business vertical, and the three keywords that are not literal.

Twenty of the twenty-three supplied terms are transcribed as given. Three are
scoped, and those three are what this file mostly tests, because a keyword that
matches too much is the failure this project has already paid for twice: the
digest filter that matched "ict" inside "District", and `\\bResearch\\b` alone
accounting for 114 of 738 Health tags.

    "Equity"    In development writing this almost always means fairness —
                gender equity, health equity, equitable access. Bare, it would
                pull a large share of the Health and Worker Wellbeing corpus
                into a vertical about capital structure.
    "Markets"   Bare, it matches market research, labour market, emerging
                markets, go-to-market.
    "Commodity" Bare, it matches procurement boilerplate.

The tests that matter most here are the negatives.
"""
from __future__ import annotations

import pytest

from app.services.verticals import (
    VERTICAL_SOCIAL_BUSINESS,
    VERTICALS,
    classify_verticals,
)


def tags(title: str, summary: str = "") -> list[str]:
    return classify_verticals(title, summary)


def is_sb(title: str, summary: str = "") -> bool:
    return VERTICAL_SOCIAL_BUSINESS in tags(title, summary)


# ------------------------------------------------------------- it exists

def test_it_is_a_vertical():
    assert VERTICAL_SOCIAL_BUSINESS == "Social Business"
    assert VERTICAL_SOCIAL_BUSINESS in VERTICALS


def test_it_is_last_so_existing_vertical_order_is_unchanged():
    """The frontend renders these in order and members' saved routing is by
    name, but a reordering would still churn every UI list for no reason."""
    assert VERTICALS[-1] == VERTICAL_SOCIAL_BUSINESS
    assert VERTICALS[:6] == [
        "Livelihood", "Health", "E4C(Evidence for Change)",
        "Climate/Sustainability(ESG)", "Worker Wellbeing", "Innovative Finance",
    ]


def test_the_frontend_list_matches_the_backend_list():
    """These are two hand-maintained copies of one list. They have drifted
    before — the comment in types.ts records the last time, when two names were
    short of the backend's and rows were being discarded."""
    from pathlib import Path

    ts = (Path(__file__).parents[2] / "frontend" / "src" / "lib" / "types.ts")
    if not ts.exists():                      # backend-only checkout
        pytest.skip("frontend not present")
    text = ts.read_text(encoding="utf-8")
    for vertical in VERTICALS:
        assert f'"{vertical}"' in text, f"{vertical} missing from types.ts"


# --------------------------------------------------- the supplied keywords

@pytest.mark.parametrize("title", [
    "Impact Ventures Challenge for Southeast Asia",
    "Social Venture Accelerator: applications open",
    "Agribusiness Development Facility — call for proposals",
    "Commodity price risk facility for smallholder exporters",
    "Market Access Programme for producer groups",
    "Debt Capital facility for early-stage enterprises",
    "Equity investment into rural distribution enterprises",
    "Post-harvest management and cold chain grants",
    "Value chain strengthening in horticulture",
    "Supply chain resilience fund",
    "Organic Food business incubation",
    "Support to residue-free produce enterprises",
    "Farmers market infrastructure grants",
    "Improving farmer incomes through aggregation",
    "Blended financing window for agri-SMEs",
    "Innovative financing for rural enterprise",
    "Revolving fund for women-led cooperatives",
    "Revolving grant facility for producer organisations",
    "Strengthening cooperatives and farmer producer organisations",
])
def test_the_supplied_terms_are_recognised(title):
    assert is_sb(title), f"{title!r} should tag Social Business"


def test_npm_is_the_agriculture_sense():
    assert is_sb("Scaling NPM practices with farmer groups")
    assert is_sb("Non-pesticidal management training for cotton farmers")


# ------------------------------------------------- the scoped ones: negatives

def test_social_equity_is_not_equity_capital():
    """The single highest-risk term. "Equity" in this corpus overwhelmingly
    means fairness."""
    for title in ("Advancing gender equity in maternal health",
                  "Health equity research grants",
                  "Equity, diversity and inclusion programme",
                  "Equitable access to clean water"):
        assert not is_sb(title), f"{title!r} must not tag Social Business"


def test_equity_capital_still_does():
    for title in ("Equity capital for social enterprises",
                  "Private equity co-investment facility",
                  "Quasi-equity instrument for agri-SMEs"):
        assert is_sb(title)


def test_the_word_market_on_its_own_is_not_a_market_system():
    for title in ("Market research consultancy services",
                  "Labour market analysis for the garment sector",
                  "Go-to-market strategy support",
                  "Emerging markets macroeconomic review"):
        assert not is_sb(title), f"{title!r} must not tag Social Business"


def test_market_systems_language_does():
    for title in ("Market systems development in northern Kenya",
                  "Market linkages for tribal producers",
                  "Improving access to markets for smallholders"):
        assert is_sb(title)


def test_commodity_needs_its_trading_sense():
    assert not is_sb("Procurement of commodity items for office use")
    assert is_sb("Commodity trading platform for farmer collectives")
    assert is_sb("Agricultural commodities price transparency initiative")


# --------------------------------------------------------- multi-label

def test_it_is_a_second_lens_not_a_slice_taken_from_another_vertical():
    """Deliberate overlap. Value chain and cooperatives already belong to
    Livelihood; blended financing already belongs to Innovative Finance. A row
    can and should carry both — but it means these rows now reach members
    routed to EITHER vertical, which is a routing consequence, not a bug."""
    got = tags("Blended financing for agribusiness value chains and cooperatives")
    assert VERTICAL_SOCIAL_BUSINESS in got
    assert "Livelihood" in got


def test_adding_it_does_not_strip_an_existing_vertical():
    """The regression that would matter most: an existing row losing a tag it
    had before. Classification is additive, so nothing here should remove
    Health from a health row."""
    assert tags("Maternal health systems strengthening") == ["Health"]
    assert "Climate/Sustainability(ESG)" in tags("Climate adaptation finance")


def test_an_unrelated_row_gains_nothing():
    assert not is_sb("Tuberculosis diagnostics procurement")
    assert not is_sb("School curriculum development consultancy")
