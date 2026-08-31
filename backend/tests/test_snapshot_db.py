import sqlite3

import pytest

from scripts.snapshot_db import snapshot


def test_snapshot_includes_committed_rows_from_wal(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "transfer.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE rows (value TEXT)")
    writer.execute("INSERT INTO rows VALUES ('new fetch')")
    writer.commit()

    snapshot(source, destination)

    with sqlite3.connect(destination) as copied:
        assert copied.execute("SELECT value FROM rows").fetchone()[0] == "new fetch"
    writer.close()


def test_snapshot_refuses_to_overwrite(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "transfer.db"
    sqlite3.connect(source).close()
    destination.write_text("keep me", encoding="utf-8")

    with pytest.raises(FileExistsError):
        snapshot(source, destination)

    assert destination.read_text(encoding="utf-8") == "keep me"


def test_snapshot_can_keep_only_active_rows_from_one_source(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "transfer.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE opportunities "
                   "(source_website TEXT, status TEXT, title TEXT)")
        db.executemany("INSERT INTO opportunities VALUES (?, ?, ?)", [
            ("DevelopmentAid", "Active", "keep"),
            ("DevelopmentAid", "Expired", "closed"),
            ("World Bank", "Active", "other source"),
        ])

    snapshot(source, destination, only_source="DevelopmentAid", active_only=True)

    with sqlite3.connect(destination) as copied:
        assert copied.execute(
            "SELECT source_website, status, title FROM opportunities"
        ).fetchall() == [("DevelopmentAid", "Active", "keep")]
