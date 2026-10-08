import sqlite3
import sys

from scripts.dedupe_exact import candidates, main
from app.services.deduplication import make_unique_id


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE opportunities (id INTEGER PRIMARY KEY, unique_id TEXT,
        title TEXT, organization TEXT, opportunity_url TEXT, deadline TEXT,
        approved INTEGER, verticals_source TEXT, date_scraped TEXT, last_seen TEXT,
        summary TEXT);
        CREATE TABLE sent_log (opportunity_id INTEGER);
    ''')
    url = 'https://www2.fundsforngos.org/grants/test-specific-call/'
    canonical = make_unique_id('A specific call', 'Funder', None, url)
    for i in range(1, 8):
        db.execute('INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (i, canonical if i == 3 else f'old-{i}', 'A specific call',
                    'Funder', url, '2026-10-01', 0, 'auto', '2026-09-01',
                    f'2026-09-0{i}', 'Same description'))
    db.execute("UPDATE opportunities SET deadline='2027-10-01' WHERE id=4")
    db.execute("UPDATE opportunities SET summary='Different description' WHERE id=5")
    db.execute('UPDATE opportunities SET approved=1 WHERE id=6')
    db.execute('INSERT INTO sent_log VALUES (7)')
    db.commit()
    return db


def test_conservative_candidates(tmp_path):
    with database(tmp_path / 'test.db') as db:
        plan = candidates(db)
        assert {(r['drop'], r['keep']) for r in plan} == {(1, 3), (2, 3)}
        db.execute("UPDATE opportunities SET verticals_source='human' WHERE id=1")
        assert {(r['drop'], r['keep']) for r in candidates(db)} == {(2, 3)}


def test_apply_backup_and_repeat(tmp_path, monkeypatch):
    from app.core.config import settings
    path = tmp_path / 'test.db'
    database(path).close()
    backup = tmp_path / 'before.db'
    monkeypatch.setattr(settings, 'database_url', f'sqlite:///{path}')
    monkeypatch.setattr(sys, 'argv', ['dedupe_exact'])
    main()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0] == 7
    monkeypatch.setattr(sys, 'argv', ['dedupe_exact', '--apply', '--backup', str(backup)])
    main()
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0] == 7
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0] == 5
        assert candidates(db) == []
    assert backup.with_suffix('.db.removed.json').exists()
