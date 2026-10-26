"""Operational catalogs, with explicit reviews and an append-only history."""
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy import select
from app.database.models import DirectoryEntry, DirectoryEvent, DirectorySeed


def entry_key(kind, name, scope=""):
    value = scope if kind == "website" and scope else " ".join(name.casefold().split())
    if kind == "keyword":
        value += "|" + scope
    return hashlib.sha256(value.encode()).hexdigest()


def seed_directories(db):
    """Idempotent migration of the existing lists; never overwrite an edit."""
    from app.services.keyword_inventory import VERTICAL_KEYWORDS
    from app.scrapers.registry import SCRAPER_REGISTRY
    existing = {(r.kind, r.key) for r in db.scalars(select(DirectoryEntry)).all()}
    seed_keywords = db.get(DirectorySeed, "keyword") is None
    defaults = [dict(kind="keyword", name=term, scope=vertical)
                for vertical, terms in VERTICAL_KEYWORDS.items() for term in terms] if seed_keywords else []
    defaults += [dict(kind="website", name=cls.display_name, scope=key,
                      url=getattr(cls, "start_url", "") or getattr(cls, "website", "") or "")
                 for key, cls in SCRAPER_REGISTRY.items()]
    for fields in defaults:
        key = entry_key(fields["kind"], fields["name"], fields.get("scope", ""))
        if (fields["kind"], key) in existing:
            continue
        row = DirectoryEntry(**fields, key=key, updated_by="existing-platform-catalog")
        db.add(row)
        existing.add((row.kind, key))
    if seed_keywords:
        db.add(DirectorySeed(kind="keyword"))
    db.commit()
    refresh_keywords(db)


def refresh_keywords(db):
    from app.services import verticals
    terms = {}
    rows = db.scalars(select(DirectoryEntry).where(DirectoryEntry.kind == "keyword")).all()
    if not rows:
        return
    for row in rows:
        if row.status == "active":
            terms.setdefault(row.scope, []).append(row.name)
    verticals.reload_team_keywords(terms)


def blocked_sources(db):
    return {r.scope for r in db.scalars(select(DirectoryEntry).where(
        DirectoryEntry.kind == "website", DirectoryEntry.status.in_(["paused", "dropped"]))) if r.scope}


def audit(db, row, action, actor, note="", payload=None):
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = actor
    db.add(DirectoryEvent(entry_id=row.id, action=action, actor=actor,
                          note=note, payload=json.dumps(payload or {}, default=str)))


def entry_data(row):
    return {column.name: getattr(row, column.name) for column in DirectoryEntry.__table__.columns}
