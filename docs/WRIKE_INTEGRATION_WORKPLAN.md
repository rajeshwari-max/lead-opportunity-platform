# Wrike integration — verified work plan

Status: planning only. No Wrike integration code or credentials are present in this deliverable. Verified against the current project checkout and Wrike documentation on 21 September 2026. See [the implementation guide](WRIKE_INTEGRATION_FROM_SCRATCH.md) for file-by-file instructions.

## Outcome and scope

On the opportunity dashboard, an authenticated user selects an opportunity, chooses one or more eligible colleagues, and clicks **Create task in Wrike**. The server creates one task in a configured Wrike folder, records the Wrike ID and permalink, and replaces the create action with **Open task in Wrike**. No task is created merely by opening a Wrike folder link. Initial release is one-way: changes made later in Wrike do not flow back to this platform.

The recommended first-release ownership rule is **one Wrike task per canonical opportunity across the whole platform**, not one per person. This prevents two users creating duplicates from the same listing. If the actual requirement is one task per person's application, change the uniqueness key to `(owner, opportunity_id)` and attach it to `ApplicationJourney` instead; make this decision before migration.

## What is already in this project

- React/Vite frontend. The ordinary-user opportunity brief and its action buttons are in `frontend/src/components/UserDashboard.tsx`; the admin table has its own brief in `frontend/src/components/OpportunitiesTable.tsx`.
- Browser calls go through `frontend/src/lib/api.ts` to `/api`. Shared opportunity shapes live in `frontend/src/lib/types.ts`.
- FastAPI registers its routers in `backend/app/main.py`. The main opportunities API lives in `backend/app/api/routes.py`; the personal-workspace API and its stricter signed-in/active-team-member guard live in `backend/app/api/workspace.py`.
- `Opportunity` and `TeamMember` are in `backend/app/database/models.py`; SQLite startup schema/migrations are in `backend/app/database/db.py`.
- The app has a `LOP_READ_ONLY` mirror. Wrike writes must be refused there. Authentication can be disabled locally, in which case `current_user()` reports a synthetic Local user; **do not let that synthetic user create Wrike tasks**.
- `httpx` is already in `backend/requirements.txt`. No new HTTP client is needed.

## Decisions to approve before building

| Decision | Recommended first release |
| --- | --- |
| Connected Wrike identity | Dedicated technical/service user with access to the target folder; employee account is acceptable for development. |
| Authentication | OAuth authorization-code flow with `wsReadWrite`; keep client secret and refresh tokens on the backend. A permanent token is acceptable only for a narrow prototype. |
| Destination | One administrator-configured Wrike folder ID and saved folder permalink. |
| Task owner/cardinality | One task per canonical opportunity. |
| Assignee choice | Active `TeamMember` rows that resolve by email to active Wrike person contacts; unmatched users are shown as unavailable, never silently substituted. |
| Missing assignee | Block creation and explain which mapping is missing; optionally allow explicitly unassigned tasks in a later release. |
| Opening Wrike | On confirmed success, show **Open task in Wrike**; do not force navigation before the user sees the result. |
| Scheduling | First release may omit Wrike due dates; a listing deadline is not automatically a staff task due date. If added, require an explicit date and validate it. |
| Who may create | Signed-in, active platform member on writable primary; separate admin-only setup/connect endpoints. |

## Delivery phases and acceptance gates

1. **Wrike account and access.** Confirm API-app permission, `wsReadWrite` authorization, and task-create access in the intended folder. Deliverable: test folder ID, no secrets in Git/chat. Gate: a manually created test task works with the connected identity.
2. **Secure connection.** Add backend-only OAuth settings, connect/callback, state validation, durable encrypted token storage and refresh. Gate: reconnect and expiry are handled without user-facing token disclosure.
3. **Folder and people mapping.** Select a folder, query contacts, map platform users to Wrike contact IDs by normalized email, handle ambiguity/inactive users. Gate: correct folder and assignees visible in a dry-run preview.
4. **Task creation and deduplication.** Validate opportunity, permissions, payload; create through Wrike; persist the external link. Gate: one click makes one task, retries do not silently make a second.
5. **Dashboard UX.** Add assignee selector, create/progress/error/open states in ordinary-user brief; decide whether admin table needs the same feature. Gate: keyboard and mobile use are clear, errors are actionable.
6. **Tests and rollout.** Mock Wrike in backend tests; run frontend build, backend tests, staging manual test, deploy backend and frontend together. Gate: read-only mirror and unauthenticated requests cannot create tasks; existing scraping, login and personal-workspace flows still work.

## Minimum external API surface

| Wrike call | Use |
| --- | --- |
| OAuth authorize and token/refresh | Connect the service user and maintain access. |
| `GET /api/v4/folders` or `GET /api/v4/folders/{folderIds}` | Find/verify the destination folder. Setup-time, not every click. |
| `GET /api/v4/contacts` with email filtering | Resolve platform assignees to Wrike user IDs. Setup/sync-time; validate before create. |
| `POST /api/v4/folders/{folderId}/tasks` | Create the task, including `title`, optional `description`, and `responsibles`. |
| `GET /api/v4/tasks/{taskIds}` | Optional reconciliation after an uncertain timeout or to check an existing task. |

`GET /api/v4/access_roles` is **not required** for this workflow. The integration identity's real folder permissions still apply. Webhooks and `PUT /tasks/{taskId}` are phase-two features, not prerequisites.

## Risks and controls

- **Duplicate creation:** use a database uniqueness constraint and a reserved `creating` state before the network call. Do not auto-retry a timed-out POST unless the outcome is reconciled. Include a stable platform reference in the Wrike description/metadata to help recovery.
- **Secret leakage:** never return tokens to the frontend, log them, put them in URLs, or commit `backend/.env`. Use HTTPS and a server-side encryption key for persisted refresh tokens.
- **Wrong assignee:** match by verified email, reject duplicate/ambiguous matches, and use Wrike IDs in `responsibles`; do not confuse platform `TeamMember.id` with Wrike contact ID.
- **Wrong environment:** read-only mirror and local auth-disabled mode must reject external writes. Production folder ID and OAuth redirect URL must match the production Wrike app/environment.
- **Partial failure:** if Wrike created a task but the local save failed, show an uncertain status and reconcile. A failure message must never invite blind repeat-clicking.

## Official references

- [Wrike OAuth authorization](https://developers.wrike.com/docs/oauth-20-authorization)
- [Create Task (Folder)](https://developers.wrike.com/reference/postfolderssingletasks)
- [Query Contacts](https://developers.wrike.com/reference/getcontactsempty)
- [Get Folders](https://developers.wrike.com/reference/getfoldersempty)
- [Wrike API setup and tokens](https://help.wrike.com/hc/en-us/articles/210409445-Wrike-API)
