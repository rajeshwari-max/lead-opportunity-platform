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


def _with_system_tables(db):
    db.executescript("""
        CREATE TABLE opportunity_intelligence (opportunity_id INTEGER UNIQUE, score REAL);
        CREATE TABLE sent_log (member_id INTEGER, opportunity_id INTEGER,
                               UNIQUE (member_id, opportunity_id));
        CREATE TABLE experience_events (opportunity_id INTEGER, event_type TEXT);
    """)
    # Every row has a score and was emailed: none of that is a person's work.
    db.executemany("INSERT INTO opportunity_intelligence VALUES (?, 50)",
                   [(i,) for i in (1, 2, 3, 4, 5)])
    db.executemany("INSERT INTO sent_log VALUES (?, ?)", [(7, 1), (7, 2), (8, 1)])
    db.executemany("INSERT INTO experience_events VALUES (?, 'prediction')",
                   [(i,) for i in (1, 2)])
    db.commit()


def test_system_written_rows_do_not_protect_a_duplicate(tmp_path):
    with _database(tmp_path / "dedupe.db") as db:
        _with_system_tables(db)
        actions, blocked = plan(db)
    assert blocked == 0
    assert {(row["drop"], row["keep"]) for row in actions} == {(1, 2), (5, 4)}


def test_a_persons_review_event_still_protects(tmp_path):
    with _database(tmp_path / "dedupe.db") as db:
        _with_system_tables(db)
        db.execute("INSERT INTO experience_events VALUES (1, 'human_feedback')")
        db.commit()
        actions, blocked = plan(db)
    # 1 and 2 are both kept: 1 has a review, so it becomes the keeper.
    assert (2, 1) in {(row["drop"], row["keep"]) for row in actions}
    assert all(row["drop"] != 1 for row in actions)


def test_removing_a_copy_moves_its_email_log_and_drops_its_score(tmp_path):
    from scripts.dedupe_dashboard import _release_links

    with _database(tmp_path / "dedupe.db") as db:
        _with_system_tables(db)
        _release_links(db, drop=1, keep=2)
        db.execute("DELETE FROM opportunities WHERE id=1")
        sent = sorted(tuple(r) for r in db.execute("SELECT member_id, opportunity_id FROM sent_log"))
        scores = {r[0] for r in db.execute("SELECT opportunity_id FROM opportunity_intelligence")}
        events = [tuple(r) for r in db.execute("SELECT * FROM experience_events ORDER BY rowid")]
    # Member 7 already had the kept copy; member 8 is moved to it.
    assert sent == [(7, 2), (8, 2)]
    assert 1 not in scores and 2 in scores
    assert events == [(2, "prediction"), (2, "prediction")]


def test_undated_calls_with_a_reused_title_are_not_merged(tmp_path):
    db = sqlite3.connect(tmp_path / "undated.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE opportunities (
          id INTEGER PRIMARY KEY, title TEXT, organization TEXT,
          source_website TEXT, deadline TEXT, approved INTEGER,
          verticals_source TEXT, classification_source TEXT, status TEXT,
          opportunity_url TEXT, summary TEXT, last_seen TEXT,
          date_scraped TEXT, pdf_url TEXT, documents_json TEXT, country TEXT
        );
    """)
    common = ("Call for proposals for action grants", "EC", "DevelopmentAid", None,
              0, "auto", "rule", "Active")
    db.executemany("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, *common, "https://da.org/grants/view/1", "Support for victims of crime", "", "", None, None, ""),
        (2, *common, "https://da.org/grants/view/2", "Judicial training projects", "", "", None, None, ""),
        # Same text republished under a new link: a real duplicate.
        (3, *common, "https://da.org/grants/view/3", "Judicial training projects", "", "", None, None, ""),
        # Same title, same date, different country: a different call.
        (4, "Arts grant", "Fund", "DevelopmentAid", "2027-01-01", 0, "auto", "rule", "Active",
         "https://da.org/grants/view/4", "", "", "", None, None, "Canada"),
        (5, "Arts grant", "Fund", "DevelopmentAid", "2027-01-01", 0, "auto", "rule", "Active",
         "https://da.org/grants/view/5", "", "", "", None, None, "Kenya"),
    ])
    db.commit()
    actions, _ = plan(db)
    assert {(a["drop"], a["keep"]) for a in actions} in ({(2, 3)}, {(3, 2)})
