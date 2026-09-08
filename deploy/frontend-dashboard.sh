#!/usr/bin/env bash
# Frontend-only release. Explicitly target this site's Nginx root.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${WEB_ROOT:?Set WEB_ROOT to the root of the Lead Scanning Nginx site}"
[[ "$WEB_ROOT" = /* && "$WEB_ROOT" != / ]] || { echo "WEB_ROOT must be an absolute site directory" >&2; exit 1; }
sudo test -f "$WEB_ROOT/index.html" || { echo "No existing dashboard at $WEB_ROOT; check the site's Nginx root" >&2; exit 1; }
sudo nginx -t
cd "$APP_DIR/frontend"
npm ci
npm run build
test -f dist/index.html
BACKUP="${WEB_ROOT%/}.backup-$(date +%Y%m%d-%H%M%S)"
sudo cp -a "$WEB_ROOT" "$BACKUP"
# Keep previous hashed assets available for clients with an older open tab.
# Publish index.html last, using a rename on the same filesystem.
sudo cp -a dist/assets "$WEB_ROOT/"
sudo chmod -R a+rX "$WEB_ROOT/assets"
sudo cp dist/index.html "$WEB_ROOT/index.html.next"
sudo chmod 644 "$WEB_ROOT/index.html.next"
sudo mv "$WEB_ROOT/index.html.next" "$WEB_ROOT/index.html"
echo "Dashboard deployed. Backup: $BACKUP"
echo "Hard-refresh the browser with Ctrl+Shift+R. The API was not restarted."
