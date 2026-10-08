# Personal workspaces and application learning

## What is implemented

Both user and admin dashboards have **My workspace**. An opportunity brief (and
expanded admin table row) has **Track in my workspace**. The catalogue is shared;
application journeys, files, contacts and personal preferences belong to one account.

- Stages: Saved, Preparing, Applied, Shortlisted, Accepted, Unsuccessful, Withdrawn.
- Application notes, win/loss factors, next action and date, and a change timeline.
- Proposal/supporting attachments with authenticated download and delete.
- Personal contact records, organisations, expertise tags and relationship notes.
- Saved workspace name, blue/teal/violet theme, interests, countries, CMS verticals,
  experience and registrations.
- Recommendations with explicit preference, relationship and outcome reasons.
- Admin account-password provisioning and aggregate team stage totals. There is
  no API to browse another person's notes or files using an owner parameter.

## Authentication and privacy

Personal login now opens both the dashboard and workspace with one session.
The separate workspace password prompt has been removed. See
[single-login-setup.md](single-login-setup.md) for the current account setup and
mandatory first-admin migration steps. The older shared passwords are no longer
accepted. Users choose a password through an admin-issued invitation. Only an
explicit database role grants admin access. Personal records remain owner-scoped.

## How prioritisation learns

This release is an explainable ranking engine, not a trained AI probability model.
It recomputes ranking when recommendations are requested, using the owner's saved
interests, country/vertical preferences, contacts and recorded outcomes. No daily
training job or external AI API key is required.

Only Accepted and Unsuccessful count as decided outcomes. Shortlisted and pending
applications never inflate the denominator. An organisation-and-opportunity-type
history signal is used only after five decided applications for that combination.
The UI displays counts and reasons, not a promised chance of winning. Interests
and contacts work immediately for a new user. Unknown offering organisations do
not contribute an organisation outcome signal. Contacts are suggested from exact
offering-organisation matches or matching expertise tags, not inferred relationships.

Recommendations rank up to the 1,000 nearest-deadline active opportunities matching
the search and return 40. This is not an exhaustive personalised scan of the whole
catalogue. The standard strict active/deadline predicate excludes expired and merged
records. Historical applications remain in the journey even when their call closes.

Experience, registrations, win/loss notes and factors are captured for review.
They are not yet semantically analysed to establish eligibility or infer causation.
Proposal parsing, an LLM, calibrated prediction, team-wide learning, automatic
outcome collection, relationship enrichment and Wrike are not part of this release.
Users must record their applications and outcomes to build a useful history.

## Stack and storage

React 18 + TypeScript + Vite and responsive CSS on the frontend. Python + FastAPI,
Pydantic and SQLAlchemy on the backend. Existing SQLite on EC2 stores six additive
tables: workspace_credentials, workspace_profiles, application_journeys,
journey_events, journey_attachments and workspace_contacts. Application tracking
has a unique owner/opportunity constraint, making repeated Track clicks idempotent.

Attachments use SQLite BLOB storage: 5 MB each, 10 files per application and 50 MB
per owner. Upload quota checks are serialized on SQLite. Downloads use attachment
disposition and nosniff. Nothing is uploaded to an external AI or storage service.
Full database snapshots include these files and private records. Protect backup
access. Filtered opportunity transfers (--only-source / --active-only) strip all
workspace tables, including credentials, from the transfer copy.

For much larger document volumes, migrate attachments to private object storage
and use a managed database; that is not required by this implementation.

## Validate locally

From Windows PowerShell:

```powershell
cd E:\lead-opportunity-platform\backend
.venv\Scripts\python.exe -m unittest tests.test_personal_workspace
cd ..\frontend
npm run build
```

Disposable demo (synthetic records; never modifies the real database):

```powershell
cd E:\lead-opportunity-platform\backend
.venv\Scripts\python.exe scripts\preview_workspace.py
```

Open http://127.0.0.1:5194/?view=workspace. The demo serves the last frontend build.
Its temporary data disappears when the process stops. Catalogue links are for the
real app and are not implemented by this workspace-only demo.

## Deploy to the existing EC2 installation

Follow [the single-login migration guide](single-login-setup.md). It includes the
first-admin step required before restarting the updated application.
