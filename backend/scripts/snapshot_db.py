"""Create a consistent copy of the local SQLite database for EC2 transfer.

Unlike copying ``opportunities.db`` directly, SQLite's online backup API also
includes committed rows still held in ``opportunities.db-wal``.  The source is
opened read-only and is never modified.

    python scripts/snapshot_db.py --output data/local-scrape-transfer.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


def configured_database_path() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise SystemExit(f"Only SQLite is supported here (got {url})")
    return Path(url.split("///")[-1]).expanduser().resolve()


def snapshot(source: Path, destination: Path, *, only_source: str = "",
             active_only: bool = False) -> None:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    if source == destination:
        raise ValueError("output must be different from the live database")
    if destination.exists():
        raise FileExistsError(
            f"refusing to overwrite existing snapshot: {destination}\n"
            "Remove that explicit transfer file first or choose another name."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    if only_source or active_only:
        keep: list[str] = []
        params: list[str] = []
        if only_source:
            keep.append("lower(source_website) = lower(?)")
            params.append(only_source)
        if active_only:
            keep.append("status = 'Active'")
        with sqlite3.connect(destination) as destination_db:
            destination_db.execute(
                f"DELETE FROM opportunities WHERE NOT ({' AND '.join(keep)})",
                params,
            )
            destination_db.commit()
            destination_db.execute("VACUUM")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True,
                        help="new .db file to create; existing files are refused")
    parser.add_argument("--only-source", default="",
                        help="keep only this exact source_website value")
    parser.add_argument("--active-only", action="store_true",
                        help="keep only Active opportunities")
    args = parser.parse_args()
    source = configured_database_path()
    destination = Path(args.output)
    snapshot(source, destination, only_source=args.only_source,
             active_only=args.active_only)

    with sqlite3.connect(f"file:{destination.resolve().as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT count(*) FROM opportunities").fetchone()[0]
        devaid = db.execute(
            "SELECT count(*) FROM opportunities "
            "WHERE lower(source_website) LIKE '%development%aid%'"
        ).fetchone()[0]
    print(f"source                  : {source}")
    print(f"snapshot                : {destination.resolve()}")
    print(f"opportunities           : {rows:,}")
    print(f"DevelopmentAid records  : {devaid:,}")
    if args.only_source:
        print(f"source filter           : {args.only_source}")
    if args.active_only:
        print("status filter           : Active")
    print("The snapshot is ready to upload; the live database was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
