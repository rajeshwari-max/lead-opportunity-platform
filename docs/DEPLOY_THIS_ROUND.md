# Deploy the World Bank, deadline and admin-access changes

These steps deliberately avoid `deploy/update.sh`: that script resets the EC2
checkout to `origin/main`, which is unsafe until every server-side change has
been reviewed and committed. Run one command at a time.

## 1. Verify locally

The local virtual-environment launcher has been repaired against the installed
Python 3.12 runtime. Verify with the normal project commands:

```powershell
cd E:\lead-opportunity-platform\backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\devaid_urls.py
.\.venv\Scripts\python.exe scripts\devaid_session.py status
.\.venv\Scripts\python.exe scripts\verify_source.py developmentaid --pages 3 --note "person-established DevelopmentAid session"
.\.venv\Scripts\python.exe scripts\verify_source.py world_bank --pages 3
cd E:\lead-opportunity-platform\frontend
npm run build
```

For World Bank, retain the three proof lines showing the canonical page, the
Current Opportunities selector and the first-party endpoint that page requested.

## 2. Review and publish the code

Do not use `git add -A` in this dirty worktree. Review first, make sure no `.env`,
database, session, verification-output or temporary files are staged, then stage
only the intended files.

```powershell
cd E:\lead-opportunity-platform
git status --short
git diff --check
git diff --stat
git add backend/app/scrapers/worldbank.py backend/app/services/actionable.py backend/app/services/filter_service.py backend/app/services/matching_service.py backend/app/schemas/opportunity.py backend/app/api/routes.py backend/app/services/source_manifest.py backend/app/services/verification.py backend/scripts/devaid_urls.py backend/scripts/active_deadline_audit.py backend/scripts/verify_source.py backend/tests/test_devaid_urls.py backend/tests/test_worldbank_canonical.py backend/tests/test_admin_gating.py backend/tests/test_active_rule.py backend/tests/test_active_deadline_audit.py backend/tests/test_notice_types.py backend/tests/test_verify_source_script.py frontend/src/App.tsx docs/DEPLOY_THIS_ROUND.md
git diff --cached --check
git diff --cached --stat
git commit -m "Use canonical World Bank opportunities and strict active access"
git push origin main
```

## 3. Discover the live EC2 checkout

SSH to EC2, then obtain the backend working directory from the running process.
Do not infer it from an old Supervisor file.

```bash
BACKEND_DIR=$(sudo readlink -f "/proc/$(pgrep -fo 'gunicorn|uvicorn')/cwd")
PROJECT_DIR=$(dirname "$BACKEND_DIR")
printf 'backend=%s\nproject=%s\n' "$BACKEND_DIR" "$PROJECT_DIR"
sudo supervisorctl status lead-scanning-api
```

The backend path must end in `/backend` and the project path must contain the
expected `.git`, `frontend`, `backend` and `deploy` directories.

Confirm both passwords are configured; without the admin password every signed-in
person is intentionally treated as an administrator.

```bash
grep -Eq '^LOP_DASHBOARD_PASSWORD=.+$' "$BACKEND_DIR/.env" && echo 'dashboard password set' || echo 'DASHBOARD PASSWORD NOT SET'
grep -Eq '^LOP_ADMIN_PASSWORD=.+$' "$BACKEND_DIR/.env" && echo 'admin password set' || echo 'ADMIN PASSWORD NOT SET'
```

## 4. Pull, build and audit before applying

First confirm the server checkout is clean. Do not pull over unexplained changes.

```bash
cd "$PROJECT_DIR"
git status --short
git pull --ff-only origin main
cd "$BACKEND_DIR"
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/devaid_urls.py
./.venv/bin/python scripts/active_deadline_audit.py --json "$HOME/deadlines-before.json"
cd "$PROJECT_DIR/frontend"
npm ci
npm run build
```

Read `$HOME/deadlines-before.json`. When the passed-deadline bucket is correct,
archive only those passed rows. This creates a backup first and deletes nothing:

```bash
cd "$BACKEND_DIR"
./.venv/bin/python scripts/active_deadline_audit.py --apply --backup "$HOME/pre-deadline-archive-$(date +%F-%H%M).db" --json "$HOME/deadlines-after.json"
```

## 5. Publish the frontend and restart

Use the web root already configured in Nginx. Inspect it before copying.

```bash
WEBROOT=$(sudo nginx -T 2>/dev/null | grep -m1 -oP '(?<=root\s)[^;]+')
printf 'webroot=%s\n' "$WEBROOT"
sudo cp -a "$WEBROOT" "$HOME/webroot-backup-$(date +%F-%H%M)"
sudo cp -r "$PROJECT_DIR/frontend/dist/." "$WEBROOT/"
sudo chown -R www-data:www-data "$WEBROOT"
sudo supervisorctl restart lead-scanning-api
sudo supervisorctl status lead-scanning-api
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/config
pgrep -fc 'chrome|chromium' || true
```

Test with an ordinary user's `lop_session` cookie: both routes must return 403.
They must return 200 with an administrator cookie.

```bash
curl -s -o /dev/null -w 'review-queue %{http_code}\n' -H 'Cookie: lop_session=PASTE_USER_COOKIE' http://127.0.0.1:8001/api/review-queue
curl -s -o /dev/null -w 'unclassified %{http_code}\n' -H 'Cookie: lop_session=PASTE_USER_COOKIE' http://127.0.0.1:8001/api/opportunities/unclassified
```

## 6. Push the DevelopmentAid session from Windows

First use the EC2 discovery command above. The `--remote` value is the verified
`BACKEND_DIR`, not a guessed checkout.

```powershell
cd E:\lead-opportunity-platform\backend
.\.venv\Scripts\python.exe scripts\devaid_session.py status
.\.venv\Scripts\python.exe scripts\devaid_session.py push --host ubuntu@15.207.68.78 --remote /home/ubuntu/Deployment/lead-opportunity-platform/backend --python ./.venv/bin/python
```

Change `--remote` if the running process reported another backend path. Success
means the command finishes with `CONNECTED`, not merely that cookies were copied.

On EC2:

```bash
cd "$BACKEND_DIR"
chmod 600 data/devaid_session.json
./.venv/bin/python scripts/devaid_session.py status
BEFORE=$(pgrep -fc 'chrome|chromium' || true)
./.venv/bin/python scripts/verify_source.py developmentaid --pages 2 --note "session pushed from the laptop $(date +%F)"
AFTER=$(pgrep -fc 'chrome|chromium' || true)
printf 'browser processes before=%s after=%s\n' "$BEFORE" "$AFTER"
```

The before and after browser counts must match. Never print, commit or email the
DevelopmentAid session JSON.
