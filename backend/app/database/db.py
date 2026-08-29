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
    # Large scrapes commit once per listing page — DevelopmentAid alone can be
    # ~1,600 commits in a run — while the dashboard keeps polling. These make
    # that workload safe and fast:
    #   busy_timeout  wait for a lock instead of failing with "database is
    #                 locked" when a read and a commit collide (the single most
    #                 likely failure mode during a long scrape)
    #   synchronous=NORMAL  skip an fsync per commit; with WAL this is still
    #                 crash-safe (only a power loss can lose the last commits)
    #   cache_size    ~64 MB page cache, so dedup lookups stay in memory
    for pragma in (
        "PRAGMA busy_timeout=30000",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-64000",
        "PRAGMA temp_store=MEMORY",
    ):
        try:
            cur.execute(pragma)
        except Exception:
            pass
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
    # Models defined outside database/models.py must be imported before
    # create_all, or their tables are silently never created.
    from app.services import reminder_service  # noqa: F401  (registers ReminderLog)

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
        # Research vs Implementation routing. Additive: existing rows get an
        # empty value and are filled in by the startup backfill.
        if "work_type" not in columns("opportunities"):
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN work_type VARCHAR(32) NOT NULL DEFAULT ''"
            )
        if "study_type" not in columns("opportunities"):
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN study_type VARCHAR(32) NOT NULL DEFAULT ''"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_opportunities_study_type "
                "ON opportunities(study_type)"
            )
        # Approval flag. Additive with a false default, so every existing row
        # starts unapproved — approval is an explicit human act and must never
        # be granted retroactively by a migration.
        cols = columns("opportunities")
        if "approved" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN approved BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_opportunities_approved "
                "ON opportunities(approved)"
            )
        if "approved_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE opportunities ADD COLUMN approved_at DATETIME")
        if "approved_by" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE opportunities ADD COLUMN approved_by VARCHAR(320) NOT NULL DEFAULT ''"
            )
        # When a scrape last saw this listing on its source. Backfilled from
        # date_scraped, which is the only evidence existing rows carry — setting
        # them all to "now" instead would hand every stale Ongoing row a fresh
        # lease on the day this ships, which is the opposite of the point.
        if "last_seen" not in cols:
            conn.exec_driver_sql("ALTER TABLE opportunities ADD COLUMN last_seen DATETIME")
            conn.exec_driver_sql("UPDATE opportunities SET last_seen = date_scraped")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_opportunities_last_seen "
                "ON opportunities(last_seen)"
            )

    if "team_members" in _tables(conn):
        cols = columns("team_members")
        if "sectors" in cols and "verticals" not in cols:
            conn.exec_driver_sql("ALTER TABLE team_members RENAME COLUMN sectors TO verticals")
        elif "verticals" not in cols:
            conn.exec_driver_sql("ALTER TABLE team_members ADD COLUMN verticals TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------ deadline states
    # `deadline IS NULL` meant both "the source says rolling" (actionable) and
    # "we could not parse a date" (not actionable), for 3,021 Active rows.
    #
    # The backfill below is deliberately conservative and deliberately marked.
    # The original signal — the scraper's assume_active flag — was never stored,
    # so which of the two a legacy NULL row was CANNOT be recovered. Marking
    # them all UNKNOWN would hide 3,021 rows that are currently visible, on a
    # guess. So they become ROLLING with confidence='legacy_assumed', which
    # preserves today's behaviour exactly and leaves the assumption visible and
    # queryable for a later re-check. Dated rows get DATED/'parsed'.
    if "opportunities" in _tables(conn):
        opp_cols = columns("opportunities")
        for name, ddl in (
            ("deadline_state",      "VARCHAR(16)"),
            ("deadline_raw",        "VARCHAR(256)"),
            ("deadline_confidence", "VARCHAR(24)"),
            ("deadline_convention", "VARCHAR(16)"),
            ("deadline_checked_at", "DATETIME"),
        ):
            if name not in opp_cols:
                conn.exec_driver_sql(f"ALTER TABLE opportunities ADD COLUMN {name} {ddl}")

        # Only rows that have never been classified — so this is a one-time
        # backfill that a later re-check can override, not a repeated overwrite
        # of whatever the scrapers have since determined.
        conn.exec_driver_sql(
            "UPDATE opportunities "
            "   SET deadline_state = 'dated', deadline_confidence = 'parsed' "
            " WHERE deadline_state IS NULL AND deadline IS NOT NULL"
        )
        conn.exec_driver_sql(
            "UPDATE opportunities "
            "   SET deadline_state = 'rolling', deadline_confidence = 'legacy_assumed' "
            " WHERE deadline_state IS NULL AND deadline IS NULL"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_opp_actionable "
            "ON opportunities(status, deadline_state, deadline)"
        )

    # ---------------------------------------------------- scrape-run evidence
    # A run used to be able to say only "completed", and 792 of the 916 runs in
    # the 2026-08-29 baseline said exactly that — including all 127 attempts by
    # the 16 sources that have never fetched a page or saved a row. Nothing
    # recorded a status code, a URL or an error, so "the site blocked us" and
    # "our parser is broken" were indistinguishable rows.
    #
    # Every column here is additive and nullable (or defaulted), so this runs on
    # the existing 177 MB database without rewriting a single row: SQLite's ADD
    # COLUMN is O(1) metadata when there is no NOT NULL without a default.
    # Historical runs stay exactly as they are — evidence nobody captured cannot
    # be back-filled, and pretending otherwise would put invented causes on 916
    # rows.
    if "scrape_runs" in _tables(conn):
        run_cols = columns("scrape_runs")
        for name, ddl in (
            # identity: the registry key, which survives a display-name rename.
            # 91 distinct names for 85 sources in the baseline, because renames
            # split each source's history ("Macfound"/"Macarthur Foundation").
            ("source_key",          "VARCHAR(64)"),
            # the taxonomy verdict, alongside the original `status`
            ("outcome",             "VARCHAR(32)"),
            ("error_code",          "VARCHAR(48)"),
            ("error_message",       "TEXT"),
            # transport — the half of the picture that was entirely missing
            ("first_http_status",   "INTEGER"),
            ("last_http_status",    "INTEGER"),
            ("final_url",           "VARCHAR(1024)"),
            ("fetch_mode",          "VARCHAR(16)"),
            ("attempts",            "INTEGER NOT NULL DEFAULT 0"),
            ("duration_s",          "FLOAT"),
            # counts that separate "healthy and repeating itself" from
            # "the gate threw everything away"
            ("duplicates",          "INTEGER NOT NULL DEFAULT 0"),
            ("rejected",            "INTEGER NOT NULL DEFAULT 0"),
            # liveness: the lease that tells a live run from an abandoned one.
            # 106 runs sit in `running` with no way to express which is which.
            ("heartbeat_at",        "DATETIME"),
            ("worker_id",           "VARCHAR(64)"),
            # parser drift
            ("structure_signature", "VARCHAR(64)"),
            ("debug_capture",       "VARCHAR(512)"),
        ):
            if name not in run_cols:
                conn.exec_driver_sql(f"ALTER TABLE scrape_runs ADD COLUMN {name} {ddl}")

        # Indexes for the three questions the health view asks constantly:
        # "what is this source's current state", "which runs are abandoned",
        # "when did this source last produce anything".
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scrape_runs_source_key "
            "ON scrape_runs(source_key, started_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scrape_runs_outcome ON scrape_runs(outcome)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_scrape_runs_heartbeat "
            "ON scrape_runs(heartbeat_at) WHERE finished_at IS NULL"
        )

    # The cross-process scrape lease. create_all() makes the table; this seeds
    # the single row it will ever hold, so acquire() never has to decide whether
    # it is inserting or updating under a race.
    if "scrape_lease" in _tables(conn):
        conn.exec_driver_sql(
            "INSERT OR IGNORE INTO scrape_lease (id, worker_id, acquired_at, "
            "heartbeat_at, label) VALUES (1, NULL, NULL, NULL, '')"
        )

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
