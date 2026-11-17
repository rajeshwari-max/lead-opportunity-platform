"""scripts/lead_accuracy_audit.py finds the defects users have actually seen."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, Category, Opportunity, Status

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lead_accuracy_audit as audit_mod  # noqa: E402

TODAY = date(2026, 11, 17)


def _row(i, source, title, deadline, raw="", org="Funder", status=Status.ACTIVE):
    return Opportunity(id=i + 1, unique_id=f"u{i}", title=title, organization=org,
                       source_website=source, category=Category.TENDER,
                       deadline=deadline, deadline_raw=raw, deadline_state="dated",
                       deadline_convention="dayfirst", status=status,
                       opportunity_url=f"https://x.org/{i}")


def test_each_known_defect_is_reported():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        same = date(2026, 12, 1)
        db.add_all([_row(i, "UNDP Procurement", f"Notice number {i}", same, "01-Dec-26")
                    for i in range(30)])
        db.add_all([
            _row(100, "Board", "Survey of households", date(2027, 2, 1), "01-03-27"),
            _row(101, "Board", "Very distant call", TODAY + timedelta(days=4 * 365)),
            _row(102, "Board", "Audit Ref No UNDP-1 Deadline 01-Dec-26", date(2027, 1, 1)),
            _row(103, "A", "Baseline study, Odisha", date(2027, 1, 5)),
            _row(104, "B", "Baseline Study Odisha", date(2027, 1, 5)),
            _row(105, "Board", "Closed but active", date(2026, 1, 1)),
        ])
        db.commit()
        found = audit_mod.audit(db, TODAY)
    assert found["shared_source_date"][0][0] == "UNDP Procurement"
    assert [x[0] for x in found["date_disagrees_raw"]] == [101]
    assert [x[0] for x in found["far_future"]] == [102]
    assert [x[0] for x in found["run_together_title"]] == [103]
    assert found["duplicate_title"][0][0] == 2
    assert [x[0] for x in found["passed_but_active"]] == [106]
