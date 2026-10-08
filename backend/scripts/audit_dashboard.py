"""Read-only, all-source dashboard audit; runnable on EC2 using Python stdin."""
import json
import re
import sqlite3
from collections import defaultdict
from collections import Counter
from urllib.parse import urlsplit
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlalchemy.dialects import sqlite
from app.core.config import settings
from app.services.actionable import application_today, strict_actionable_clause
from app.services.deduplication import make_unique_id


def main():
    path = Path(make_url(settings.database_url).database).resolve(strict=True)
    clause = str(strict_actionable_clause().compile(
        dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        live = {r[0] for r in db.execute('SELECT id FROM opportunities WHERE ' + clause)}
        groups, titles = defaultdict(list), defaultdict(list)
        mismatches = 0
        for row in db.execute("SELECT id,unique_id,title,organization,source_website,opportunity_url,deadline,country "
                              "FROM opportunities WHERE unique_id NOT LIKE 'merged:%'"):
            r = dict(row)
            key = make_unique_id(r['title'], r['organization'], None, r['opportunity_url'], r['source_website'])
            mismatches += key != r['unique_id']
            groups[key].append(r)
            if r['id'] in live:
                title = re.sub(r'\W+', ' ', r['title'] or '').strip().casefold()
                if len(title) >= 25:
                    titles[(title, r['deadline'], (r['country'] or '').casefold())].append(r)
        duplicates = [g for g in groups.values() if len(g) > 1]
        live_duplicates = [[r for r in g if r['id'] in live] for g in duplicates]
        live_duplicates = [g for g in live_duplicates if len(g) > 1]
        possible = [g for g in titles.values() if len(g) > 1]
        aliases = defaultdict(list)
        for g in possible:
            for r in g:
                u = urlsplit(r['opportunity_url'] or '')
                if (u.hostname or '').lower() in {'www2.fundsforngos.org', 'www.fundsforngos.org'}:
                    aliases[(u.hostname, u.path.rstrip('/').split('/')[-1], r['deadline'])].append(r['id'])
        alias_groups = [v for v in aliases.values() if len(v) > 1]
        out = {
            'database': str(path), 'india_date': str(application_today()),
            'scope': 'All sources; live view before optional language, vertical or approval filters',
            'live_rows': len(live), 'canonical_duplicate_groups_all_history': len(duplicates),
            'canonical_extra_rows_all_history': sum(len(g)-1 for g in duplicates),
            'canonical_duplicate_groups_live': len(live_duplicates),
            'canonical_extra_rows_live': sum(len(g)-1 for g in live_duplicates),
            'stored_identity_mismatches': mismatches,
            'passed_deadline_but_status_active': db.execute(
                "SELECT count(*) FROM opportunities WHERE status='Active' AND deadline < ?",
                (str(application_today()),)).fetchone()[0],
            'passed_deadline_visible': db.execute('SELECT count(*) FROM opportunities WHERE (' + clause +
                ') AND deadline < ?', (str(application_today()),)).fetchone()[0],
            'superseded_rows_status_active': db.execute(
                "SELECT count(*) FROM opportunities WHERE unique_id LIKE 'merged:%' AND status='Active'").fetchone()[0],
            'possible_same_title_date_country_groups_live': len(possible),
            'possible_groups_by_source_combination': dict(Counter(' + '.join(sorted({r['source_website'] for r in g})) for g in possible)),
            'fundsforngos_same_slug_date_alias_groups': len(alias_groups),
            'fundsforngos_same_slug_date_extra_rows': sum(len(g)-1 for g in alias_groups),
            'canonical_live_samples': sorted(live_duplicates, key=len, reverse=True)[:10],
            'possible_samples_not_confirmed_duplicates': sorted(possible, key=len, reverse=True)[:10],
        }
        print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()
