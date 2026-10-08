"""Why can't I sign in? — a read-only report on the accounts in this database.

Nothing here writes. It exists because a failed sign-in has several very
different causes that all look identical in the browser, and guessing between
them wastes an afternoon:

  * the backend is not running          -> the browser shows a JSON parse error,
                                           because the Vite proxy answers with a
                                           proxy error page, not JSON
  * no credential row for your address  -> HTTP 401 "Incorrect email or password"
  * a credential exists but has never   -> HTTP 401, same message, because
    had a password set (invited, never     verify_password("", "") is false
    activated)
  * the TeamMember row is inactive      -> HTTP 401, same message again
  * the invitation expired              -> the /#setup= link says so, but only
                                           after you have found the link

`POST /api/login` deliberately returns one message for all of those, so an
attacker cannot use the response to discover which addresses exist. That is
correct for the endpoint and useless for the owner of the machine, which is
what this script is for.

It never prints a password hash, a salt, or an invitation token. The hash is
reported only as present/absent, because that is the whole question.

Usage:
    python scripts/login_status.py
    python scripts/login_status.py --email you@catalysts.org
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# `app` is imported inside main(), not here. Importing the models at module
# scope would mean this script could not be loaded at all on a checkout whose
# models.py predates WorkspaceCredential — and the one moment you most want a
# sign-in diagnostic is when something about the accounts code is wrong.
# It also lets the tests import verdict() without touching the ORM.


def verdict(member, cred) -> str:
    """What would POST /api/login actually do with this pair?

    Mirrors the checks in app/api/accounts.py::login, in the same order, so the
    answer here and the answer the endpoint gives cannot drift apart silently.
    """
    if cred is None or not cred.password_hash:
        if cred is not None and cred.invitation_hash:
            expires = cred.invitation_expires
            if expires and expires < datetime.utcnow():
                return "INVITED, LINK EXPIRED — ask for a new invitation"
            return f"INVITED, NOT YET ACTIVATED — open the /#setup= link (expires {expires})"
        return "CANNOT SIGN IN — no password has ever been set for this address"
    if member is None:
        return "CANNOT SIGN IN — credential exists but there is no team member row"
    if not member.active:
        return "CANNOT SIGN IN — the team member is marked inactive"
    return "CAN SIGN IN" + (" (administrator)" if cred.is_admin else " (user)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="", help="report on one address only")
    args = parser.parse_args()

    from sqlalchemy import select

    from app.core.config import settings
    from app.database.db import SessionLocal, init_db
    from app.database.models import TeamMember, WorkspaceCredential

    init_db()
    wanted = args.email.strip().lower()

    print(f"database      : {settings.database_url}")
    print(f"personal_login: {settings.personal_login}"
          + ("" if settings.personal_login
             else "   <- sign-in is DISABLED; every request is treated as admin"))
    print(f"read_only     : {settings.read_only}")
    print(f"cors_origins  : {', '.join(settings.cors_origins)}")
    print()

    with SessionLocal() as db:
        members = {m.email.lower(): m for m in db.scalars(
            select(TeamMember).order_by(TeamMember.name)).all()}
        creds = {c.owner.lower(): c for c in db.scalars(
            select(WorkspaceCredential)).all()}

        addresses = sorted(set(members) | set(creds))
        if wanted:
            addresses = [a for a in addresses if a == wanted]
            if not addresses:
                print(f"{wanted}: NOT FOUND — neither a team member nor a credential.")
                print("Nothing in this database would let that address sign in.")
                return 1

        if not addresses:
            print("This database has no team members and no credentials at all.")
            print("Create the first administrator:")
            print("    python scripts/bootstrap_account.py "
                  '--email you@catalysts.org --name "Your Name"')
            return 1

        width = max(len(a) for a in addresses)
        for address in addresses:
            member, cred = members.get(address), creds.get(address)
            print(f"{address:<{width}}  {verdict(member, cred)}")

        admins = [a for a, c in creds.items() if c.is_admin and c.password_hash]
        print()
        if admins:
            print(f"administrators with a working password: {', '.join(sorted(admins))}")
        else:
            print("NO ADMINISTRATOR HAS A PASSWORD SET.")
            print("bootstrap_account.py is the only way in from here — it refuses to")
            print("run once one admin exists, so it is safe to leave in the repo:")
            print("    python scripts/bootstrap_account.py "
                  '--email you@catalysts.org --name "Your Name"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
