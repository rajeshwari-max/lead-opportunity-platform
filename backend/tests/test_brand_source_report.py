"""scripts/brand_source_report.py counts brand leads per source website."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, Category, Opportunity, Status

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import brand_source_report as rep  # noqa: E402


def _row(i, source, verticals="", brands="", status=Status.ACTIVE, days=30):
    return Opportunity(id=i, unique_id=f"u{i}", title=f"Lead {i}", organization="F",
                       source_website=source, category=Category.GRANT,
                       verticals=verticals, brands=brands, status=status,
                       deadline=date.today() + timedelta(days=days), deadline_state="dated",
                       opportunity_url=f"https://x.org/{i}")


def test_counts_per_source_and_brand():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        db.add_all([
            _row(1, "DevelopmentAid", "Health", "Swasti"),
            _row(2, "DevelopmentAid", "Livelihood, Health", "Vrutti, Swasti, Setu"),
            _row(3, "World Bank", "E4C(Evidence for Change)"),
            _row(4, "World Bank", "Health", "Swasti", status=Status.EXPIRED),
        ])
        db.commit()
        live = rep.collect(db, live_only=True)
        every = rep.collect(db, live_only=False)
    assert live["DevelopmentAid"]["leads"] == 2 and live["DevelopmentAid"]["Swasti"] == 2
    assert live["DevelopmentAid"]["Vrutti"] == 1 and live["DevelopmentAid"]["Setu"] == 1
    assert live["World Bank"]["leads"] == 1 and live["World Bank"]["CMS"] == 1
    assert live["World Bank"]["Swasti"] == 0
    assert every["World Bank"]["Swasti"] == 1
