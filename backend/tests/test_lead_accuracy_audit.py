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


def _row(i, source, title, deadline, raw="", org="Funder", status=Status.ACTIVE,
         country="India"):
    return Opportunity(id=i + 1, unique_id=f"u{i}", title=title, organization=org,
                       source_website=source, category=Category.TENDER,
                       country=country,
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



def test_apply_repairs_titles_dates_duplicates_and_junk_without_deleting():
    from sqlalchemy import func, select

    from app.database.models import LeadActivity
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as db:
        db.add_all([
            _row(1, "UNDP Procurement",
                 "National Consultant - Payment for Ecosystem Services Ref No UNDP-PNG-00214 "
                 "UNDP Office/Country UNDP-PNG Deadline 01-Dec-26", date(2026, 12, 1)),
            _row(2, "Board", "Survey of households", date(2027, 2, 1), "01-03-27"),
            _row(3, "Board", "Old call read wrongly", date(2027, 2, 1), "01-03-26"),
            _row(4, "DevelopmentAid", "Provision of architectural designs", date(2026, 12, 8)),
            _row(5, "DevelopmentAid", "Provision of architectural designs", date(2026, 12, 8)),
            _row(6, "DevelopmentAid", "Provision of architectural designs", date(2027, 6, 1)),
            _row(7, "DevelopmentAid", "971503529923", date(2027, 1, 1)),
            _row(8, "DevelopmentAid", "Provision of architectural designs", date(2026, 12, 8),
                 country="Kenya"),
        ])
        db.add(LeadActivity(owner="asha@example.org", opportunity_id=6))   # row 5 (id 6)
        db.commit()
        fixed = audit_mod.repair(db, TODAY)
        db.commit()
        rows = {o.id: o for o in db.execute(select(Opportunity)).scalars()}
        assert db.execute(select(func.count(Opportunity.id))).scalar_one() == 8   # none deleted
    assert rows[2].title == "National Consultant - Payment for Ecosystem Services"
    assert rows[3].deadline == date(2027, 3, 1)
    assert rows[4].deadline == date(2026, 3, 1) and rows[4].status == Status.EXPIRED
    # the copy a person viewed is the one kept; the other is hidden, not deleted
    assert rows[6].unique_id == "u5" and rows[5].unique_id == "merged:u4"
    assert rows[7].unique_id == "u6"                 # same title, different date: kept
    assert rows[8].status == Status.EXPIRED          # junk title archived
    assert rows[9].unique_id == "u8"                 # different country: distinct call
    assert fixed == {"run_together_title": 1, "date_disagrees_raw": 2,
                     "duplicate_title": 1, "junk_title": 1}
