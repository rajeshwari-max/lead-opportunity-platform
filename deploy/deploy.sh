#!/usr/bin/env bash
# Deploy the Lead Scanning Platform on EC2.
#
#   ./deploy/deploy.sh            # both halves
#   ./deploy/deploy.sh backend    # Python changed
#   ./deploy/deploy.sh frontend   # React changed
#
# Mirrors the PH-EBS procedure: the backend is restarted through Supervisor,
# the frontend is built and the dist copied into the Nginx web root. The step
# most often forgotten by hand is that `npm run build` alone deploys nothing —
# this script always does the copy.

set -euo pipefail

# Derived from this script's own location, so the checkout can live under any
# directory name. A hard-coded path here silently deployed the wrong tree if the
# repo was cloned under its GitHub name rather than "lead-scanning".
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_ROOT="/var/www/lead-scanning/dist"
SERVICE="lead-scanning-api"
TARGET="${1:-all}"

green() { printf "\033[0;32m%s\033[0m\n" "$1"; }
fail()  { printf "\033[0;31m%s\033[0m\n" "$1" >&2; exit 1; }

[[ -d "$APP_DIR" ]] || fail "Not found: $APP_DIR"
cd "$APP_DIR"

if [[ "$TARGET" == "all" || "$TARGET" == "backend" ]]; then
    green "==> Backend"
    source backend/.venv/bin/activate
    pip install -q -r backend/requirements.txt

    # Fail before restarting rather than after: a syntax error caught here
    # leaves the old worker serving traffic, instead of taking the API down
    # and discovering the problem in the Supervisor log.
    ( cd backend && python -c "import app.main" ) || fail "Backend import failed — not restarting"

    sudo supervisorctl restart "$SERVICE"
    sleep 3
    sudo supervisorctl status "$SERVICE"

    # Prove the app actually answers, not merely that the process is RUNNING.
    curl -fsS --max-time 10 http://127.0.0.1:8001/api/config > /dev/null \
        && green "    API responding on 127.0.0.1:8001" \
        || fail "API is not responding — check logs/supervisor-err.log"
fi

if [[ "$TARGET" == "all" || "$TARGET" == "frontend" ]]; then
    green "==> Frontend"
    cd "$APP_DIR/frontend"
    npm ci
    npm run build
    [[ -f dist/index.html ]] || fail "Build produced no dist/index.html"

    sudo mkdir -p "$WEB_ROOT"
    # Clear first. Copying over the top leaves every previous build's hashed
    # bundles behind, and the directory grows without limit.
    sudo rm -rf "${WEB_ROOT:?}"/*
    sudo cp -r dist/* "$WEB_ROOT"/
    sudo chown -R www-data:www-data "$WEB_ROOT"

    sudo nginx -t || fail "Nginx config invalid — not reloading"
    sudo systemctl reload nginx
    green "    Deployed bundle: $(ls "$WEB_ROOT"/assets/*.js 2>/dev/null | head -1 | xargs -r basename)"
fi

green "==> Done. Hard-refresh the browser (Ctrl+Shift+R) to drop the old bundle."
