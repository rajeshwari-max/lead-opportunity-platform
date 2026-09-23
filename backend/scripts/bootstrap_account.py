"""Create the first personal administrator; run interactively on the server."""
import sys
import argparse
from getpass import getpass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func, text
from app.database.db import init_db, SessionLocal
from app.database.models import TeamMember, WorkspaceCredential
from app.core.auth import hash_password

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    email = args.email.strip().lower()
    if email.count('@') != 1 or any(c.isspace() for c in email):
        raise SystemExit('Invalid email')
    password = getpass('Choose your personal password (12+ characters): ')
    if len(password) < 12 or len(password) > 200 or password != getpass('Confirm password: '):
        raise SystemExit('Passwords must match and be 12–200 characters')
    init_db()
    with SessionLocal() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        if db.scalar(select(WorkspaceCredential.owner).where(WorkspaceCredential.is_admin.is_(True))):
            raise SystemExit('An administrator already exists. Use account management.')
        member = db.scalar(select(TeamMember).where(func.lower(TeamMember.email)==email))
        if member and not member.active:
            raise SystemExit('This team member is inactive')
        if not member:
            db.add(TeamMember(email=email,name=args.name,auto_send=False))
        credential = db.get(WorkspaceCredential,email) or WorkspaceCredential(owner=email)
        credential.password_hash=hash_password(password)
        credential.is_admin=True
        credential.invitation_hash=None
        credential.invitation_expires=None
        db.add(credential);db.commit()
    print('Personal administrator ready. Sign in using your email and chosen password.')

if __name__=='__main__':
    main()
