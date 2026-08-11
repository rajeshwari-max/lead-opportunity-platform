"""Add the BD team leads named in the sources spreadsheet.

    python scripts/seed_team.py            # preview
    python scripts/seed_team.py --apply

Names and emails are read from scripts/team.txt, which is gitignored — this
repository is public. The spreadsheet also carries account passwords beside
them; those belong in a password manager, never in this repo or this database.

Everyone is created as Manual rather than Auto on purpose: with no keywords set
a member matches every opportunity, and switching six people to Auto at once
would send six digests of many thousands of rows on the next 09:00 run. Set
their keywords first, then flip them to Auto in the dashboard.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.db import session_scope          # noqa: E402
from app.database.models import TeamMember         # noqa: E402

# Read from an untracked file rather than hard-coded here: this repository is
# public, and a committed roster publishes colleagues' work addresses to anyone
# who browses it. Create backend/scripts/team.txt (gitignored) with one
# "Name, email" per line:
#
#     Raghu, raghu@example.org
#     Nitin, nitin@example.org
#
ROSTER = Path(__file__).with_name("team.txt")


def load_leads() -> list[tuple[str, str]]:
    try:
        lines = ROSTER.read_text(encoding="utf-8").splitlines()
    except OSError:
        print(f"No roster found. Create {ROSTER} with one 'Name, email' per line.")
        return []
    leads = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, email = line.partition(",")
        if email.strip():
            leads.append((name.strip(), email.strip()))
    return leads


def main() -> int:
    apply = "--apply" in sys.argv
    LEADS = load_leads()
    if not LEADS:
        return 1
    with session_scope() as db:
        have = {m.email.lower() for m in db.query(TeamMember).all()}
        new = [(n, e) for n, e in LEADS if e.lower() not in have]
        print(f"already present: {len(LEADS) - len(new)}   to add: {len(new)}")
        for n, e in new:
            print(f"  + {n:12} {e}")
        if not apply:
            print("\n(preview — re-run with --apply to add them)")
            return 0
        for n, e in new:
            db.add(TeamMember(name=n, email=e, keywords="", categories="",
                              verticals="", auto_send=False, active=True))
    print(f"\nAdded {len(new)}. They are set to Manual — give them keywords in the "
          "dashboard, then switch to Auto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
