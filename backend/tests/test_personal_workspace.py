"""Isolation and workflow tests using a disposable database, never live data."""
import tempfile
import sqlite3
from contextlib import closing
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import workspace as w
from app.api import accounts as a
from app.core.auth import COOKIE_NAME, make_session_token, hash_password, password_version
from app.database.db import get_db
from app.database.models import Base, Opportunity, TeamMember, Category, ApplicationJourney, WorkspaceContact, WorkspaceCredential
from app.services.actionable import application_today


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'test.db'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)
        def db():
            with self.sessions() as session:
                yield session
        app = FastAPI()
        app.include_router(w.router, prefix='/api')
        app.include_router(a.router, prefix='/api')
        from app.api.leads import router as leads_router
        app.include_router(leads_router, prefix='/api')
        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        self.patches = [patch('app.database.db.SessionLocal', self.sessions), patch.object(w.settings, 'personal_login', True), patch.object(w.settings, 'dashboard_password', 'shared'), patch.object(w.settings, 'admin_password', 'separate-admin'), patch.object(w.settings, 'read_only', False)]
        for p in self.patches:
            p.start()
        a._attempts.clear()
        with self.sessions() as db:
            db.add_all([TeamMember(name='Alice', email='alice@example.org'), TeamMember(name='Bob', email='bob@example.org')])
            db.add_all([Opportunity(unique_id='one', title='Health grant', source_website='Source', organization='Funder', category=Category.GRANT, deadline=application_today()+timedelta(days=10)),
                        Opportunity(unique_id='expired', title='Expired grant', source_website='Source', deadline=application_today()-timedelta(days=1))])
            db.commit()
        with self.sessions() as db:
            db.add_all([WorkspaceCredential(owner=name+'@example.org', password_hash=hash_password('individual-password-123'), is_admin=name=='alice') for name in ['alice','bob']])
            db.commit()
        self.unlock('alice')

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self, name, admin=False):
        self.client.cookies.clear()
        with self.sessions() as db:
            c = db.get(WorkspaceCredential, name+'@example.org')
            self.client.cookies.set(COOKIE_NAME, make_session_token(name+'@example.org', name, c.is_admin, password_version(c.password_hash)))

    def unlock(self, name):
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/login', json={'email':name+'@example.org', 'password':'individual-password-123'}).status_code,200)

    def track(self):
        r = self.client.post('/api/workspace/journeys', json={'opportunity_id': 1})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['id']

    def test_single_login_and_password_reset_revokes_session(self):
        self.assertEqual(self.client.get('/api/workspace/journeys').status_code,200)
        self.client.cookies.clear()
        self.client.cookies.set(COOKIE_NAME,make_session_token('alice@example.org','Alice',True))
        self.assertEqual(self.client.get('/api/workspace/journeys').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'email':'alice@example.org','password':'shared'}).status_code,401)
        self.unlock('alice')
        with self.sessions() as db:
            db.get(WorkspaceCredential,'alice@example.org').password_hash=hash_password('replacement-password')
            db.commit()
        self.assertEqual(self.client.get('/api/workspace/journeys').status_code,401)

    def test_owner_isolation_including_files_and_contacts(self):
        j = self.track()
        r = self.client.post(f'/api/workspace/journeys/{j}/attachments', content=b'private proposal', headers={'x-filename':'proposal.txt'})
        self.assertEqual(r.status_code, 200, r.text)
        file_id = r.json()['id']
        c = self.client.post('/api/workspace/contacts', json={'name': 'Private contact'}).json()['id']
        self.client.put('/api/workspace/profile', json={'title': 'Alice private space'})
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/workspace/journeys').json(), [])
        self.assertEqual(self.client.get('/api/workspace/contacts').json(), [])
        self.assertNotEqual(self.client.get('/api/workspace/profile').json()['title'], 'Alice private space')
        for method, path, kw in [('get', f'/journeys/{j}/details', {}), ('put', f'/journeys/{j}', {'json': {'stage':'Accepted'}}), ('get', f'/journeys/{j}/attachments/{file_id}', {}), ('delete', f'/journeys/{j}/attachments/{file_id}', {}), ('post', f'/journeys/{j}/attachments', {'content': b'bad'}), ('delete', f'/contacts/{c}', {}), ('put', f'/contacts/{c}', {'json': {'name':'changed'}})]:
            self.assertEqual(getattr(self.client, method)('/api/workspace'+path, **kw).status_code, 404)
        self.assertEqual(self.client.post('/api/workspace/journeys', json={'opportunity_id':1, 'owner':'alice@example.org'}).status_code, 422)

    def test_tracking_idempotency_timeline_and_read_only(self):
        j = self.track()
        self.assertEqual(self.track(), j)
        self.assertEqual(len(self.client.get('/api/workspace/journeys').json()), 1)
        r = self.client.put(f'/api/workspace/journeys/{j}', json={'stage':'Accepted', 'notes':'Relevant experience won', 'factors':['Relevant experience']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.client.get(f'/api/workspace/journeys/{j}/details').json()['events']), 2)
        with patch.object(w.settings, 'read_only', True):
            self.assertEqual(self.client.put(f'/api/workspace/journeys/{j}', json={'stage':'Unsuccessful'}).status_code, 403)
        self.assertEqual(self.client.post('/api/workspace/contacts', json={'name':'x'}, headers={'Origin':'https://evil.example'}).status_code, 403)

    def test_attachment_limits_download_and_delete(self):
        j = self.track()
        self.assertEqual(self.client.post(f'/api/workspace/journeys/{j}/attachments', content=b'x'*(5*1024*1024+1)).status_code, 413)
        r = self.client.post(f'/api/workspace/journeys/{j}/attachments', content=b'proposal', headers={'X-Filename':'../../private.html'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['filename'], 'private.html')
        path = f"/api/workspace/journeys/{j}/attachments/{r.json()['id']}"
        response = self.client.get(path)
        self.assertEqual(response.content, b'proposal')
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(self.client.delete(path).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_active_recommendations_and_history_denominator(self):
        with self.sessions() as db:
            for index, stage in enumerate(['Accepted']*3+['Unsuccessful']*2+['Shortlisted']*4):
                o = Opportunity(unique_id='history'+str(index), title='Past opportunity', source_website='Other', organization='Funder', category=Category.GRANT, deadline=application_today()-timedelta(days=1))
                db.add(o); db.flush()
                db.add(ApplicationJourney(owner='alice@example.org', opportunity_id=o.id, stage=stage))
            db.add(WorkspaceContact(owner='alice@example.org', name='Ally', organization='Funder'))
            db.add(WorkspaceContact(owner='bob@example.org', name='Other user secret', organization='Funder'))
            db.commit()
        result = self.client.get('/api/workspace/recommendations').json()['items']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['history'], {'accepted':3, 'decided':5})
        self.assertEqual([c['name'] for c in result[0]['contacts']], ['Ally'])
        self.unlock('bob')
        result = self.client.get('/api/workspace/recommendations').json()['items'][0]
        self.assertEqual(result['history']['decided'], 0)
        self.assertFalse(any('Your history' in r for r in result['reasons']))

    def test_disabled_member_and_admin_access(self):
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/workspace/admin/summary').status_code, 403)
        self.unlock('alice')
        with self.sessions() as db:
            db.scalar(select(TeamMember).where(TeamMember.email=='alice@example.org')).active = False
            db.commit()
        self.assertEqual(self.client.get('/api/workspace/profile').status_code, 401)

    def test_filtered_transfer_excludes_private_data_but_full_backup_keeps_it(self):
        from scripts.snapshot_db import snapshot
        self.track()
        source = Path(self.temp.name) / 'test.db'
        filtered = Path(self.temp.name) / 'filtered.db'
        full = Path(self.temp.name) / 'full.db'
        snapshot(source, filtered, only_source='Source')
        snapshot(source, full)
        with closing(sqlite3.connect(filtered)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_credentials').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM application_journeys').fetchone()[0], 0)
        with closing(sqlite3.connect(full)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_credentials').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM application_journeys').fetchone()[0], 1)

    def test_invitation_password_choice_and_live_admin_roles(self):
        result=self.client.post('/api/accounts/invite',json={'name':'New user','email':'new@example.org'})
        self.assertEqual(result.status_code,200,result.text)
        token=result.json()['setup_path'].split('=')[1]
        self.client.cookies.clear()
        result=self.client.post('/api/login/activate',json={'token':token,'password':'self-chosen-password'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(result.json()['is_admin'])
        self.assertEqual(self.client.get('/api/workspace/journeys').status_code,200)
        self.assertEqual(self.client.get('/api/accounts').status_code,403)
        self.assertEqual(self.client.post('/api/login/activate',json={'token':token,'password':'different-password'}).status_code,400)
        new_cookie=self.client.cookies.get(COOKIE_NAME)
        self.unlock('alice')
        self.assertEqual(self.client.put('/api/accounts/role',json={'email':'new@example.org','is_admin':True}).status_code,200)
        self.client.cookies.clear();self.client.cookies.set(COOKIE_NAME,new_cookie)
        self.assertEqual(self.client.get('/api/accounts').status_code,200)
        with self.sessions() as db:
            db.get(WorkspaceCredential,'new@example.org').is_admin=False
            db.commit()
        self.assertEqual(self.client.get('/api/accounts').status_code,403)
        self.assertEqual(self.client.post('/api/login',json={'email':'new@example.org','password':'self-chosen-password','is_admin':True}).status_code,422)

    def test_existing_credentials_migrate_without_granting_admin(self):
        from app.database.db import _run_migrations
        with self.engine.begin() as conn:
            for column in ('is_admin','invitation_hash','invitation_expires'):
                conn.exec_driver_sql(f'ALTER TABLE workspace_credentials DROP COLUMN {column}')
            _run_migrations(conn)
            self.assertEqual(conn.exec_driver_sql('SELECT count(*) FROM workspace_credentials').scalar(),2)
            self.assertEqual(conn.exec_driver_sql('SELECT count(*) FROM workspace_credentials WHERE is_admin = 1').scalar(),0)

    def test_vite_origin_allowed_and_untrusted_origin_rejected(self):
        self.client.cookies.clear()
        body={'email':'alice@example.org','password':'individual-password-123'}
        self.assertEqual(self.client.post('/api/login', json=body, headers={'Origin':'http://localhost:5173'}).status_code,200)
        self.assertEqual(self.client.post('/api/workspace/contacts', json={'name':'Allowed'},headers={'Origin':'http://localhost:5173'}).status_code,200)
        self.assertEqual(self.client.post('/api/login', json=body, headers={'Origin':'https://untrusted.example'}).status_code,403)

    def test_self_registration_and_password_recovery(self):
        self.client.cookies.clear()
        with patch('app.services.email_service.is_configured', return_value=True), patch.object(a,'send_account_link') as send, patch.object(w.settings,'allowed_email_domains',''):
            response=self.client.post('/api/login/register',json={'email':'new@example.org','name':'New person','password':'new-person-password'})
            self.assertEqual(response.status_code,200,response.text)
            send.assert_not_called()
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.json()['is_admin'])
            send.reset_mock()
            self.assertEqual(self.client.post('/api/login/register',json={'email':'new@example.org','name':'Overwritten','password':'replacement-pass-123'}).status_code,409)
            send.assert_not_called()
            self.assertEqual(self.client.post('/api/login',json={'email':'new@example.org','password':'new-person-password'}).status_code,200)
            cookie=self.client.cookies.get(COOKIE_NAME)
            known=self.client.post('/api/login/forgot-password',json={'email':'new@example.org'})
            reset=send.call_args.args[1]
            unknown=self.client.post('/api/login/forgot-password',json={'email':'unknown@example.org'})
            self.assertEqual(known.json(),unknown.json())
            self.assertEqual(self.client.post('/api/login/activate',json={'token':reset,'password':'replacement-pass-123'}).status_code,200)
            self.client.cookies.clear();self.client.cookies.set(COOKIE_NAME,cookie)
            self.assertEqual(self.client.get('/api/workspace/journeys').status_code,401)
            self.assertEqual(self.client.post('/api/login',json={'email':'new@example.org','password':'new-person-password'}).status_code,401)
            self.assertEqual(self.client.post('/api/login',json={'email':'new@example.org','password':'replacement-pass-123'}).status_code,200)

    def test_missing_email_configuration_is_reported(self):
        with patch('app.services.email_service.is_configured',return_value=False):
            r=self.client.post('/api/login/forgot-password',json={'email':'alice@example.org'})
            self.assertEqual(r.status_code,503)
            self.assertIn('not configured',r.json()['detail'])

    def test_dashboard_saved_leads_and_admin_monitoring(self):
        self.unlock('bob')
        r = self.client.post('/api/my-leads', json={'opportunity_id': 1})
        lead = r.json()['id']
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post('/api/my-leads', json={'opportunity_id': 1}).json()['id'], lead)
        data = {'stage':'Applied','notes':'Submitted proposal','next_action':'Follow up','next_action_date':None,'factors':[]}
        self.assertEqual(self.client.put(f'/api/my-leads/{lead}',json=data).status_code,200)
        self.assertEqual(self.client.get('/api/my-leads/team/activity').status_code,403)
        self.assertEqual(self.client.get(f'/api/my-leads/team/history/{lead}').status_code,403)
        self.unlock('alice')
        self.assertEqual(self.client.get('/api/my-leads').json(),[])
        self.assertEqual(self.client.put(f'/api/my-leads/{lead}',json=data).status_code,404)
        rows = self.client.get('/api/my-leads/team/activity').json()
        bob = next(p for p in rows if p['email']=='bob@example.org')
        self.assertEqual(bob['leads'][0]['notes'],'Submitted proposal')
        self.assertEqual(len(self.client.get(f'/api/my-leads/team/history/{lead}').json()),2)
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/my-leads').json()[0]['stage'],'Applied')

    def test_unsave_is_private_and_preserves_progress(self):
        self.unlock('bob')
        lead = self.client.post('/api/my-leads', json={'opportunity_id': 1}).json()['id']
        self.client.put(f'/api/my-leads/{lead}', json={'stage':'Applied','notes':'Keep this note'})
        self.client.post('/api/my-leads/activity', json={'opportunity_id':1,'action':'viewed'})
        self.unlock('alice')
        self.assertEqual(self.client.delete(f'/api/my-leads/{lead}').status_code,404)
        self.unlock('bob')
        with patch.object(w.settings, 'read_only', True):
            self.assertEqual(self.client.delete(f'/api/my-leads/{lead}').status_code,403)
        self.assertEqual(self.client.delete(f'/api/my-leads/{lead}').status_code,200)
        self.assertEqual(self.client.delete(f'/api/my-leads/{lead}').status_code,200)
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/my-leads').json(),[])
        self.assertEqual(self.client.get('/api/my-leads/activity').json()['viewed'],1)
        restored = self.client.post('/api/my-leads',json={'opportunity_id':1}).json()
        self.assertEqual(restored['id'],lead)
        self.assertEqual(restored['notes'],'Keep this note')
        self.assertEqual(restored['stage'],'Applied')
        self.assertEqual(len(self.client.get('/api/my-leads').json()),1)

    def test_external_registration_and_change_password(self):
        with patch('app.services.email_service.is_configured',return_value=True), patch.object(a,'send_account_link') as send, patch.object(w.settings,'allowed_email_domains','catalysts.org'):
            response=self.client.post('/api/login/register',json={'email':'external@gmail.com','name':'External','password':'personal-password-123'})
            self.assertEqual(response.status_code,200)
            send.assert_not_called()
            self.assertFalse(response.json()['is_admin'])
            self.assertEqual(self.client.post('/api/login',json={'email':'external@gmail.com','password':'shared'}).status_code,401)
            old=self.client.cookies.get(COOKIE_NAME)
            self.assertEqual(self.client.post('/api/accounts/password',json={'current_password':'wrong','new_password':'updated-password-123'}).status_code,400)
            self.assertEqual(self.client.post('/api/accounts/password',json={'current_password':'personal-password-123','new_password':'updated-password-123'}).status_code,200)
            self.assertEqual(self.client.get('/api/my-leads').status_code,200)
            self.client.cookies.clear(); self.client.cookies.set(COOKIE_NAME,old)
            self.assertEqual(self.client.get('/api/my-leads').status_code,401)
            self.assertEqual(self.client.post('/api/login',json={'email':'external@gmail.com','password':'updated-password-123'}).status_code,200)

    def test_dashboard_filters_are_private_and_persist(self):
        self.unlock('bob')
        value={'filters':{'search':'health','countries':['India']}}
        self.assertEqual(self.client.put('/api/my-leads/preferences',json=value).status_code,200)
        self.unlock('alice')
        self.assertEqual(self.client.get('/api/my-leads/preferences').json(),{'filters':{}})
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/my-leads/preferences').json(),value)
        self.assertEqual(self.client.put('/api/my-leads/preferences',json={'filters':{'search':'x'*17000}}).status_code,422)

    def test_lead_activity_distinct_counts_restore_and_isolation(self):
        self.unlock('bob')
        for action in ['viewed','viewed','source_opened','reviewed','reviewed']:
            self.assertEqual(self.client.post('/api/my-leads/activity',json={'opportunity_id':1,'action':action}).status_code,200)
        data=self.client.get('/api/my-leads/activity').json()
        self.assertEqual([data[k] for k in ('viewed','reviewed','source_opened')],[1,1,1])
        self.assertEqual(len(data['recent']),1)
        self.assertEqual(self.client.get('/api/my-leads').json(),[])
        self.assertEqual(self.client.post('/api/my-leads/activity',json={'opportunity_id':99999,'action':'viewed'}).status_code,404)
        self.assertEqual(self.client.post('/api/my-leads/activity',json={'opportunity_id':1,'action':'unknown'}).status_code,422)
        self.unlock('alice')
        self.assertEqual(self.client.get('/api/my-leads/activity').json()['viewed'],0)
        bob=next(p for p in self.client.get('/api/my-leads/team/activity').json() if p['email']=='bob@example.org')
        self.assertEqual(bob['activity']['reviewed'],1)
        self.unlock('bob')
        self.assertEqual(self.client.get('/api/my-leads/activity').json()['reviewed'],1)
        with patch.object(w.settings,'read_only',True):
            self.assertEqual(self.client.post('/api/my-leads/activity',json={'opportunity_id':1,'action':'viewed'}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/my-leads/activity').status_code,401)

    def test_registration_without_email_cannot_claim_existing_identity(self):
        with patch('app.services.email_service.is_configured',return_value=False), patch.object(a,'send_account_link') as send:
            self.client.cookies.clear()
            payload={'email':'fresh@gmail.com','name':'Fresh','password':'fresh-password-123'}
            self.assertEqual(self.client.post('/api/login/register',json={**payload,'is_admin':True}).status_code,422)
            r=self.client.post('/api/login/register',json=payload)
            self.assertEqual(r.status_code,200)
            self.assertFalse(r.json()['is_admin'])
            self.assertEqual(self.client.get('/api/my-leads').status_code,200)
            send.assert_not_called()
            self.assertEqual(self.client.post('/api/login/register',json={**payload,'email':'ALICE@example.org'}).status_code,409)
            with self.sessions() as db:
                db.add(TeamMember(email='reserved@example.org',name='Reserved',auto_send=False))
                db.commit()
            self.assertEqual(self.client.post('/api/login/register',json={**payload,'email':'reserved@example.org'}).status_code,409)

    def test_existing_team_member_can_set_first_personal_password(self):
        with self.sessions() as db:
            db.add(TeamMember(email='legacy@example.org',name='Existing colleague',auto_send=False))
            db.commit()
        with patch('app.services.email_service.is_configured',return_value=True), patch.object(a,'send_account_link') as send:
            self.assertEqual(self.client.post('/api/login/forgot-password',json={'email':'legacy@example.org'}).status_code,200)
            token=send.call_args.args[1]
            r=self.client.post('/api/login/activate',json={'token':token,'password':'personal-password-123'})
            self.assertEqual(r.status_code,200)
            self.assertFalse(r.json()['is_admin'])


if __name__ == '__main__':
    unittest.main()
