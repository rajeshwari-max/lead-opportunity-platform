import json
import re
import zipfile
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, HistoricalLead
from scripts import import_historical_leads as importer


HEADERS = [
    "Title", "Item type", "Assignee", "Status group", "Status", "Start date",
    "Due date", "Duration", "Duration (Hours)", "Effort", "Project or folder",
    "Billing type", "Actual fees, ₹", "Actual cost, ₹", "Planned fees, ₹",
    "Planned cost, ₹",
]


def _wrike_style_workbook(path: Path) -> None:
    """Create a workbook whose dimension incorrectly says A1 like the export."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append([
        "Opportunity owner@example.org", "Task", "Person <private@example.org>",
        "All finished", "6. Awarded/Accepted/Attended", None, None, "", "", "",
        "Hello team@example.org", "", "", "", "", "",
    ])
    sheet.append([
        "Ignore this heading", "", "Someone", "", "", None, None, "", "", "",
        "", "", "", "", "", "",
    ])
    workbook.save(path)

    replacement = path.with_suffix(".rewritten.xlsx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(replacement, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(
                    br'<dimension ref="[^"]+"/?>',
                    b'<dimension ref="A1"/>',
                    content,
                    count=1,
                )
            target.writestr(info, content)
    replacement.replace(path)


def _row(title="A grant", status="6. Awarded/Accepted/Attended"):
    outcome, won_lost, action, reason = importer.status_mapping(status)
    return {
        "fingerprint": importer.fingerprint_for(title),
        "external_id": "",
        "title": title,
        "source": "OST Analytics",
        "opportunity_url": "",
        "opportunity_type": "Grant",
        "sector": "",
        "verticals": "Health",
        "brands": "CMS",
        "archetypes": "Devsol",
        "geography": "India",
        "focus_area": "Health",
        "eligibility_text": "",
        "funding": "",
        "company_relevance": "",
        "action_taken": action,
        "status": status,
        "outcome": outcome,
        "won_lost": won_lost,
        "reason": reason,
        "reviewer_comment": "",
        "event_date": "2024-04-01",
        "raw_data": {"status_group": "All finished"},
    }


def test_reader_recovers_bad_dimension_and_removes_personal_data(tmp_path):
    path = tmp_path / "history.xlsx"
    _wrike_style_workbook(path)

    rows = importer.read_excel(path)

    assert len(rows) == 1
    assert rows[0]["title"] == "Opportunity [email removed]"
    assert rows[0]["focus_area"] == "Hello [email removed]"
    assert rows[0]["outcome"] == "Positive / accepted"
    assert rows[0]["won_lost"] == ""
    assert "assignee" not in rows[0]["raw_data"]
    assert "Person" not in json.dumps(rows[0])


def test_status_mapping_covers_final_and_workflow_states():
    assert importer.status_mapping("Contracted & Finance Intimated")[1] == "won"
    assert importer.status_mapping("Alliance/Platform joined")[0] == "Positive / accepted"
    assert importer.status_mapping("Alliance/Platform joined")[1] == ""
    assert importer.status_mapping("2.2 Awd/Conf/Panel Appd by OST")[0] == "In progress"
    assert importer.status_mapping("12.1.Not Sub-Missed Deadline") == (
        "Not pursued / not submitted", "", "Rejected", "Deadline was missed"
    )
    assert importer.status_mapping("8. Passed on to partners")[0] == "Referred"
    assert importer.status_mapping("12.13 Infeasible/ High risk😧")[1] == ""
    assert importer.status_mapping("12.14 Alliance/Platform not jo")[1] == ""
    assert importer.status_mapping("Alliance/Platform deferred")[0] == "On hold"


def test_deduplicate_keeps_the_most_decisive_duplicate():
    lost = _row("Same title", "7.Lost/rejected/cud not attend")
    won = _row("Same title", "Contracted & Finance Intimated")
    rows, removed = importer.deduplicate([lost, won])
    assert removed == 1
    assert len(rows) == 1
    assert rows[0]["won_lost"] == "won"


def test_jsonl_is_sanitized_and_identity_is_recomputed(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({
        **_row("Contact owner@example.org"),
        "fingerprint": "untrusted",
        "raw_data": {
            "assignee": "Private Person <private@example.org>",
            "project_or_folder": "Shared by manager@example.org",
        },
    }) + "\n", encoding="utf-8")

    [row] = importer.read_jsonl(path)

    assert row["fingerprint"] == importer.fingerprint_for("Contact [email removed]")
    assert "assignee" not in row["raw_data"]
    assert "@example.org" not in json.dumps(row)


def test_apply_is_idempotent_and_preserves_existing_rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(importer, "SessionLocal", sessions)
    monkeypatch.setattr(importer, "init_db", lambda: None)

    with sessions() as db:
        db.add(HistoricalLead(
            fingerprint=importer.fingerprint_for("Existing"),
            title="Existing",
            source="Manual",
        ))
        db.commit()

    first = importer.apply_rows([_row()])
    with sessions() as db:
        imported_at = db.query(HistoricalLead).filter_by(title="A grant").one().imported_at
    second = importer.apply_rows([_row()])

    assert first == {"inserted": 1, "updated": 0, "unchanged": 0, "total": 1}
    assert second == {"inserted": 0, "updated": 0, "unchanged": 1, "total": 1}
    with sessions() as db:
        assert db.query(HistoricalLead).count() == 2
        assert db.query(HistoricalLead).filter_by(title="Existing").one().source == "Manual"
        assert db.query(HistoricalLead).filter_by(title="A grant").one().imported_at == imported_at
