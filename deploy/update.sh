#!/usr/bin/env bash
# One-command deploy for the EC2 box.
#
#     cd ~/Deployment/lead-opportunity-platform && ./deploy/update.sh
#
# Every step that has silently failed before is checked here rather than
# assumed, because the failure mode each time was a step that appeared to
# succeed while the server carried on serving old code.
set -euo pipefail

REPO="$HOME/Deployment/lead-opportunity-platform"
SERVICE="lead-scanning-api"
API="http://127.0.0.1:8001"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
die() { printf "\n\033[1;31mFAILED: %s\033[0m\n" "$*" >&2; exit 1; }

cd "$REPO"

# ---------------------------------------------------------------- 1. code
say "Fetching latest code"
before=$(git rev-parse --short HEAD)
git fetch origin --quiet
# reset rather than pull: a server build rewrites tracked artefacts, and the
# resulting "local changes would be overwritten" aborts the merge while the
# rest of the deploy carries on against stale code. Nothing here is edited by
# hand — .env, backend/data/ and the database are all gitignored and untouched
# by a reset.
git reset --hard origin/main --quiet
after=$(git rev-parse --short HEAD)

if [ "$before" = "$after" ]; then
  echo "    already at $after (nothing new was pushed)"
else
  echo "    $before -> $after"
fi
git log --oneline -1

# ------------------------------------------------------------ 2. frontend
say "Building the dashboard"
cd "$REPO/frontend"
npm install --silent
npm run build
[ -f dist/index.html ] || die "the build produced no dist/index.html"

# ------------------------------------------------------------- 3. publish
WEBROOT=$(sudo nginx -T 2>/dev/null | grep -m1 -oP '(?<=root\s)[^;]+')
[ -n "$WEBROOT" ] || die "could not read the web root out of the nginx config"
say "Publishing to $WEBROOT"

# The rm and the cp are chained on purpose. Emptying the web root without
# refilling it leaves nginx with no index.html and every visitor gets a bare
# 403 — which is exactly what happened when these ran as two separate steps.
sudo rm -rf "${WEBROOT:?}"/* && sudo cp -r dist/* "$WEBROOT"/
sudo chown -R www-data:www-data "$WEBROOT"
sudo chmod -R 755 "$WEBROOT"
[ -f "$WEBROOT/index.html" ] || die "index.html is missing from the web root"

# ------------------------------------------------------------- 4. backend
say "Restarting $SERVICE"
sudo supervisorctl restart "$SERVICE"
sleep 8

# One worker only. Two means two schedulers, which means every automatic email
# goes out twice.
workers=$(pgrep -fc gunicorn || true)
[ "$workers" -le 2 ] || echo "    WARNING: $workers gunicorn processes — expected 2 (master + one worker)"

# --------------------------------------------------------------- 5. verify
say "Verifying"
cfg=$(curl -s --max-time 15 "$API/api/config") || die "the API did not answer on $API"
echo "    $cfg"

case "$cfg" in
  *auth_required*) echo "    backend is running the new code" ;;
  *) die "the API answered with the old /api/config — it did not restart on the new code.
       Check: sudo supervisorctl status $SERVICE
              tail -40 $REPO/backend/logs/app.log" ;;
esac

case "$cfg" in
  *'"auth_required":false'*)
    echo
    echo "    NOTE: no dashboard password is set, so there is no login screen and"
    echo "    everyone is treated as a local admin. Set LOP_DASHBOARD_PASSWORD and"
    echo "    LOP_ADMIN_PASSWORD in backend/.env, then run this script again." ;;
esac

curl -sI "http://localhost" | head -1

say "Done — hard-refresh the browser with Ctrl+Shift+R"
