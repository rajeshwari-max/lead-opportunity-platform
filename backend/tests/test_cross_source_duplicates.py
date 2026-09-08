from datetime import date
import sys
import sqlite3
from sqlalchemy.orm import Session
from app.services.cross_source_duplicates import CrossSourceIndex, already_present
from test_daily_integrity import setup_db, row
from app.services.data_integrity import maintain_database


def candidate(**changes):
    value = dict(title='Apply Now: Large Community Grants Programme (UK)',
                 deadline=date(2027, 1, 15), country='United Kingdom', category='Grant',
                 organization='Community Foundation', source_website='DevelopmentAid',
                 summary='The Community Foundation supports local charities delivering community projects for disadvantaged residents through grants for equipment and programme costs.',
                 eligibility='Registered local charities', funding_amount='GBP 10000')
    value.update(changes)
    return value


def test_cross_source_match_and_separate_notices():
    index = CrossSourceIndex([candidate()])
    other = candidate(source_website='FundsForNGOs')
    assert index.contains(other)
    assert not index.contains(candidate())  # distinct same-source IDs handled separately
    for change in ({'deadline': date(2027, 2, 15)}, {'country': 'Canada'},
                   {'category': 'Tender'}, {'title': 'Request for Proposals'}, {'country': ''},
                   {'organization': ''}, {'organization': 'Government'}, {'summary': ''},
                   {'summary': other['summary'] + ' Only schools may apply.'},
                   {'eligibility': 'Schools only'}, {'funding_amount': 'GBP 20000'}):
        assert not index.contains({**other, **change})
    index = CrossSourceIndex([candidate(organization='Foundation A')])
    assert not index.contains({**other, 'organization': 'Foundation B'})
    index = CrossSourceIndex([candidate(funding_amount='$10000')])
    assert not index.contains({**other, 'funding_amount': '€10000'})


def test_scrape_lookup_sees_pending_rows(tmp_path):
    engine = setup_db(tmp_path / 'test.db')
    with Session(engine) as db:
        first = candidate()
        db.add(row('existing', **first))
        assert already_present(db, candidate(source_website='FundsForNGOs'))
        assert not already_present(db, candidate(source_website='FundsForNGOs', deadline=date(2028, 1, 1)))


def test_import_skips_cross_website_copy_and_keeps_first(tmp_path, monkeypatch):
    from app.core.config import settings
    from scripts.merge_db import main
    target, source = tmp_path / 'target.db', tmp_path / 'source.db'
    target_engine, source_engine = setup_db(target), setup_db(source)
    with Session(target_engine) as db:
        db.add(row('server', **candidate(source_website='FundsForNGOs'),
                   opportunity_url='https://www2.fundsforngos.org/grants/community-programme/'))
        db.commit()
    with Session(source_engine) as db:
        db.add(row('laptop', **candidate()))
        db.commit()
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{target}')
    monkeypatch.setattr(sys, 'argv', ['merge_db', '--source', str(source)])
    main()
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT unique_id FROM opportunities').fetchall() == [('server',)]


def test_existing_copies_consolidated_with_backup_and_history(tmp_path):
    from app.database.models import SentLog
    path = tmp_path / 'live.db'
    engine = setup_db(path)
    with Session(engine) as db:
        first = row('first', **candidate())
        second = row('second', **candidate(source_website='FundsForNGOs'),
                     opportunity_url='https://www2.fundsforngos.org/grants/community-programme/',
                     approved=True, approved_by='reviewer', verticals_source='human', verticals='Health')
        different = row('different', **candidate(source_website='Third Website',
                                                eligibility='Schools only'),
                        opportunity_url='https://example.org/grants/different-programme/')
        db.add_all([first, second, different]); db.flush()
        db.add(SentLog(member_id=10, opportunity_id=second.id)); db.commit()
    result = maintain_database(path)
    assert result['cross_source_merged'] == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM opportunities WHERE unique_id NOT LIKE 'merged:%'").fetchone()[0] == 2
        assert db.execute('SELECT approved,approved_by,verticals_source FROM opportunities WHERE id=1').fetchone() == (1, 'reviewer', 'human')
        assert db.execute('SELECT count(*) FROM sent_log WHERE opportunity_id=1').fetchone()[0] == 1
        assert db.execute('SELECT opportunity_url FROM opportunities WHERE id=2').fetchone()[0].startswith('https://www2.')
    with sqlite3.connect(result['backup']) as db:
        assert db.execute("SELECT count(*) FROM opportunities WHERE unique_id LIKE 'merged:%'").fetchone()[0] == 0
    assert maintain_database(path)['cross_source_merged'] == 0
