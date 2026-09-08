# Deploy the approved user dashboard

This release adds the cobalt/cyan user dashboard with CMS branding, expanded sticky filters, a two-line category chart label, live pagination and an opportunity brief. The admin layout is unchanged. Dashboard figures and listings come from the existing API; the 50-row design snapshot is not part of the application.

Admins can open the new design using the header's **User dashboard** link or `http://localhost:5173/?view=user` locally. **Admin panel** returns to their existing controls. This switches the layout only; it does not change account permissions. Regular users always see the new user dashboard.

The user dashboard now has an Unclassified tab above the table. Its count follows current date, language and other filters; it can differ from the admin review backlog. The admin classification endpoints remain restricted. Wrike is not included.

## 1. Send the local code to the repository

The changes are local until committed and pushed. In PowerShell on your computer:

```powershell
cd E:\lead-opportunity-platform
git add frontend/src/App.tsx frontend/src/hooks/useApi.ts frontend/src/components/UserDashboard.tsx frontend/src/components/user-dashboard.css frontend/src/components/FiltersSidebar.tsx frontend/src/lib/api.ts frontend/src/lib/types.ts backend/app/api/routes.py backend/app/schemas/opportunity.py backend/app/services/filter_service.py backend/tests/test_unclassified_section.py deploy/frontend-dashboard.sh docs/user-dashboard-deployment.md
git diff --cached --stat
git commit -m "Apply approved CMS user dashboard design"
git push origin main
```

Review the staged list before committing. Do not include unrelated backend/data or document changes. If other work was already staged, separate it first.

## 2. Update EC2

The repository's current EC2 update script identifies the checkout as `~/Deployment/lead-opportunity-platform`. Use that path unless your checkout has moved.

```bash
cd ~/Deployment/lead-opportunity-platform
git status --short
git pull --ff-only origin main
```

If the pull fails, stop and resolve the reported local changes or branch conflict; do not reset the server's work automatically.

Inspect the existing Lead Scanning server block:

```bash
sudo nginx -T 2>&1 | less
```

Find the server serving your Lead Scanning address, then its `root` directive. The supplied `deploy/nginx-leads.conf` uses `/var/www/lead-scanning/dist`. If the live configuration uses a different directory, substitute that directory below. Do not choose another site's root.

```bash
backend/.venv/bin/python -m compileall -q backend/app/api/routes.py backend/app/schemas/opportunity.py backend/app/services/filter_service.py
sudo supervisorctl restart lead-scanning-api
curl --retry 30 --retry-connrefused --retry-delay 3 --max-time 10 -fsS http://127.0.0.1:8001/api/config
WEB_ROOT=/var/www/lead-scanning/dist bash deploy/frontend-dashboard.sh
```

The Unclassified tab uses a new backend filter, so restart the API before publishing the frontend. No new database schema or dependencies are needed for this change. Stop if any step fails.

The frontend script checks Nginx, installs locked frontend dependencies, builds, backs up the existing web root, copies assets and replaces the entry page last. Old hashed assets stay available for already-open browser tabs. It does not change Nginx configuration, restart the API, migrate the database or run scrapers.

## 3. Verify

```bash
curl -fsS -o /dev/null -w 'Dashboard HTTP %{http_code}\n' http://15.207.68.78/
```

Open the site, hard-refresh with Ctrl+Shift+R, and sign in as a regular user. Check CMS in the logo, readable chart centre text, open filters remaining on the left while scrolling, filtering, page navigation and the selected opportunity brief. Sign in as admin separately to confirm the existing panel.

## Roll back the page

The deployment prints its backup directory. Substitute that exact path for the placeholder:

```bash
WEB_ROOT=/var/www/lead-scanning/dist
BACKUP=/var/www/lead-scanning/dist.backup-YYYYMMDD-HHMMSS
sudo test -f "$BACKUP/index.html" && sudo cp "$BACKUP/index.html" "$WEB_ROOT/index.html.rollback" && sudo mv "$WEB_ROOT/index.html.rollback" "$WEB_ROOT/index.html"
```

Previous assets were retained, so restoring the old entry page restores the previous dashboard without restarting the backend.

## Validation performed locally

The production TypeScript/Vite build passes. All 26 unclassified backend tests pass, including filtering, pagination and export parity. Vite reports an existing large-bundle advisory. This does not prevent the build. The deployment script still needs execution on your EC2 host; no remote deployment is performed by these local changes.
