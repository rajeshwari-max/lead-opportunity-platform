# Wrike task integration — from-scratch implementation guide

Status: instructions only, verified against the current checkout on 21 September 2026. No application code was changed. This guide implements the [work plan](WRIKE_INTEGRATION_WORKPLAN.md). Treat file paths below as **intended edits**, not existing Wrike code.

## 0. Understand the existing app and decide the record model

The user-facing list is `frontend/src/components/UserDashboard.tsx`. Clicking a row opens its brief in the right rail; that brief already has **Track in my workspace**, an original-source link, and **Approve opportunity**. This is the natural placement for a new Wrike action. `frontend/src/components/OpportunitiesTable.tsx` is a separate admin view; adding a button only to the user dashboard will not add it there. `frontend/src/App.tsx` chooses which view renders. The frontend calls `/api` through `frontend/src/lib/api.ts`; Vite proxies `/api` to FastAPI in development.

The canonical opportunity is `Opportunity` in `backend/app/database/models.py`. `TeamMember` represents platform colleagues. `ApplicationJourney` represents a private person's tracking record. This guide chooses **one Wrike task per `Opportunity.id` globally**. Create a separate `WrikeTaskLink` table keyed uniquely by `opportunity_id`; do **not** put the Wrike fields on `Opportunity` unless you intentionally want every scrape/list response to carry them. A separate table also preserves the link if opportunity schemas change. If the business wants each colleague to create a separate Wrike task for their own journey, change the unique key and API to the private journey model instead of mixing the two meanings.

Before coding, confirm whether any approval gate is required. Existing `approved` means human sign-off for other downstream behavior; this guide does not assume Wrike creation requires approval. If it should, enforce it **server-side**.

## 1. Set up Wrike manually

1. In Wrike, check that your employee or service account can manually create a task in the target folder/project. Check *Profile → Apps & Integrations → API* for app creation. If unavailable, ask an account admin to enable the API-app developer permission; full account admin is not inherently required.
2. Create an API app and register an HTTPS callback such as `https://your-dashboard.example/api/wrike/oauth/callback` (a localhost callback is for development only). Record client ID and secret in a password manager, not this repository.
3. Select a folder/project and copy its permalink. The API needs its **folder ID**, which you can obtain using `GET /api/v4/folders` or look up with the folder permalink filter. Save both ID and permalink in configuration. A browser permalink is not the same as an API folder ID.
4. Use the OAuth authorization-code flow and request `wsReadWrite`. Wrike's token response includes `host`; build API URLs with `https://{host}/api/v4`, not a hard-coded `www.wrike.com`. Store refreshed tokens durably because Wrike rotates refresh tokens.

