import sqlite3

from scripts.dedupe_dashboard import plan


def _database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE opportunities (
          id INTEGER PRIMARY KEY, title TEXT, organization TEXT,
          source_website TEXT, deadline TEXT, approved INTEGER,
          verticals_source TEXT, classification_source TEXT, status TEXT,
          opportunity_url TEXT, summary TEXT, last_seen TEXT,
          date_scraped TEXT, pdf_url TEXT, documents_json TEXT
        );
        CREATE TABLE saved_leads (opportunity_id INTEGER);
    """)
    rows = [
        (1, "Same call", "Funder", "Board", "2027-01-01", 0, "auto", "rule",
         "Active", "https://x.test/1", "short", "", "2026-01-01", None, None),
        (2, " Same call ", "FUNDER", "board", "2027-01-01", 0, "auto", "rule",
         "Active", "https://x.test/2", "a much better summary", "", "2026-02-01",
         "https://x.test/tor.pdf", '[{"url":"https://x.test/tor.pdf"}]'),
        # Same title but a different deadline: a distinct call, never removed.
        (3, "Same call", "Funder", "Board", "2028-01-01", 0, "auto", "rule",
         "Active", "https://x.test/3", "future round", "", "2026-03-01", None, None),
        # Protected by a saved lead; it wins over its otherwise better copy.
        (4, "Protected call", "Funder", "Board", "2027-02-01", 0, "auto", "rule",
         "Active", "https://x.test/4", "", "", "2026-01-01", None, None),
        (5, "Protected call", "Funder", "Board", "2027-02-01", 0, "auto", "rule",
         "Active", "https://x.test/5", "better", "", "2026-02-01", None, None),
    ]
    db.executemany("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO saved_leads VALUES (4)")
    db.commit()
    return db


def test_plan_keeps_best_copy_and_never_merges_a_different_deadline(tmp_path):
    with _database(tmp_path / "dedupe.db") as db:
        actions, blocked = plan(db)
    assert blocked == 0
    assert {(row["drop"], row["keep"]) for row in actions} == {(1, 2), (5, 4)}
    assert all(row["drop"] != 3 for row in actions)
