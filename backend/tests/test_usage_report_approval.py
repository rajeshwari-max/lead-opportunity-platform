"""The weekly usage report (Excel: name, leads viewed, Active/Inactive) goes to
the reviewer first and to the recipient only after the reviewer presses
"Yes, send it" on the review page."""
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.services import weekly_usage_report as report

REVIEWER = "admin@example.org"
RECIPIENT = "lead@example.org"
NOW = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
SNAPSHOT = {
    "start": datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc), "end": NOW,
    "users": [
        {"name": "Anne", "email": "anne@example.org", "leads_viewed": 7, "active": True,
         "last_viewed": datetime(2026, 11, 22, 5, 0, tzinfo=timezone.utc)},
        {"name": "Aman", "email": "aman@example.org", "leads_viewed": 0, "active": False,
         "last_viewed": None},
    ],
    "active": 1, "inactive": 1, "total": 2, "leads_viewed": 7, "disabled_excluded": 0,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.services import email_service
    sent = []
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "usage_reports")
    monkeypatch.setattr(report.settings, "weekly_usage_report_enabled", True)
    monkeypatch.setattr(report.settings, "weekly_usage_report_recipient", RECIPIENT)
    monkeypatch.setattr(report.settings, "weekly_usage_report_reviewer", REVIEWER)
    monkeypatch.setattr(report.settings, "weekly_usage_report_require_approval", True)
    monkeypatch.setattr(report.settings, "public_base_url", "https://leads.example.org")
    monkeypatch.setattr(report, "lead_views_snapshot", lambda now=None: SNAPSHOT)
    monkeypatch.setattr(email_service, "is_configured", lambda: True)
    monkeypatch.setattr(email_service, "send_alert",
                        lambda subject, body, to=None, html=None, attachments=None:
                        sent.append({"to": to, "subject": subject, "body": body, "html": html,
                                     "attachments": attachments or []}))
    from app.api.usage_report import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return sent, TestClient(app)


def _link(html: str) -> str:
    start = html.index("https://leads.example.org/api/usage-report/review/")
    return html[start:html.index('"', start)].replace("https://leads.example.org", "")


def _sheet(data: bytes):
    return load_workbook(BytesIO(data)).active


def test_excel_lists_name_leads_viewed_and_status():
    ws = _sheet(report.render_xlsx(SNAPSHOT))
    header = [c.value for c in ws[6]]
    assert header == ["Name", "Email", "Leads viewed (last 7 days)", "Status", "Last lead viewed"]
    assert [c.value for c in ws[7]][:4] == ["Anne", "anne@example.org", 7, "Active"]
    assert [c.value for c in ws[8]][:5] == ["Aman", "aman@example.org", 0, "Inactive", "—"]
    assert "1 of 2" in ws["A3"].value


def test_report_goes_to_reviewer_with_excel_not_to_recipient(env):
    sent, _ = env
    result = report.send_weekly_usage_report()
    assert result["review_sent"] == 1 and result["sent"] == 0
    assert [m["to"] for m in sent] == [REVIEWER]
    mail = sent[0]
    assert mail["subject"].startswith("Review before sending:")
    assert f"Is it good to send to {RECIPIENT}?" in mail["body"]
    assert "Review and approve" in mail["html"]
    (name, data, mimetype), = mail["attachments"]
    assert name.endswith(".xlsx") and mimetype == report.XLSX_TYPE
    assert _sheet(data)["A7"].value == "Anne"


def test_opening_the_link_sends_nothing_and_the_button_sends_once(env):
    sent, client = env
    report.send_weekly_usage_report()
    path = _link(sent[0]["html"])
    page = client.get(path)
    assert page.status_code == 200 and f"Yes, send it to {RECIPIENT}" in page.text
    assert "Anne" in page.text and "Inactive" in page.text
    excel = client.get(path + "/excel")
    assert excel.status_code == 200 and _sheet(excel.content)["A8"].value == "Aman"
    assert len(sent) == 1                       # GET (or a link scanner) sends nothing
    done = client.post(path)
    assert done.status_code == 200 and "has been sent" in done.text
    assert [m["to"] for m in sent] == [REVIEWER, RECIPIENT]
    to_lead = sent[1]
    assert "Active users (viewed at least one lead): 1 of 2" in to_lead["body"]
    assert to_lead["attachments"][0][1] == sent[0]["attachments"][0][1]   # same sheet
    again = client.post(path)                    # second click / forwarded link
    assert "Already sent" in again.text and len(sent) == 2
    assert "Already sent" in client.get(path).text