Relevant Wrike calls: [OAuth](https://developers.wrike.com/docs/oauth-20-authorization), [Get Folders](https://developers.wrike.com/reference/getfoldersempty), [Create Task](https://developers.wrike.com/reference/postfolderssingletasks). Do not build against `GET /access_roles`; it does not create a task.

## 2. Add backend-only configuration and token storage

Edit `backend/app/core/config.py` (`Settings` uses `LOP_` environment names) and `backend/.env.example`. Add example names, without values, for `LOP_WRIKE_CLIENT_ID`, `LOP_WRIKE_CLIENT_SECRET`, `LOP_WRIKE_REDIRECT_URI`, `LOP_WRIKE_FOLDER_ID`, `LOP_WRIKE_FOLDER_PERMALINK`, `LOP_WRIKE_TOKEN_ENCRYPTION_KEY`, and `LOP_WRIKE_ENABLED=false`. Put real values only in untracked `backend/.env` or a production secret store. Make the feature default **off** until setup is complete; never put these into Vite `VITE_*` variables. Validate Wrike `host` against a short allowlist of actual Wrike domains/hosts returned by the token flow before using it in outbound requests (SSRF defense).

Edit `backend/app/database/models.py`: add `WrikeConnection` (one service connection; encrypted refresh token, access token if retained, expiry, host, last error/status) and `WrikeTaskLink` (`opportunity_id` unique, task ID, permalink, folder ID, creator email, assignee IDs snapshot, status `creating|created|uncertain|failed`, timestamps). Add `WrikeAssigneeMap` only if you need persistent mapping overrides; otherwise resolve active `TeamMember.email` to Wrike contacts and cache verified IDs. Token encryption at rest needs a maintained crypto library; if selected, add it to `backend/requirements.txt`. A permanent token can remove refresh logic for an isolated prototype, but must still be server-only and revocable; prefer OAuth for production.

Edit `backend/app/database/db.py` only if an existing table needs new columns. New tables imported into `Base.metadata` before `init_db()` are created by `create_all()`. Follow this repo's explicit `_run_migrations()` pattern for modifications to existing tables. Back up the SQLite database before first deployment; do not rely on `create_all()` to alter a table. Avoid storing an unencrypted token in the database or logs.

## 3. Build a small Wrike client service

Create `backend/app/services/wrike_service.py`. Put all external Wrike HTTP calls here, using the already-installed `httpx` package. It should have:

- OAuth authorization URL builder with cryptographically random `state` and `wsReadWrite` scope.
- Authorization-code exchange and refresh-token rotation; lock/serialize refresh so concurrent requests cannot reuse an invalidated refresh token.
- A bounded-timeout request method that sends `Authorization: Bearer ...`, checks the saved Wrike host, handles 401/403/429/5xx deliberately, and never logs credentials or lead-sensitive descriptions.
- Folder lookup/verification, contact lookup with `emails`, task creation, and optional task lookup for reconciliation.
- A formatter that builds a short task title and description from trusted `Opportunity` fields. Include a stable marker such as `Platform opportunity ID: 12345` and the original source link. Sanitize/limit free text before sending it out.

Wrike's create endpoint is `POST /folders/{folderId}/tasks`. `title` is required; `description`, `responsibles`, `dates`, and `importance` are optional. `responsibles` must contain **Wrike contact/user IDs**, not platform member IDs or email strings. Inspect Wrike's live OpenAPI/Try-It schema while implementing the exact serialization of array/object parameters (including `responsibles` and `dates`); the reference lists them as parameters but its web rendering does not provide a reliable copy-paste request body. Start with title + description + `responsibles`, confirm in a disposable Wrike folder, then add dates. Do not automatically set a task due date equal to the opportunity's submission deadline without product approval; the employee's work may need an earlier date.

## 4. Add platform endpoints and their authorization rules

Create `backend/app/api/wrike.py` and register its router in `backend/app/main.py` **before** the static frontend mount. Use `/api/wrike` as the prefix. Use Pydantic request/response models in that route module or create `backend/app/schemas/wrike.py` if the shapes grow. Do not put Wrike business logic into `backend/app/api/routes.py` or the scraper.

Suggested local endpoints:

| Platform endpoint | Purpose and access |
| --- | --- |
| `GET /api/wrike/status` | Authenticated user sees enabled/connected state and configured folder name/link, no secrets. |
| `GET /api/wrike/assignees` | Authenticated user sees active platform members with verified Wrike mappings/availability. |
| `GET /api/wrike/opportunities/{id}` | Authenticated user sees whether this opportunity already has a Wrike task, and its task permalink. |
| `POST /api/wrike/opportunities/{id}/tasks` | Signed-in active member on writable primary creates a task with selected platform member IDs; returns task ID/link. |
| `POST /api/wrike/admin/connect` and `GET /api/wrike/oauth/callback` | Platform admin only; initiate/complete OAuth with state verification. |
| `GET /api/wrike/admin/folders` and `POST /api/wrike/admin/assignees/sync` | Admin-only setup/sync; optional if folder is configured outside the UI. |

Reuse the **identity and origin checks** already in `backend/app/api/workspace.py` (`account()`), or extract a shared dependency. Do not rely on the top-level auth middleware alone: it does not check team-member status or enforce read-only mode. The new POST must explicitly reject `settings.read_only`, invalid Origin/CSRF, anonymous users, and the synthetic `local-development` identity returned when `auth_required()` is false. Setup endpoints must additionally verify `is_admin`. The OAuth callback must validate a short-lived server-stored `state` bound to the initiating admin/session, and it must not expose a token in the redirected URL. Top-level same-site cookie behavior and proxy/HTTPS redirect configuration should be tested end-to-end.

For task creation: load `Opportunity` by ID on the server; reject nonexistent or merged records; load/validate active selected `TeamMember` rows; resolve their Wrike contact IDs; do **not** trust frontend-provided title, folder ID, Wrike IDs, or task link. Validate a maximum assignee count and request length. Return 409 if an existing task is already linked. The server should compose the final Wrike payload from stored opportunity data.

## 5. Map assignees correctly

The platform already has `TeamMember` records with `name`, `email`, `active` in `backend/app/database/models.py`. The Wrike `GET /contacts` endpoint supports an `emails` filter and active/person filtering. Normalize email for matching, but verify the exact returned email/profile and require a single active person match. A missing or ambiguous match is a visible setup error, not a reason to assign another person silently. If an individual isn't a Wrike user, they cannot be assigned by this mechanism until invited/activated in Wrike.

For a small team, resolve on demand and cache successful IDs briefly. For a larger team, run an admin sync and persist `(platform_member_id, wrike_contact_id, verified_email, synced_at)`; resync on email changes or Wrike errors. The assignee dropdown should offer only `TeamMember.active` users with a verified Wrike mapping, and the backend must recheck it. An employee can be an assignee even if another service user created the task, subject to Wrike permissions/sharing. Test this in the target folder; a permalink alone does not grant access.

Source: [Wrike Query Contacts](https://developers.wrike.com/reference/getcontactsempty).

## 6. Prevent duplicates and handle uncertain results

Put a unique constraint on `WrikeTaskLink.opportunity_id`. On create, reserve a `creating` row in a short database transaction before calling Wrike; concurrent requests then see the reserved record and do not send a second POST. Never hold the SQLite write transaction open during the external HTTP call. On confirmed success, store Wrike task ID and permalink and mark `created`. On a definite validation/permission error, mark `failed` with a safe error summary. On a timeout or lost response, mark `uncertain` and **do not automatically retry the POST**: Wrike may already have created the task. Reconcile by searching the configured folder for the stable opportunity marker or by manual admin review, then attach the found task ID. After reconciliation, an admin may explicitly clear a failed reservation and retry. Define recovery for a process that dies while the row is `creating` (age threshold → `uncertain`).

If you simplify this for a prototype, still save the returned Wrike ID and disable repeat-clicking. The unique reservation/reconciliation design is the production-safe path.

## 7. Wire the browser UI

Edit `frontend/src/lib/api.ts` to add platform-only methods for `wrikeStatus`, `wrikeAssignees`, `wrikeTaskForOpportunity`, and `createWrikeTask`. Use the existing `/api` base and same-origin cookie session. Edit `frontend/src/lib/types.ts` with response types. The browser must never call `developers.wrike.com` or Wrike API directly and never see a Wrike token.

Edit `frontend/src/components/UserDashboard.tsx` to put an assignee selector and **Create task in Wrike** button beside the existing brief actions. When selection changes, load that opportunity's link status; show **Open task in Wrike** if already created, otherwise a disabled/create button according to connection, assignee and read-only state. While saving, disable the button and show progress. Render precise server errors (not secrets). Open the returned permalink in a new tab only after confirmed success, preferably via an explicit link to avoid popup blockers. Use `target="_blank" rel="noopener noreferrer"` for trusted Wrike URLs; validate the returned URL host on the backend. Update `frontend/src/components/user-dashboard.css` for responsive selector/button/error styling and accessibility.

If administrators should also be able to create from their table, edit `frontend/src/components/OpportunitiesTable.tsx` as a second UI entry point using the same API. `frontend/src/App.tsx` normally needs **no change** for the ordinary-user action because it already renders `UserDashboard`; change it only if you add a dedicated Wrike setup screen or need to pass new top-level state. `frontend/src/components/PersonalWorkspace.tsx` needs no change under the global-one-task design; it would need changes for a per-journey design.

## 8. Test, deploy and verify

Create `backend/tests/test_wrike_integration.py` using the project's disposable SQLite/FastAPI `TestClient` pattern from `backend/tests/test_personal_workspace.py`. Mock `httpx`/Wrike; never use a real token in automated tests. Cover admin setup, state mismatch, token refresh and rotation, inactive users, missing/ambiguous contact mapping, read-only mode, local auth-disabled mode, duplicate/concurrent clicks, Wrike 401/403/429/5xx, timeout/uncertain reconciliation, and correct description/assignee/folder. Verify no token appears in JSON or logs.

Run from `backend`: `python -m unittest tests.test_wrike_integration` and then the existing relevant auth/workspace tests; run `npm run build` from `frontend`. Use a staging Wrike folder and a disposable opportunity for one real end-to-end check. Confirm the task appears in the intended folder, each assignee can open it, and a second click opens the same task rather than creating another. Do not perform that live-create test against a production folder without approval.

Deploy backend settings/schema/router and frontend together. The app's primary server and read-only mirror have separate behavior: the mirror should show the existing Wrike link if its database snapshot contains one, but all create/connect/sync actions must remain blocked there. Update `docs/RUNBOOK.md` with setup, token rotation, reauthorization, error recovery and who owns the Wrike connection. Back up the database before migration; the repo documents the snapshot flow in `docs/single-login-setup.md` and server deployment in `docs/RUNBOOK.md`.

## Exact file checklist

| File | Action |
| --- | --- |
| `backend/app/core/config.py` | Add Wrike settings/feature flag. |
| `backend/.env.example` | Document empty `LOP_WRIKE_*` names; never add secrets. |
| `backend/app/database/models.py` | Add connection/link and optional assignee-map models. |
| `backend/app/database/db.py` | Only as needed for migration of existing tables; new models must be registered before `create_all`. |
| `backend/requirements.txt` | Only if choosing a new token-encryption library; `httpx` already exists. |
| `backend/app/services/wrike_service.py` | **New:** OAuth, refresh, folder/contact/task HTTP calls, payload and error handling. |
| `backend/app/api/wrike.py` | **New:** platform Wrike routes, validation and authorization. |
| `backend/app/schemas/wrike.py` | Optional **new** separate request/response models. |
| `backend/app/main.py` | Register new `/api/wrike` router before static mount. |
| `backend/tests/test_wrike_integration.py` | **New:** mocked integration/security/idempotency tests. |
| `frontend/src/lib/api.ts` | Add calls to your backend `/api/wrike/*`, never direct Wrike calls. |
| `frontend/src/lib/types.ts` | Add status, assignee and task-link types. |
| `frontend/src/components/UserDashboard.tsx` | Add primary UI action in selected-opportunity brief. |
| `frontend/src/components/user-dashboard.css` | Style selector, status and button. |
| `frontend/src/components/OpportunitiesTable.tsx` | Optional: equivalent admin-table action. |
| `frontend/src/App.tsx` | Optional: only for a setup screen or new top-level state. |
| `docs/RUNBOOK.md` | Update operations, secrets, reconnection and error recovery after implementation. |

## Official API references

- [OAuth authorization and refresh](https://developers.wrike.com/docs/oauth-20-authorization)
- [Create Task (Folder)](https://developers.wrike.com/reference/postfolderssingletasks)
- [Query Contacts](https://developers.wrike.com/reference/getcontactsempty)
- [Get Folders](https://developers.wrike.com/reference/getfoldersempty)
- [Update Task](https://developers.wrike.com/reference/puttaskssingle) — optional future sync
- [Wrike API app setup and permissions](https://help.wrike.com/hc/en-us/articles/210409445-Wrike-API)
