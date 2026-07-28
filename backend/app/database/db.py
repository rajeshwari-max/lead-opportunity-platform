"""Engine/session management + SQLite FTS5 full-text index."""
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.database.models import Base

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # FastAPI threadpool + scraper thread
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")  # concurrent readers during scraping
    except Exception:
        pass  # WAL unsupported on some network filesystems — fall back silently
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

_FTS_STATEMENTS: list[str] = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS opportunities_fts USING fts5(
        title, organization, summary, vertical, location, eligibility,
        content='opportunities', content_rowid='id')""",
    """CREATE TRIGGER IF NOT EXISTS opp_ai AFTER INSERT ON opportunities BEGIN
        INSERT INTO opportunities_fts(rowid, title, organization, summary, vertical, location, eligibility)
        VALUES (new.id, new.title, new.organization, new.summary, new.vertical, new.location, new.eligibility);
    END""",
    """CREATE TRIGGER IF NOT EXISTS opp_ad AFTER DELETE ON opportunities BEGIN
        INSERT INTO opportunities_fts(opportunities_fts, rowid, title, organization, summary, vertical, location, eligibility)
        VALUES ('delete', old.id, old.title, old.organization, old.summary, old.vertical, old.location, old.eligibility);
    END""",
    """CREATE TRIGGER IF NOT EXISTS opp_au AFTER UPDATE ON opportunities BEGIN
        INSERT INTO opportunities_fts(opportunities_fts, rowid, title, organization, summary, vertical, location, eligibility)
        VALUES ('delete', old.id, old.title, old.organization, old.summary, old.vertical, old.location, old.eligibility);
        INSERT INTO opportunities_fts(rowid, title, organization, summary, vertical, location, eligibility)
        VALUES (new.id, new.title, new.organization, new.summary, new.vertical, new.location, new.eligibility);
    END""",
]


def init_db() -> None:
    """Create tables and the FTS5 full-text search index."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _run_migrations(conn)
        for stmt in _FTS_STATEMENTS:
            conn.execute(text(stmt))
        _ensure_fts_populated(conn)


def _ensure_fts_populated(conn) -> None:
    """Self-healing check, independent of whether a rename just ran this call.

    External-content FTS5 tables aren't auto-populated from the source table
    on CREATE, and only the docsize/idx shadow tables reveal whether the
    index actually has content — a plain `SELECT count(*) FROM
    opportunities_fts` always mirrors the source table row count regardless
    of whether 'rebuild' ever ran, since non-MATCH reads proxy straight to
    the content table. Relying on "did we just rename a column this call" to
    decide whether to rebuild is fragile (e.g. a dev server reloading mid-edit
    can miss the window entirely) — so just check the real signal directly.
    """
    try:
        indexed = conn.exec_driver_sql(
            "SELECT count(*) FROM opportunities_fts_docsize"
        ).scalar_one()
    except Exception:
        indexed = 0
    total = conn.exec_driver_sql("SELECT count(*) FROM opportunities").scalar_one()
    if indexed != total:
        conn.exec_driver_sql("INSERT INTO opportunities_fts(opportunities_fts) VALUES('rebuild')")


def _run_migrations(conn) -> None:
    """Lightweight SQLite migrations (create_all can't ALTER existing tables).

    Safe to run on every startup — each step checks before applying. Whether
    the FTS5 index needs rebuilding afterward is checked independently in
    _ensure_fts_populated (not returned from here — see its docstring).
    """
    def columns(table: str) -> set[str]:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}

    def index_names(table: str) -> set[str]:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA index_list({table})")}

    renamed_vertical_column = False

    if "opportunities" in _tables(conn):
        cols = columns("opportunities")
        # sector -> vertical, sectors -> verticals: rename in place (RENAME COLUMN,
        # SQLite 3.25+) so existing scraped data isn't dropped. Falls back to an
        # additive empty column only for brand-new databases that never had the
        # old names at all.
        if "sector" in cols and "vertical" not in cols:
            conn.exec_driver_sql("ALTER TABLE opportunities RENAME COLUMN sector TO vertical")
            renamed_vertical_column = True
        elif "vertical" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN vertical VARCHAR(256) NOT NULL DEFAULT ''"
            )
        cols = columns("opportunities")
        if "sectors" in cols and "verticals" not in cols:
            conn.exec_driver_sql("ALTER TABLE opportunities RENAME COLUMN sectors TO verticals")
        elif "verticals" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN verticals VARCHAR(256) NOT NULL DEFAULT ''"
            )

    if "team_members" in _tables(conn):
        cols = columns("team_members")
        if "sectors" in cols and "verticals" not in cols:
            conn.exec_driver_sql("ALTER TABLE team_members RENAME COLUMN sectors TO verticals")
        elif "verticals" not in cols:
            conn.exec_driver_sql("ALTER TABLE team_members ADD COLUMN verticals TEXT NOT NULL DEFAULT ''")

    if "ix_opportunities_sectors" in index_names("opportunities"):
        conn.exec_driver_sql("DROP INDEX ix_opportunities_sectors")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_opportunities_verticals ON opportunities(verticals)"
    )

    # The FTS5 index and its triggers are keyed to the old `sector` column name
    # and content-linked to `opportunities`; when the rename above just happened,
    # drop them so _FTS_STATEMENTS (run right after this function returns)
    # recreates them against the new `vertical` column name. No data loss —
    # FTS5 with content='opportunities' is a derived index, and
    # _ensure_fts_populated repopulates it afterward regardless.
    if renamed_vertical_column:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS opp_ai")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS opp_ad")
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS opp_au")
        conn.exec_driver_sql("DROP TABLE IF EXISTS opportunities_fts")


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for background jobs (scraper, scheduler)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
