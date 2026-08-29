"""Read-only baseline snapshot of the opportunities database.

Phase 0 of the reliability work. Runs SELECTs only — it creates no tables,
updates nothing and deletes nothing. Run it with the backend STOPPED so SQLite
can fold in the write-ahead log cleanly.

    cd E:\\lead-opportunity-platform\\backend
    .venv\\Scripts\\activate
    python scripts\\db_baseline.py

Writes backend/data/baseline_report.txt. That directory is gitignored, so the
report never lands in a commit. Nothing it prints is a credential: it reports
counts, source names, run statuses and error text — no cookies, passwords,
session blobs or profile paths.

Why a script and not a handful of ad-hoc queries: the numbers in it are the
"before" column of the before/after report, and they have to be reproducible
after the fixes land. Same script, same queries, run twice.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "opportunities.db"
OUT = DB.parent / "baseline_report.txt"

lines: list[str] = []


def say(text: str = "") -> None:
    lines.append(text)
    print(text)


def cols(cx: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in cx.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def scalar(cx: sqlite3.Connection, label: str, sql: str, *args) -> None:
    """One number, or an explicit reason it could not be produced.

    Every query is guarded because the point of a baseline is to survive a
    schema that does not match expectations — reporting 'column missing' is a
    finding, not a crash.
    """
    try:
        value = cx.execute(sql, args).fetchone()[0]
        say(f"  {label:<38} {value:>10,}")
    except sqlite3.Error as exc:
        say(f"  {label:<38} {'n/a':>10}   ({exc})")


def table(cx: sqlite3.Connection, title: str, sql: str, headers: tuple[str, ...]) -> None:
    say()
    say(title)
    say("-" * len(title))
    try:
        rows = cx.execute(sql).fetchall()
    except sqlite3.Error as exc:
        say(f"  unavailable: {exc}")
        return
    if not rows:
        say("  (no rows)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    say("  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    say("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        say("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


def main() -> int:
    if not DB.exists():
        print(f"No database at {DB}", file=sys.stderr)
        return 1

    # Opened read-write on purpose: a WAL-mode database opened read-only cannot
    # replay its write-ahead log, so a 37 MB WAL of recent scrapes would be
    # invisible and every count below would be quietly stale. Only SELECTs are
    # issued. Run with the backend stopped.
    cx = sqlite3.connect(str(DB))
    cx.execute("PRAGMA query_only = ON")     # belt and braces: writes now fail loudly

    say("=" * 78)
    say(f"BASELINE  {DB}")
    say(f"generated {date.today().isoformat()}   size {DB.stat().st_size / 1e6:.0f} MB")
    say("=" * 78)

    opp = cols(cx, "opportunities")
    run = cols(cx, "scrape_runs")
    say()
    say(f"opportunities columns : {', '.join(sorted(opp)) or '(table missing)'}")
    say()
    say(f"scrape_runs columns   : {', '.join(sorted(run)) or '(table missing)'}")

    # ---------------------------------------------------------------- volumes
    say()
    say("VOLUMES")
    say("-------")
    scalar(cx, "opportunities (all)", "SELECT COUNT(*) FROM opportunities")
    scalar(cx, "scrape_runs (all)", "SELECT COUNT(*) FROM scrape_runs")
    scalar(cx, "distinct source_website (opps)",
           "SELECT COUNT(DISTINCT source_website) FROM opportunities")
    scalar(cx, "distinct source_website (runs)",
           "SELECT COUNT(DISTINCT source_website) FROM scrape_runs")

    # ------------------------------------------------- acceptance-test numbers
    say()
    say("ACCEPTANCE METRICS  (the 'before' column)")
    say("-----------------------------------------")
    if "status" in opp and "deadline" in opp:
        scalar(cx, "ACTIVE with deadline < today",
               "SELECT COUNT(*) FROM opportunities "
               "WHERE UPPER(status)='ACTIVE' AND deadline IS NOT NULL "
               "AND date(deadline) < date('now')")
        scalar(cx, "ACTIVE with deadline >= today",
               "SELECT COUNT(*) FROM opportunities "
               "WHERE UPPER(status)='ACTIVE' AND deadline IS NOT NULL "
               "AND date(deadline) >= date('now')")
        scalar(cx, "ACTIVE with NULL deadline",
               "SELECT COUNT(*) FROM opportunities "
               "WHERE UPPER(status)='ACTIVE' AND deadline IS NULL")
        # A placeholder date that parses perfectly and expires in the year 9999.
        scalar(cx, "deadline sentinel 9999/0001/3000",
               "SELECT COUNT(*) FROM opportunities WHERE deadline IS NOT NULL "
               "AND (deadline LIKE '9999-%' OR deadline LIKE '0001-%' "
               "OR deadline LIKE '3000-%')")
    if "verticals" in opp:
        scalar(cx, "ACTIVE with no vertical",
               "SELECT COUNT(*) FROM opportunities WHERE UPPER(status)='ACTIVE' "
               "AND (verticals IS NULL OR TRIM(verticals)='')")
        scalar(cx, "ACTIVE with >=1 vertical",
               "SELECT COUNT(*) FROM opportunities WHERE UPPER(status)='ACTIVE' "
               "AND verticals IS NOT NULL AND TRIM(verticals)<>''")
    if "status" in run:
        scalar(cx, "runs status='running'",
               "SELECT COUNT(*) FROM scrape_runs WHERE status='running'")
        # running + finished_at means the source raised before it could set a
        # terminal status; running + NULL means the process disappeared. They
        # need different recovery rules, so they are counted apart.
        scalar(cx, "  ...of those, finished_at SET",
               "SELECT COUNT(*) FROM scrape_runs WHERE status='running' "
               "AND finished_at IS NOT NULL")
        scalar(cx, "  ...of those, finished_at NULL",
               "SELECT COUNT(*) FROM scrape_runs WHERE status='running' "
               "AND finished_at IS NULL")

    # --------------------------------------------------------- status spreads
    if "status" in opp:
        table(cx, "OPPORTUNITIES BY STATUS",
              "SELECT status, COUNT(*) FROM opportunities "
              "GROUP BY status ORDER BY 2 DESC",
              ("status", "rows"))
    if "category" in opp:
        table(cx, "OPPORTUNITIES BY CATEGORY",
              "SELECT category, COUNT(*) FROM opportunities "
              "GROUP BY category ORDER BY 2 DESC",
              ("category", "rows"))
    if "verticals" in opp and "status" in opp:
        table(cx, "ACTIVE BY VERTICAL STRING (raw, multi-label unsplit)",
              "SELECT COALESCE(NULLIF(TRIM(verticals),''),'(none)'), COUNT(*) "
              "FROM opportunities WHERE UPPER(status)='ACTIVE' "
              "GROUP BY 1 ORDER BY 2 DESC LIMIT 25",
              ("verticals", "rows"))

    # ------------------------------------------------------- coverage matrix
    # The heart of it: per source, when it last ran, when it last produced
    # anything, and how many of its runs ended in each state. A source whose
    # last non-zero run is months old but whose last run "succeeded" is exactly
    # the failure the outcome taxonomy exists to surface.
    if run >= {"source_website", "started_at", "status"}:
        has_saved = "saved" in run
        saved_bits = (
            ", MAX(CASE WHEN saved > 0 THEN started_at END) AS last_nonzero"
            ", SUM(COALESCE(saved,0)) AS saved_total"
            ", SUM(COALESCE(found,0)) AS found_total"
            if has_saved else ", '' AS last_nonzero, '' AS saved_total, '' AS found_total"
        )
        table(
            cx, "SOURCE COVERAGE MATRIX",
            "SELECT source_website AS source"
            ", COUNT(*) AS runs"
            ", MAX(started_at) AS last_run"
            f"{saved_bits}"
            ", SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS stuck"
            ", SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed"
            " FROM scrape_runs GROUP BY source_website"
            " ORDER BY last_nonzero IS NULL DESC, last_nonzero ASC",
            ("source", "runs", "last_run", "last_nonzero",
             "saved", "found", "stuck", "failed"),
        )
        table(cx, "RUN OUTCOMES (all history)",
              "SELECT status, COUNT(*) FROM scrape_runs GROUP BY status ORDER BY 2 DESC",
              ("status", "runs"))

    # Sources that exist in the registry but have never produced a row are the
    # ones the health audit must not report as healthy.
    if "source_website" in opp and "source_website" in run:
        table(cx, "SOURCES WITH RUNS BUT ZERO STORED OPPORTUNITIES",
              "SELECT r.source_website, COUNT(*) AS runs, MAX(r.started_at) AS last_run "
              "FROM scrape_runs r WHERE r.source_website NOT IN "
              "(SELECT DISTINCT source_website FROM opportunities) "
              "GROUP BY r.source_website ORDER BY runs DESC",
              ("source", "runs", "last_run"))

    say()
    say("=" * 78)
    say("END OF BASELINE — no rows were modified")
    say("=" * 78)

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWritten: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
