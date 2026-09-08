"""Conservative cross-publisher checks, shared by scraping and imports."""
import re
import unicodedata
import hashlib
from html import unescape
from collections import defaultdict


def norm(value):
    value = getattr(value, 'value', value)
    return re.sub(r'\W+', ' ', unicodedata.normalize('NFKC', str(value or ''))).strip().casefold()


def match_key(row):
    title = norm(row.get('title'))
    country = norm(row.get('country'))
    category = norm(row.get('category'))
    deadline = str(row.get('deadline') or '')[:10]
    if (len(title) < 35 or len(title.split()) < 5 or not deadline or
            country in {'', 'unknown', 'not listed'} or category in {'', 'other'}):
        return None
    return title, deadline, country, category


def organisation(value):
    value = norm(value)
    return '' if value in {'', 'government', 'npo', 'ngo', 'other', 'not listed', 'unknown'} else value


def detail(value):
    plain = unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', plain)).strip().casefold()


def evidence(row):
    org, summary = organisation(row.get('organization')), detail(row.get('summary'))
    if not org or len(summary) < 80 or len(summary.split()) < 15:
        return None
    # Compact signatures keep imports bounded even with long descriptions.
    return (org,) + tuple(hashlib.sha256(text.encode('utf-8')).digest()
                          for text in (summary, detail(row.get('eligibility')),
                                       detail(row.get('funding_amount'))))


def same_cross_source(a, b):
    key = match_key(a)
    source_a, source_b = norm(a.get('source_website')), norm(b.get('source_website'))
    if not key or key != match_key(b) or not source_a or not source_b or source_a == source_b:
        return False
    # Missing detail is not evidence of equality. Only presentation differences
    # (HTML, whitespace and case) are ignored; currency/number signs are kept.
    proof = evidence(a)
    return proof is not None and proof == evidence(b)


class CrossSourceIndex:
    def __init__(self, rows=()):
        self.rows = defaultdict(list)
        for row in rows:
            self.add(row)

    def add(self, row):
        key = match_key(row)
        proof = evidence(row) if key else None
        if proof:
            self.rows[key].append((norm(row.get('source_website')), proof))

    def contains(self, row):
        source, proof = norm(row.get('source_website')), evidence(row)
        return bool(source and proof) and any(other_source and source != other_source and proof == other_proof
                   for other_source, other_proof in self.rows.get(match_key(row), ()))


def already_present(db, candidate):
    """The deadline index bounds the lookup; autoflush includes this batch's inserts."""
    if match_key(candidate) is None:
        return False
    from sqlalchemy import select
    from app.database.models import Opportunity as O
    rows = db.execute(select(O.title, O.deadline, O.country, O.category,
                             O.organization, O.source_website, O.summary,
                             O.eligibility, O.funding_amount).where(
        O.deadline == candidate['deadline'], O.unique_id.not_like('merged:%'))).mappings()
    return any(same_cross_source(candidate, row) for row in rows)
