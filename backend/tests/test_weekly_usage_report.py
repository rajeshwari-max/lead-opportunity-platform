from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database.models import Base, TeamMember, UserActivityEvent, WorkspaceCredential


def test_weekly_report_uses_event_ledger_and_lists_inactive_users(tmp_path, monkeypatch):
    from app.services import weekly_usage_report as report

    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add_all([
            TeamMember(name="Anne", email="anne@catalysts.org", active=True),
            TeamMember(name="Aman", email="aman@catalysts.org", active=True),
            WorkspaceCredential(owner="anne@catalysts.org", password_hash="hash"),
            UserActivityEvent(actor="anne@catalysts.org", event_type="opportunity_viewed",
                              occurred_at=datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)),
            UserActivityEvent(actor="anne@catalysts.org", event_type="login_success",
                              occurred_at=datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)),
        ])
        db.commit()

    @contextmanager
    def scope():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(report, "session_scope", scope)
    snapshot = report.usage_snapshot(now)
    assert snapshot["engaged"] == 1
    assert snapshot["inactive"] == 1
    assert snapshot["actions"] == 2
    anne = next(row for row in snapshot["users"] if row["name"] == "Anne")
    aman = next(row for row in snapshot["users"] if row["name"] == "Aman")
    assert anne["source"] == "append-only event ledger"
    assert anne["breakdown"] == {"login_success": 1, "opportunity_viewed": 1}
    assert aman["actions"] == 0
    text = report.render_text(snapshot)
    assert "Anne <anne@catalysts.org>: 2 action(s)" in text
    assert "Aman <aman@catalysts.org>: 0 action(s)" in text