def test_no_reviewer_means_nothing_is_sent(env, monkeypatch):
    sent, _ = env
    monkeypatch.setattr(report.settings, "weekly_usage_report_reviewer", "")
    result = report.send_weekly_usage_report()
    assert result["sent"] == 0 and result["review_sent"] == 0 and sent == []


def test_tampered_or_expired_links_are_refused(env):
    sent, client = env
    report.send_weekly_usage_report()
    path = _link(sent[0]["html"])
    bad = path[:-3] + ("AAA" if not path.endswith("AAA") else "BBB")
    assert client.post(bad).status_code == 400
    assert client.get(bad + "/excel").status_code == 400
    rid = report.read_review_token(path.rsplit("/", 1)[1])
    expired = report.make_review_token(rid, ttl=-10)
    assert client.post(f"/api/usage-report/review/{expired}").status_code == 400
    assert len(sent) == 1


def test_review_link_cannot_be_used_as_opportunity_approval(env):
    from app.services import approval_service
    sent, _ = env
    report.send_weekly_usage_report()
    token = _link(sent[0]["html"]).rsplit("/", 1)[1]
    with pytest.raises(approval_service.InvalidToken):
        approval_service.read_token(token)


def test_approval_can_be_switched_off(env, monkeypatch):
    sent, _ = env
    monkeypatch.setattr(report.settings, "weekly_usage_report_require_approval", False)
    assert report.send_weekly_usage_report()["sent"] == 1
    assert [m["to"] for m in sent] == [RECIPIENT] and sent[0]["attachments"]


def test_active_means_viewed_at_least_one_lead_last_week(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database.models import Base, LeadActivity, TeamMember, UserActivityEvent

    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(engine)
    utc = timezone.utc
    with Session(engine) as db:
        db.add_all([
            TeamMember(name="Anne", email="anne@example.org", active=True),
            TeamMember(name="Aman", email="aman@example.org", active=True),
            TeamMember(name="Old", email="old@example.org", active=True),
            TeamMember(name="Gone", email="gone@example.org", active=False),
            # Anne: lead 1 opened twice (counts once), lead 2 from the older record.
            UserActivityEvent(actor="anne@example.org", event_type="opportunity_viewed",
                              opportunity_id=1, occurred_at=datetime(2026, 11, 20, 9, tzinfo=utc)),
            UserActivityEvent(actor="Anne@Example.org", event_type="opportunity_viewed",
                              opportunity_id=1, occurred_at=datetime(2026, 11, 21, 9, tzinfo=utc)),
            LeadActivity(owner="anne@example.org", opportunity_id=2,
                         viewed_at=datetime(2026, 11, 19, 9)),
            LeadActivity(owner="anne@example.org", opportunity_id=1,
                         viewed_at=datetime(2026, 11, 21, 9)),
            # Aman only logged in: not a lead view, so inactive.
            UserActivityEvent(actor="aman@example.org", event_type="login_success",
                              occurred_at=datetime(2026, 11, 22, 9, tzinfo=utc)),
            # Old viewed a lead, but more than 7 days ago: inactive.
            LeadActivity(owner="old@example.org", opportunity_id=3,
                         viewed_at=datetime(2026, 11, 10, 9)),
        ])
        db.commit()

    @contextmanager
    def scope():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(report, "session_scope", scope)
    snap = report.lead_views_snapshot(NOW)
    by_name = {r["name"]: r for r in snap["users"]}
    assert set(by_name) == {"Anne", "Aman", "Old"}       # access switched off: not listed
    assert by_name["Anne"]["leads_viewed"] == 2 and by_name["Anne"]["active"]
    assert by_name["Aman"]["leads_viewed"] == 0 and not by_name["Aman"]["active"]
    assert by_name["Old"]["leads_viewed"] == 0 and not by_name["Old"]["active"]
    assert (snap["active"], snap["inactive"], snap["total"], snap["disabled_excluded"]) == (1, 2, 3, 1)
    assert snap["users"][0]["name"] == "Anne"            # active first


def test_review_link_passes_the_login_gate(monkeypatch):
    """The admin opens the link from her inbox: the login gate must let it
    through (the signed token is the proof), while other API paths stay closed."""
    import app.core.auth as auth
    import app.main as main
    # Other tests reload the config module; patch the objects these modules hold.
    monkeypatch.setattr(main.settings, "personal_login", True)
    monkeypatch.setattr(auth.settings, "personal_login", True)
    client = TestClient(main.app)
    assert client.get("/api/usage-report/review/not-a-token").status_code == 400
    assert client.get("/api/usage-report/review/not-a-token/excel").status_code == 400
    assert client.get("/api/stats").status_code == 401
