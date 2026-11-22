"""A lead in a CMS vertical is also the matching brand's lead (Big Bets)."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, Category, Opportunity
from app.services import ml_hierarchy
from app.services.brands import brands_from_verticals

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_brand_mapping  # noqa: E402


def test_each_vertical_brings_its_brand():
    assert brands_from_verticals(["Health"]) == ["Swasti"]
    assert brands_from_verticals(["Livelihood", "Worker Wellbeing"]) == ["Vrutti", "Upfront"]
    assert brands_from_verticals(["Climate/Sustainability(ESG)"]) == ["Green Foundation"]
    assert brands_from_verticals(["E4C(Evidence for Change)", "Innovative Finance"]) == []


def test_the_hierarchy_adds_the_brand_with_the_vertical():
    selected = {"brands": [], "archetypes": [], "verticals": ["Health", "Livelihood"]}
    ml_hierarchy._enforce_hierarchy(selected)
    assert selected["brands"] == ["CMS", "Swasti", "Vrutti"]


def test_a_global_health_call_reaches_swasti_without_swasti_phrases():
    """No India in the text and none of Swasti's own phrases: the Health
    vertical alone now routes it."""
    p = ml_hierarchy._fallback(
        "Request for proposals: strengthening hospital services in Kenya",
        "Health facilities, hospital management and patient care improvement.",
        None)
    if "Health" in p.verticals:
        assert "Swasti" in p.brands


def _row(i, verticals, brands="", source="auto"):
    return Opportunity(id=i, unique_id=f"u{i}", title=f"Lead {i}", organization="F",
                       source_website="S", category=Category.GRANT, verticals=verticals,
                       brands=brands, classification_source=source,
                       opportunity_url=f"https://x.org/{i}")


def test_stored_leads_gain_the_brand_and_hand_labels_are_left_alone():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        db.add_all([
            _row(1, "Health"),
            _row(2, "Livelihood, Health", "Setu"),
            _row(3, "Health", "", source="human"),
            _row(4, "E4C(Evidence for Change)"),
        ])
        db.commit()
        added = apply_brand_mapping.apply_mapping(db, write=True)
        db.commit()
        rows = {o.id: o.brands for o in db.execute(select(Opportunity)).scalars()}
    assert rows[1] == "Swasti"
    assert set(rows[2].split(", ")) == {"Vrutti", "Swasti", "Setu"}
    assert rows[3] == ""            # a person's labels are not changed
    assert rows[4] == ""
    assert added == {"Swasti": 2, "Vrutti": 1}
