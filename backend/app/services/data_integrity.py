"""Transactional identity migration and daily duplicate suppression.

Superseded rows remain as recovery/history records, never as dashboard entries.
All writers must use make_unique_id; the unique index arbitrates concurrent writes.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3

from sqlalchemy.engine import make_url
from app.core.config import settings
from app.services.actionable import application_today
from app.services.deduplication import make_unique_id

log = logging.getLogger('scraper')


def maintain_database(path=None):
    path = Path(path or make_url(settings.database_url).database).resolve(strict=True)
    today = application_today().isoformat()
    with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=60) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        groups = defaultdict(list)
        for row in db.execute("SELECT id,unique_id,title,organization,deadline,opportunity_url,"
                              "last_seen,date_scraped,source_website,country,category FROM opportunities WHERE unique_id NOT LIKE 'merged:%'"):
            key = make_unique_id(row['title'], row['organization'], None, row['opportunity_url'], row['source_website'])
            groups[key].append(dict(row))
        # Compare full details only inside matching title/date/country/type buckets.
        # Leave buckets with multiple distinct notices from one publisher alone:
        # their IDs may describe different lots, even when their titles agree.
        from app.services.cross_source_duplicates import match_key, same_cross_source, norm
        buckets = defaultdict(list)
        preferred = {}
        for key, rows in groups.items():
            representative = next((r for r in rows if r['unique_id'] == key),
                                  max(rows, key=lambda r: (r['last_seen'] or r['date_scraped'] or '', r['id'])))
            preferred[key] = representative['id']
            bucket = match_key(representative)
            if bucket:
                buckets[bucket].append((key, representative))
        cross_groups = 0
        for bucket in buckets.values():
            sources = [norm(r['source_website']) for _, r in bucket]
            if len(bucket) < 2 or len(set(sources)) != len(sources):
                continue
            survivors = []
            for key, r in sorted(bucket, key=lambda pair: pair[1]['id']):
                full = dict(db.execute('SELECT * FROM opportunities WHERE id=?', (r['id'],)).fetchone())
                matches = [k for k, members in survivors
                           if all(same_cross_source(full, member) for member in members)]
                if len(matches) == 1:
                    keeper_key = matches[0]
                    groups[keeper_key].extend(groups.pop(key))
                    next(members for k, members in survivors if k == keeper_key).append(full)
                    cross_groups += 1
                else:
                    survivors.append((key, [full]))
        changed = [(key, rows) for key, rows in groups.items()
                   if len(rows) > 1 or rows[0]['unique_id'] != key]
        stats = {'rekeyed': 0, 'merged': 0, 'expired': 0, 'cross_source_merged': cross_groups}
        if changed:
            folder = path.parent / 'integrity-backups'
            folder.mkdir(exist_ok=True)
            backup = folder / f"before-integrity-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.db"
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as src:
                with sqlite3.connect(backup) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Integrity backup failed')
            stats['backup'] = str(backup)
            # Free all old keys first, including keys that another survivor needs.
            for _, rows in changed:
                db.executemany("UPDATE opportunities SET unique_id=? WHERE id=?",
                               [(f"migration:{r['id']}", r['id']) for r in rows])
            for key, rows in changed:
                # Keep a stable canonical ID if it exists, preserving links to it.
                keeper = next(r for r in rows if r['id'] == preferred[key])
                db.execute('UPDATE opportunities SET unique_id=? WHERE id=?', (key, keeper['id']))
                stats['rekeyed'] += 1
                if len(rows) == 1:
                    continue
                full = [dict(db.execute('SELECT * FROM opportunities WHERE id=?', (r['id'],)).fetchone())
                        for r in rows]
                # Carry human decisions with their attribution and timestamps.
                approved = [r for r in full if r.get('approved')]
                if approved:
                    best = max(approved, key=lambda r: r.get('approved_at') or '')
                    db.execute('UPDATE opportunities SET approved=1,approved_by=?,approved_at=? WHERE id=?',
                               (best.get('approved_by'), best.get('approved_at'), keeper['id']))
                human = [r for r in full if r.get('verticals_source') == 'human']
                if human:
                    best = max(human, key=lambda r: r.get('verticals_labeled_at') or '')
                    cols = [c for c in best if c.startswith('vertical') or c.startswith('classification_') or c == 'classified_at']
                    db.execute('UPDATE opportunities SET ' + ','.join(f'{c}=?' for c in cols) + ' WHERE id=?',
                               [best[c] for c in cols] + [keeper['id']])
                for other in rows:
                    if other['id'] == keeper['id']:
                        continue
                    db.execute("UPDATE opportunities SET unique_id=?,status='Expired' WHERE id=?",
                               (f"merged:{other['id']}:{keeper['id']}", other['id']))
                    # Retain old links and history; duplicate sent entries are not new mail.
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sent_log'").fetchone():
                        db.execute('INSERT OR IGNORE INTO sent_log(member_id,opportunity_id,sent_at) '
                                   'SELECT member_id,?,sent_at FROM sent_log WHERE opportunity_id=?',
                                   (keeper['id'], other['id']))
                    stats['merged'] += 1
        stats['expired'] = db.execute("UPDATE opportunities SET status='Expired' WHERE status='Active' "
                                      "AND (deadline < ? OR unique_id LIKE 'merged:%')", (today,)).rowcount
        db.commit()
    log.info('Data integrity: %s', json.dumps(stats))
    return stats
