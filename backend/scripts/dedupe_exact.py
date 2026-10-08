"""Conservative duplicate cleanup. Preview by default; apply requires a backup.

Compare every stored column except row identity and scrape timestamps. Never
merge different URLs, dates, descriptions, labels or approval states.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def candidates(db):
    from app.services.deduplication import make_unique_id
    from app.services.links import link_kind

    columns = [r[1] for r in db.execute('PRAGMA table_info(opportunities)')]
    required = {'id', 'unique_id', 'title', 'organization', 'opportunity_url',
                'deadline', 'approved', 'verticals_source', 'date_scraped', 'last_seen'}
    if not required.issubset(columns):
        raise RuntimeError('Unexpected database schema; no changes made.')
    compared = [c for c in columns if c not in {'id', 'unique_id', 'date_scraped', 'last_seen'}]
    protected = set()
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cols = [r[1] for r in db.execute(f'PRAGMA table_info({quote(table)})')]
        refs = {'opportunity_id'} & set(cols)
        for fk in db.execute(f'PRAGMA foreign_key_list({quote(table)})'):
            if fk[2] == 'opportunities':
                if fk[4] not in ('id', None, ''):
                    raise RuntimeError('Unexpected opportunity reference; cleanup stopped.')
                refs.add(fk[3])
        for col in refs:
            protected.update(r[0] for r in db.execute(
                f'SELECT {quote(col)} FROM {quote(table)} WHERE {quote(col)} IS NOT NULL'))
    seen = {}
    redirects = {}
    result = []
    for row in db.execute('SELECT * FROM opportunities ORDER BY id'):
        data = dict(row)
        if (data['id'] in protected or data['approved'] or
                data['verticals_source'] == 'human' or
                data.get('classification_source') == 'human' or
                link_kind(data['opportunity_url'] or '') != 'deep'):
            continue
        payload = [data[c] for c in compared]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).digest()
        if digest not in seen:
            seen[digest] = data['id']
            continue
        keeper = dict(db.execute('SELECT * FROM opportunities WHERE id=?',
                                 (seen[digest],)).fetchone())
        if [keeper[c] for c in compared] != payload:
            raise RuntimeError('Fingerprint collision; cleanup stopped.')
        # Keep the current ingestion key where possible to avoid recreating it.
        canonical = make_unique_id(data['title'], data['organization'], None,
                                   data['opportunity_url'])
        if data['unique_id'] == canonical:
            redirects[keeper['id']] = data['id']
            drop, keep = keeper, data
            seen[digest] = data['id']
        else:
            drop, keep = data, keeper
        result.append({'drop': drop['id'], 'keep': keep['id'], 'title': data['title']})
    for item in result:
        while item['keep'] in redirects:
            item['keep'] = redirects[item['keep']]
    return result


def main():
    from app.core.config import settings
    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error('--apply requires --backup')
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'sqlite' or not url.database:
        parser.error('Only file-backed SQLite is supported.')
    path = Path(url.database).resolve(strict=True)
    print(f'Database: {path}')
    mode = 'rw' if args.apply else 'ro'
    with sqlite3.connect(path.as_uri() + f'?mode={mode}', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
        plan = candidates(db)
        print(f'Exact redundant rows: {len(plan)} (whole database, including archives)')
        for item in plan[:30]:
            print(f"Remove {item['drop']} / keep {item['keep']}: {item['title'][:100]}")
        if not args.apply or not plan:
            print('No changes made.')
            return
        backup = args.backup.expanduser().resolve()
        # Exclusive creation prevents overwriting an earlier recovery point.
        with backup.open('xb'):
            pass
        # The reserved write lock prevents changes during backup and cleanup.
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed; nothing deleted.')
        audit = backup.with_suffix(backup.suffix + '.removed.json')
        with audit.open('x', encoding='utf-8') as out:
            json.dump(plan, out, ensure_ascii=False, indent=2)
        for item in plan:
            db.execute('UPDATE opportunities SET last_seen=MAX(COALESCE(last_seen,\'\'), '
                       'COALESCE((SELECT last_seen FROM opportunities WHERE id=?),\'\')) '
                       'WHERE id=?', (item['drop'], item['keep']))
            db.execute('DELETE FROM opportunities WHERE id=?', (item['drop'],))
        db.commit()
        print(f'Deleted {len(plan)} exact copies. Backup: {backup}. Audit: {audit}')


if __name__ == '__main__':
    main()
