"""Miscellaneous: the label an unclassified row gets, and how it is built.

The rule, as the owner set it on 2026-10-05:
  * every vertical and brand is scored as a % of ITS OWN threshold;
  * sort strongest first, add the top 1, top 2, top 3 … (n ascending);
  * the first n that reaches 100% names the row "Miscellaneous — A + B";
  * if nothing reaches 100%, the row is plain "Miscellaneous", and its near
    misses are still listed with their percentages.

The combining logic is tested on injected scores, so these tests do not move
every time a keyword list is edited. Only the last section touches the real
classifiers.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import miscellaneous as M
from app.services.miscellaneous import Sector, describe, for_row


def inject(monkeypatch, verticals=(), brands=()):
    """verticals/brands: (name, pct) pairs. Thresholds 2 and 3, as in prod."""
    v = [Sector(n, "vertical", pct / 100 * 2.0, 2.0, (f"{n.lower()}-word",)) for n, pct in verticals]
    b = [Sector(n, "brand", pct / 100 * 3.0, 3.0, (f"{n.lower()}-word",)) for n, pct in brands]
    monkeypatch.setattr(M, "_vertical_sectors", lambda t, b_: list(v))
    monkeypatch.setattr(M, "_brand_sectors", lambda t, b_: list(b))


# ---------------------------------------------------------- the combining rule

def test_nothing_close_is_plain_miscellaneous(monkeypatch):
    inject(monkeypatch)
    d = describe("Supply of office furniture").as_dict()
    assert d["label"] == "Miscellaneous"
    assert d["reached"] is False and d["sectors"] == []


def test_near_misses_that_never_add_up_are_listed_with_percentages(monkeypatch):
    """Three labels under threshold, summing to 90%: plain Miscellaneous, and
    all three shown so a reviewer sees what it was close to."""
    inject(monkeypatch, verticals=[("Health", 50), ("Livelihood", 25)], brands=[("Setu", 15)])
    d = describe("x").as_dict()
    assert d["label"] == "Miscellaneous"
    assert [(s["name"], s["pct"]) for s in d["sectors"]] == [
        ("Health", 50), ("Livelihood", 25), ("Setu", 15)]
    assert d["combined_pct"] == 90


def test_the_first_n_that_reaches_the_threshold_names_the_row(monkeypatch):
    """50 + 45 = 95 (no), 50 + 45 + 12 = 107 (yes) -> n = 3."""
    inject(monkeypatch, verticals=[("E4C(Evidence for Change)", 50)],
           brands=[("Setu", 45), ("Upfront", 12)])
    d = describe("x").as_dict()
    assert d["reached"] is True and d["n"] == 3
    assert d["label"] == "Miscellaneous — E4C(Evidence for Change) + Setu + Upfront"
    assert d["combined_pct"] == 107


def test_n_stops_at_the_first_combination_that_reaches_it(monkeypatch):
    """60 + 50 already reaches 100; the 30% third sector is not added to the
    label even though it would raise the total."""
    inject(monkeypatch, verticals=[("Health", 60), ("Livelihood", 30)], brands=[("Swasti", 50)])
    d = describe("x").as_dict()
    assert d["n"] == 2
    assert d["label"] == "Miscellaneous — Health + Swasti"
    # Still listed as a near miss, just not part of the name.
    assert "Livelihood" in [s["name"] for s in d["sectors"]]


def test_strongest_first_with_a_stable_tie_break(monkeypatch):
    inject(monkeypatch, verticals=[("Livelihood", 50), ("Health", 50)])
    d = describe("x").as_dict()
    assert d["label"] == "Miscellaneous — Health + Livelihood"   # tie -> by name


def test_exactly_100_percent_counts_as_reaching_it(monkeypatch):
    inject(monkeypatch, verticals=[("Health", 50), ("Livelihood", 50)])
    assert describe("x").reached is True


def test_a_single_label_at_threshold_is_n_equals_1(monkeypatch):
    """Possible for a brand: score >= 3.0 built only from broad words, which
    brands.py refuses to assign without one specific term."""
    inject(monkeypatch, brands=[("Setu", 105)])
    d = describe("x").as_dict()
    assert d["n"] == 1 and d["label"] == "Miscellaneous — Setu"


def test_verticals_and_brands_share_one_scale(monkeypatch):
    """Percent of each label's own threshold is what makes a vertical
    (threshold 2) and a brand (threshold 3.0) addable at all."""
    inject(monkeypatch, verticals=[("Health", 50)], brands=[("Swasti", 50)])
    d = describe("x").as_dict()
    assert {s["kind"] for s in d["combined"]} == {"vertical", "brand"}
    assert d["combined_pct"] == 100


def test_only_the_top_few_are_shown_but_all_are_summed(monkeypatch):
    inject(monkeypatch, verticals=[(f"V{i}", 15) for i in range(8)])
    d = describe("x").as_dict()
    assert len(d["sectors"]) == M.MAX_SHOWN
    assert d["n"] == 7 and d["reached"] is True       # 7 x 15 = 105


# ---------------------------------------------------- who gets the label at all

def row(**kw):
    base = dict(title="t", summary="", vertical="", eligibility="", verticals="", brands="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_row_with_a_vertical_is_never_miscellaneous():
    assert for_row(row(verticals="Health")) is None


def test_a_row_with_a_brand_is_never_miscellaneous():
    """A Swasti or Setu row already has an owner, even with no CMS vertical."""
    assert for_row(row(brands="Swasti")) is None


def test_an_unowned_row_gets_a_label():
    assert for_row(row(title="Supply of office furniture"))["label"].startswith("Miscellaneous")


def test_the_body_is_built_exactly_as_ingest_builds_it():
    """summary + the source's own vertical field + eligibility. Any other text
    and the percentages describe something the classifiers never judged."""
    assert M.body_for("a", "b", "c") == "a b c"
    assert M.body_for(None, "", "c") == "c"


# ------------------------------------------------------------ real classifiers

def test_the_thresholds_this_is_measured_against():
    """Pinned because the percentages mean nothing if these move silently."""
    from app.services.brands import ASSIGNMENT_THRESHOLD
    from app.services.verticals import _THRESHOLD

    assert _THRESHOLD == 2
    assert ASSIGNMENT_THRESHOLD == 3.0


def test_a_single_body_hit_is_half_way_to_a_vertical():
    """One body keyword scores 1 against a threshold of 2. Uses the real rules."""
    d = describe("Supply of office furniture", "for the district dairy office").as_dict()
    liv = [s for s in d["sectors"] if s["name"] == "Livelihood"]
    assert liv and liv[0]["pct"] == 50


def test_the_percentage_agrees_with_the_classifier_that_decides():
    """'agriculture' fires TWO Livelihood patterns (agricultur, agri), so one
    word scores 2 and classify_verticals assigns Livelihood. The percentage
    must say 100% for that text, or Miscellaneous would describe a row as
    'half way' that the real rule has already classified."""
    from app.services.verticals import classify_verticals

    title, body = "Supply of office furniture", "for the district agriculture office"
    liv = [s for s in describe(title, body).near_misses if s.name == "Livelihood"][0]
    assert liv.pct == 100
    assert "Livelihood" in classify_verticals(title, body)


def test_the_api_carries_the_label_only_for_unowned_rows():
    from datetime import datetime

    from app.schemas.opportunity import OpportunityOut

    common = dict(id=1, unique_id="u", title="Supply of office furniture",
                  organization="", country="", region="", funding_type="",
                  vertical="", category="Tender", deadline=None, website="",
                  opportunity_url="", summary="", location="", eligibility="",
                  funding_amount="", status="Active", source_website="s",
                  date_scraped=datetime(2026, 10, 5))
    assert OpportunityOut(**common).miscellaneous["label"] == "Miscellaneous"
    assert OpportunityOut(**common, verticals="Health").miscellaneous is None
    assert OpportunityOut(**common, brands="Setu").miscellaneous is None
