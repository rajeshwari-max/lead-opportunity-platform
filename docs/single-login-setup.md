> Current release: registration now creates an ordinary user immediately with a chosen password, without email verification. Existing/reserved addresses must sign in or recover their password. See [current deployment guide](dashboard-accounts-ec2.md). Email is still required for Forgot password.

# Single personal login and explicit admin access

Users sign in once with their email and chosen password. My workspace opens
immediately using that same session. There is no second unlock form.

## User and administrator flow

The login page also offers **Register** and **Forgot password?**. Register asks
for a name and email, then emails a verification link where the recipient chooses
their password. Existing accounts are never overwritten by registration. Forgot
password emails a single-use reset link without disclosing whether an account
exists. These self-service links expire after one hour; administrator invitations
below expire after 24 hours. Existing personal passwords continue to work.
Existing active team members who previously used only a shared password can use
Forgot password to verify their email and establish their personal password.

Self-registration respects `LOP_ALLOWED_EMAIL_DOMAINS` when configured. If it is
empty, any email may register after email verification. New accounts always have
ordinary user access and automatic digest delivery disabled. An inactive account
cannot reactivate itself through either flow.

Email delivery requires the existing SMTP configuration (`LOP_SMTP_USER`,
`LOP_SMTP_PASSWORD`, `LOP_SMTP_HOST`, `LOP_SMTP_PORT`). Configure
`LOP_DASHBOARD_URL` to the deployed frontend URL. A request from an exact configured
`LOP_CORS_ORIGINS` frontend uses that origin for the email link, so local Vite
requests return to localhost. Untrusted Origin/Host headers cannot set reset links.
If SMTP is unavailable, the UI reports the delivery/configuration error; it never
claims a reset email was sent. No live test emails are sent by automated tests.

Vite preserves the browser host when proxying API requests. The backend allows
same-origin requests and explicitly configured frontend origins, fixing the
previous localhost cross-origin rejection without accepting arbitrary websites.
Restart a running backend/Vite server after deploying the changed configuration.

1. An administrator opens My workspace → Manage user accounts & admin access.
2. Enter the person's name/email and create an invitation. Share the generated
   link privately with that person. It is not emailed automatically.
3. The recipient opens the single-use link, chooses a password (12–200 characters)
   and is signed into the user dashboard. Links expire after 24 hours.
4. New users have no admin rights. After activation, an administrator can explicitly
   grant or remove admin access. Changing the URL cannot grant permission.
5. Password recovery uses a new invitation for the existing email. The workspace
   is retained, and old sessions stop working when the new password is set.

Roles and active-member status are checked in the database on every authenticated
request. Removing admin access takes effect for existing sessions. Deactivated
team members cannot sign in or continue using an old session. Administrators
cannot remove their own role through this interface.

Existing individual workspace passwords are reused as the single login password.
Existing shared-password cookies are rejected, and shared dashboard/admin passwords
no longer authenticate anyone. Previous accounts default to ordinary user roles.
The first personal administrator must therefore be provisioned before rollout.

## First administrator: local or EC2

Do not deploy only the frontend. Commit/push all personal-workspace and single-login
files together, including the database migration, accounts router and bootstrap
script. Review `git diff` and stage the intended files; exclude local databases,
secrets and unrelated work. No additional package installation is required.

On EC2, after the new commit has been pushed:

```bash
cd ~/Deployment/lead-opportunity-platform
git pull --ff-only origin main
cd backend
.venv/bin/python scripts/snapshot_db.py --output "data/before-personal-login-$(date +%Y%m%d-%H%M%S).db"
.venv/bin/python scripts/bootstrap_account.py --email YOUR_WORK_EMAIL --name "YOUR NAME"
```

Replace the two placeholders with your own identity. The script asks for your
chosen password without echoing it. It refuses to run if a personal administrator
already exists. It applies the additive schema migration and preserves workspace
records. Run it only through trusted server access. Do not put a password in a
shell command, Git, or chat.

For local Windows development the equivalent command, from `backend`, is:

```powershell
.venv\Scripts\python.exe scripts\bootstrap_account.py --email YOUR_WORK_EMAIL --name "YOUR NAME"
```

Then on EC2:

```bash
.venv/bin/python -m unittest tests.test_personal_workspace
sudo supervisorctl restart lead-scanning-api
curl --retry 30 --retry-connrefused --retry-delay 3 --max-time 10 -fsS http://127.0.0.1:8001/api/config
cd ..
WEB_ROOT=/var/www/lead-opportunity-platform bash deploy/frontend-dashboard.sh
```

Stop if any command fails. Sign in with your own email and chosen password, invite
users, and grant admin rights only to the accounts you choose. Verify a normal
user cannot open admin APIs and can open My workspace without another prompt.

Personal login is enabled by default (`LOP_PERSONAL_LOGIN=true`). Keep a strong,
persistent `LOP_APPROVAL_SECRET`; it signs the session cookies. Use HTTPS on the
deployed site to protect passwords and sessions in transit. The legacy shared
password settings are not accepted for login. Setting `LOP_PERSONAL_LOGIN=false`
is strictly for an isolated development demo: it exposes a local-admin workspace.

Password hashes use salted PBKDF2-SHA256 with 600,000 iterations. Cookies are
HttpOnly, SameSite=Lax and Secure on HTTPS, with a 30-day expiry. Login attempts
are rate-limited per email and client IP per worker. Apply an additional edge
rate limit for multi-worker production deployments. Role management is enforced
server-side; invitation tokens are stored only as hashes and consumed atomically.
The invitation token is placed in a URL fragment rather than the server access log.

Administrators can issue recovery links, so this is access control rather than
encryption against server administrators. Protect recovery links and backups.

## Disposable preview

```powershell
cd E:\lead-opportunity-platform\backend
.venv\Scripts\python.exe scripts\preview_workspace.py --login --port 5195
```

Build the frontend first. Open http://127.0.0.1:5195/?view=user. The synthetic demo
account is `preview@example.org` with password `Preview-only-pass-2026`. This account
exists only in the temporary preview database. The demo covers login/workspace;
catalogue APIs are not provided by this isolated preview. Nothing is deployed to
EC2 by running it.

The disposable preview disables SMTP to avoid sending real mail from a temporary
database. Registration/recovery buttons are visible; submitting them reports that
email is not configured. Full flows are tested using mocked delivery.
