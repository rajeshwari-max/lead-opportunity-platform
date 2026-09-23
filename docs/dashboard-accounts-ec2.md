# Deploy personal accounts and saved leads (without the separate workspace)

This release adds self-service registration without email verification, personal passwords,
password recovery/change, saved leads and progress inside the dashboard, account
filter preferences and admin-only team activity. Common-password login is removed.
Existing shared-password users must register (if their address is new) or use Forgot password to establish
their own password. Existing personal passwords remain valid.

The separate workspace UI is not imported into this frontend build. Its API is
disabled unless LOP_WORKSPACE_ENABLED=true; keep it false on EC2. The saved-lead
feature reuses the existing journey storage/helpers without exposing workspace
documents, recommendations or network screens. No local database is uploaded.

## 1. On Windows: publish the application changes

Run in PowerShell. Review staged paths before committing; do not include .env,
databases, documents or unrelated changes. If Git reports an error, stop.

```powershell
cd E:\lead-opportunity-platform
git status --short
git add backend/app/api/accounts.py backend/app/api/leads.py backend/app/api/workspace.py backend/app/api/routes.py backend/app/core/auth.py backend/app/core/config.py backend/app/database/db.py backend/app/database/models.py backend/app/main.py backend/scripts/bootstrap_account.py backend/scripts/snapshot_db.py backend/tests/test_personal_workspace.py
git add frontend/src/App.tsx frontend/src/components/LoginScreen.tsx frontend/src/components/login.css frontend/src/components/DashboardLeads.tsx frontend/src/components/dashboard-leads.css frontend/src/components/AccountManagement.tsx frontend/src/components/UserDashboard.tsx frontend/src/components/OpportunitiesTable.tsx frontend/vite.config.ts docs/dashboard-accounts-ec2.md
git diff --cached --stat
git commit -m "Add personal dashboard accounts, saved leads and admin activity"
git push origin main
```

If your branch is not main, merge these changes into main before the EC2 pull.
Do not proceed if the push failed.

## 2. Connect to EC2

Use the private address only while connected to the network that reaches it:

```powershell
ssh -i "C:\Users\rajes\Downloads\cg-bd-agent.pem" ubuntu@10.0.1.189
```

## 3. On EC2: back up and pull

```bash
cd ~/Deployment/lead-opportunity-platform
git status --short
git rev-parse HEAD
cd backend
.venv/bin/python scripts/snapshot_db.py --output "data/before-dashboard-accounts-$(date +%Y%m%d-%H%M%S).db"
cd ..
git pull --ff-only origin main
git log -1 --oneline
```

Keep the printed previous commit and backup path for recovery. Do not overwrite
EC2 with a laptop database: that would overwrite server records and accounts.

## 4. Configure EC2 account email and disable the separate workspace

```bash
nano backend/.env
```

Set these values (edit existing entries rather than making duplicate entries):

```dotenv
LOP_PERSONAL_LOGIN=true
LOP_WORKSPACE_ENABLED=false
LOP_DASHBOARD_URL=http://15.207.68.78
```

Use your HTTPS dashboard URL instead if HTTPS is configured. Retain the existing
LOP_APPROVAL_SECRET: changing it invalidates signed sessions and links. Remove
LOP_DASHBOARD_PASSWORD and LOP_ADMIN_PASSWORD; neither grants access now.

Ensure LOP_SMTP_HOST, LOP_SMTP_PORT, LOP_SMTP_USER and LOP_SMTP_PASSWORD are set to
working SMTP credentials. These remain only in EC2's .env, never Git. Registration works without SMTP; password recovery requires email delivery. The company-domain setting no longer limits
self-registration. Registration does not establish ownership of the supplied email address. Existing or reserved addresses cannot be claimed through registration.

## 5. Initialise tables and your admin account on EC2

```bash
cd ~/Deployment/lead-opportunity-platform/backend
.venv/bin/python -c 'from app.database.db import init_db; init_db()'
.venv/bin/python scripts/bootstrap_account.py --email rajeshwari@catalysts.org --name "Rajeshwari Chaubey"
```

Choose and confirm your private password in the terminal (12+ characters). The
local admin account is separate from EC2. If an EC2 administrator already exists,
the script refuses to replace it; use that account's password or Forgot password.
Other users register themselves and never receive admin access automatically.

## 6. Restart the API and publish the frontend

```bash
cd ~/Deployment/lead-opportunity-platform
sudo supervisorctl restart lead-scanning-api
sudo supervisorctl status lead-scanning-api
curl --retry 12 --retry-connrefused --retry-delay 5 --max-time 15 -fsS http://127.0.0.1:8001/api/config
WEB_ROOT=/var/www/lead-opportunity-platform bash deploy/frontend-dashboard.sh
```

Stop if a command fails. The frontend script backs up the current Nginx web root
and prints its path. `/api/config` should respond with `auth_required:true` and,
before sign-in, `authenticated:false` and `is_admin:false`.

## 7. Verify in the live browser

Open http://15.207.68.78 and press Ctrl+Shift+R.

1. Sign in with your EC2 admin account. Team activity and account management are
   available within the dashboard; there is no My workspace entry.
2. In an incognito window register a different email, including Gmail if desired,
   with a name and password (12+ characters). It should sign in immediately
   without sending an email. Test actual email receipt separately using Forgot password.
3. That user must see no Admin link; `?view=admin` must still show the user dashboard.
4. Save a lead, update its progress/notes and filters, sign out, then sign back in.
   The same saved data must return. Verify another account cannot see it.
5. In your admin dashboard expand Team activity and that user to inspect the lead.
6. Change the user's password and test the new password and Forgot password flow.
7. The common password must not sign in. A signed-in request to `/api/workspace/session`
   must return 404 with the workspace disabled.

My leads is a single collapsed bar above the dashboard. Opening it shows saved leads,
distinct viewed/reviewed counts, source-link opens and the ten most recently visited
opportunities. Views are recorded only when the user explicitly opens a brief; reviews
require Mark reviewed. A source-link click is not proof the external page loaded.
Tracking starts with this release; older browsing cannot be reconstructed.

Admin monitoring includes these per-account counts, saved leads, notes, next steps
and status history. It does not track browsing outside this application or time online.
The new lead_activity table is created by init_db during deployment and is excluded
from filtered opportunity-only database transfers.

## Recovery

If a release fails, retain all database files and backups. Restore the previous
frontend backup printed by the deploy script and use the previous application
commit in a separate checkout. Additive tables do not require deleting data to
run the old code. Restoring a database snapshot discards newer writes; plan that
separately rather than automatically overwriting the live database.

## Unsave leads

Save lead toggles to Unsave after loading the account saved list. Unsave hides the
lead from saved lists while retaining its notes, stages, attachments, and viewed/reviewed
activity. Saving again restores the same journey. The additive application_journeys.saved
column is migrated at API startup; deploy backend and frontend together.
