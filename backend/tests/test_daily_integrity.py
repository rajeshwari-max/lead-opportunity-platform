from contextlib import contextmanager
from datetime import date, datetime, timedelta
import sqlite3
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.models import Base, Opportunity, Status, SentLog
from app.services.data_integrity import maintain_database
from app.services.deduplication import make_unique_id
from app.services.actionable import application_today


def setup_db(path):
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    return engine


def row(uid, **kw):
    values = dict(unique_id=uid, title='A specific opportunity', organization='Org',
                  opportunity_url='https://www.developmentaid.org/tenders/view/123/a-specific-opportunity',
                  source_website='DevelopmentAid', deadline=application_today() + timedelta(days=2),
                  deadline_state='dated', status=Status.ACTIVE, date_scraped=datetime(2026, 1, 1),
                  last_seen=datetime(2026, 1, 1), verticals='Health')
    values.update(kw)
    return Opportunity(**values)


def test_identity_ignores_tracking_deadline_and_devaid_slug():
    f = make_unique_id
    assert f('old', '', date(2020, 1, 1), 'https://www.developmentaid.org/tenders/view/123/old?utm_source=x') == f(
        'new', '', date(2027, 1, 1), 'https://www.developmentaid.org/tenders/view/123/new')
    assert f('same', '', None, 'https://www.developmentaid.org/tenders/view/124/new') != f(
        'same', '', None, 'https://www.developmentaid.org/tenders/view/123/new')
    assert f('Lot A', '', None, 'https://procurement-notices.undp.org/view_negotiation.cfm?neg_id=1') != f(
        'Lot B', '', None, 'https://procurement-notices.undp.org/view_negotiation.cfm?neg_id=1')
    assert f('One', '', None, 'https://www.devnetjobsindia.org/rfp_assignments.aspx') != f(
        'Two', '', None, 'https://www.devnetjobsindia.org/rfp_assignments.aspx')


def test_maintenance_idempotent_preserves_decisions_and_never_reactivates(tmp_path, monkeypatch):
    path = tmp_path / 'live.db'
    engine = setup_db(path)
    with Session(engine) as db:
        a = row('legacy-a', approved=True, approved_by='admin', verticals_source='human', verticals='Livelihood')
        b = row('legacy-b', last_seen=datetime(2026, 2, 1))
        c = row('old', opportunity_url='https://example.org/calls/another-specific-notice', deadline=application_today()-timedelta(days=1))
        db.add_all([a, b, c]); db.flush()
        db.add(SentLog(member_id=9, opportunity_id=a.id))
        db.commit()
    stats = maintain_database(path)
    assert stats['merged'] == 1
    assert stats['expired'] == 1
    with sqlite3.connect(stats['backup']) as db:
        assert db.execute("SELECT count(*) FROM opportunities WHERE unique_id LIKE 'merged:%'").fetchone()[0] == 0
    assert maintain_database(path)['merged'] == 0
    from app.database import db as dbmodule
    @contextmanager
    def scope():
        with Session(engine) as db:
            yield db
            db.commit()
    monkeypatch.setattr(dbmodule, 'session_scope', scope)
    from app.services.deadline_audit import audit_deadlines
    audit_deadlines()
    with Session(engine) as db:
        merged = db.scalar(select(Opportunity).where(Opportunity.unique_id.like('merged:%')))
        assert merged.status == Status.EXPIRED
        keeper = db.scalar(select(Opportunity).where(Opportunity.id == 2))
        assert keeper.approved and keeper.approved_by == 'admin'
        assert keeper.verticals == 'Livelihood'
        assert db.scalar(select(SentLog).where(SentLog.opportunity_id == 2))
        from app.services.filter_service import FilterService
        from app.schemas.opportunity import OpportunityFilters
        for opts in ({}, {'approved': True}, {'unclassified_only': True}, {'archived': True}):
            results = FilterService(db).query(OpportunityFilters(english_only=False, has_vertical=False, **opts))
            assert merged.id not in [r.id for r in results.items]
            if not opts.get('archived'):
                assert all(r.deadline >= application_today() for r in results.items)


def test_import_recalculates_identity_and_repeat_is_noop(tmp_path, monkeypatch):
    from app.core.config import settings
    from scripts.merge_db import main
    target, source = tmp_path / 'target.db', tmp_path / 'source.db'
    eng1, eng2 = setup_db(target), setup_db(source)
    with Session(eng1) as db:
        db.add(row('old-server-id', approved=True)); db.commit()
    with Session(eng2) as db:
        db.add(row('different-laptop-id'))
        db.add(row('new', opportunity_url='https://www.developmentaid.org/tenders/view/456/new'))
        db.add(row('expired', deadline=application_today()-timedelta(days=1), opportunity_url='https://www.developmentaid.org/tenders/view/789/old'))
        db.commit()
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{target}')
    args = ['merge_db', '--source', str(source), '--only-source', 'DevelopmentAid', '--active-only']
    monkeypatch.setattr(sys, 'argv', args + ['--dry-run'])
    main()
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM opportunities').fetchone()[0] == 1
    monkeypatch.setattr(sys, 'argv', args)
    main()
    # Separate unique backup filenames are required for rapid repeat imports.
    main()
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM opportunities').fetchone()[0] == 2
        assert db.execute("SELECT approved FROM opportunities WHERE unique_id='old-server-id'").fetchone()[0] == 1
