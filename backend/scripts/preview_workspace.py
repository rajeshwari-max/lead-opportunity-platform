"""Disposable local workspace demo. Does not import main or modify the real DB.

Run from backend: .venv/Scripts/python.exe scripts/preview_workspace.py
Build frontend first; open http://127.0.0.1:5194/?view=workspace
"""
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn

from app.api.workspace import router
from app.core.config import settings
from app.database.db import get_db
from app.database.models import Base, Opportunity, ApplicationJourney, JourneyEvent, WorkspaceProfile, WorkspaceContact
from app.services.actionable import application_today


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--port', type=int, default=5194)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="workspace-preview-") as tmp:
        engine = create_engine('sqlite:///' + str(Path(tmp) / 'preview.db'), connect_args={'check_same_thread':False})
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        settings.personal_login = args.login
        # Disposable preview must never send real account emails using local .env.
        settings.smtp_user = ''
        settings.smtp_password = ''
        settings.dashboard_password = ''
        settings.admin_password = ''
        settings.read_only = False
        with sessions() as db:
            owner = 'preview@example.org' if args.login else 'local-development'
            if args.login:
                from app.database.models import WorkspaceCredential, TeamMember
                from app.core.auth import hash_password
                db.add(TeamMember(email=owner, name='Preview user', auto_send=False))
                db.add(WorkspaceCredential(owner=owner, password_hash=hash_password('Preview-only-pass-2026'), is_admin=True))
            db.add(WorkspaceProfile(owner=owner, title='CMS opportunity journey', keywords='health, climate', accent='blue'))
            db.add(WorkspaceContact(owner=owner, name='Demo relationship contact', organization='Example Foundation', role='Programme adviser', tags='health'))
            for index, (title, stage) in enumerate([('Community health innovation programme — demo', 'Preparing'), ('Climate resilience partnership — demo', 'Applied'), ('Inclusive livelihoods research grant — demo', 'Accepted')]):
                o = Opportunity(unique_id=f'demo-{index}', title=title, source_website='Preview fixture', organization='Example Foundation', deadline=application_today()+timedelta(days=15+index), summary='Demonstration data for the personal workspace. This is not a live funding call.', country='India')
                db.add(o); db.flush()
                j = ApplicationJourney(owner=owner, opportunity_id=o.id, stage=stage, next_action='Prepare proposal outline' if index==0 else '')
                db.add(j); db.flush()
                db.add(JourneyEvent(journey_id=j.id, stage=stage, note='Demo application'))
            db.commit()
        def session():
            with sessions() as db:
                yield db
        app = FastAPI()
        app.dependency_overrides[get_db] = session
        app.include_router(router, prefix='/api')
        from app.api.accounts import router as accounts_router
        app.include_router(accounts_router, prefix='/api')
        from app.api.leads import router as leads_router
        app.include_router(leads_router, prefix='/api')
        @app.get('/api/config')
        def config(request: Request, db=Depends(session)):
            from app.core.auth import current_user, COOKIE_NAME
            return dict(**current_user(request.cookies.get(COOKIE_NAME), db), read_only=False, auth_required=args.login)
        app.mount('/', StaticFiles(directory=Path(__file__).resolve().parents[2] / 'frontend' / 'dist', html=True))
        try:
            uvicorn.run(app, host='127.0.0.1', port=args.port)
        finally:
            engine.dispose()


if __name__ == '__main__':
    main()
