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
PYTHON="$REPO/backend/.venv/bin/python"

# These default to ON so the normal one-command deployment verifies the new
# scraper/deadline work as part of the same workflow.  They are switches, not a
# second deployment method: set one to 0 only when diagnosing that specific
# check, e.g. RUN_TESTS=0 ./deploy/update.sh.
RUN_TESTS=${RUN_TESTS:-1}
RUN_WORLD_BANK_CHECK=${RUN_WORLD_BANK_CHECK:-1}
ARCHIVE_PASSED=${ARCHIVE_PASSED:-1}
WORLD_BANK_PAGES=${WORLD_BANK_PAGES:-3}

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

# ----------------------------------------------------- 2. backend preflight
say "Checking the backend before publishing"
[ -x "$PYTHON" ] || die "backend Python is missing at $PYTHON"
cd "$REPO/backend"

# Proves that DevelopmentAid still uses the required filtered grant and tender
# URLs.  This check does not need a signed-in session and prints both effective
# URLs so a changed environment override cannot remain invisible.
"$PYTHON" scripts/devaid_urls.py

if [ "$RUN_TESTS" = "1" ]; then
  say "Running the backend test suite"
  "$PYTHON" -m pytest -q --disable-warnings
else
  echo "    backend tests skipped because RUN_TESTS=$RUN_TESTS"
fi

# ------------------------------------------------------------ 3. frontend
say "Building the dashboard"
cd "$REPO/frontend"
npm install --silent
npm run build
[ -f dist/index.html ] || die "the build produced no dist/index.html"

# ------------------------------------------------------------- 4. publish
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

# ------------------------------------------------------------- 5. backend
say "Restarting $SERVICE"
sudo supervisorctl restart "$SERVICE"

# Wait for the API to ANSWER, rather than sleeping a fixed 8 seconds and hoping.
#
# Startup runs the migrations, the FTS index check and the column backfills
# against the whole database. On the production database (~176 MB) that takes
# well over 8s, so the old `sleep 8` declared
#
#     FAILED: the API did not answer on http://127.0.0.1:8001
#
# on a deploy that had in fact worked perfectly: supervisor showed the service
# RUNNING the entire time and the API answered correctly a minute later. A
# false failure is worse than no check at all — it arrives exactly when someone
# is deciding whether to roll back, and it argues for rolling back a good
# deploy.
#
# So: poll every 3s up to BOOT_TIMEOUT, and abort EARLY if the process has died,
# which is the case the check actually exists to catch. Slow and dead look
# identical to a fixed sleep; they do not look identical to supervisor.
BOOT_TIMEOUT=${BOOT_TIMEOUT:-180}
say "Waiting for the API (up to ${BOOT_TIMEOUT}s — a large database is slow to migrate)"
cfg=""
waited=0
while [ "$waited" -lt "$BOOT_TIMEOUT" ]; do
  if cfg=$(curl -s --max-time 5 "$API/api/config") && [ -n "$cfg" ]; then
    echo "    answered after ${waited}s"
    break
  fi
  cfg=""
  if ! sudo supervisorctl status "$SERVICE" | grep -q RUNNING; then
    die "$SERVICE stopped. It did not just start slowly — it exited.
       sudo supervisorctl status $SERVICE
       tail -60 $REPO/logs/supervisor-err.log"
  fi
  sleep 3
  waited=$((waited + 3))
  # Written as a full `if`, not `[ ... ] && echo`. Under `set -e` an AND-list
  # whose test fails is only exempt from aborting by a subtle rule about which
  # command in the list failed — this loop should not depend on knowing that.
  if [ $((waited % 30)) -eq 0 ]; then
    echo "    still starting (${waited}s)..."
  fi
done
[ -n "$cfg" ] || die "the API did not answer on $API within ${BOOT_TIMEOUT}s, but
       $SERVICE is still RUNNING — so it is probably still working through
       startup rather than broken. Check again in a minute with:
         curl -s $API/api/config
       and if it stays silent:
         tail -60 $REPO/logs/supervisor-err.log
       To allow longer next time:  BOOT_TIMEOUT=600 ./deploy/update.sh"

# -------------------------------------------------- 6. deadline maintenance
# Startup has completed its migrations, so the new audit can now safely read
# the live schema.  It first writes a dry-run report.  Only genuinely passed,
# dated Active rows are changed to Expired; undated and rolling rows remain in
# the database but the strict user query keeps them out of Active Opportunities.
say "Auditing Active opportunity deadlines (Asia/Kolkata)"
AUDIT_DIR="$REPO/backend/data/deploy-audits"
BACKUP_DIR="$REPO/backend/data/backups"
mkdir -p "$AUDIT_DIR" "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
before_report="$AUDIT_DIR/deadlines-before-$stamp.json"
after_report="$AUDIT_DIR/deadlines-after-$stamp.json"
backup_file="$BACKUP_DIR/pre-deadline-archive-$stamp.db"

cd "$REPO/backend"
"$PYTHON" scripts/active_deadline_audit.py --samples 5 --json "$before_report"
passed_count=$("$PYTHON" -c 'import json, sys; report=json.load(open(sys.argv[1], encoding="utf-8")); print(report["before"]["buckets"]["active_deadline_before_today"]["count"])' "$before_report")

if [ "$passed_count" -gt 0 ] && [ "$ARCHIVE_PASSED" = "1" ]; then
  say "Marking $passed_count passed Active opportunities Expired"
  "$PYTHON" scripts/active_deadline_audit.py \
    --apply \
    --backup "$backup_file" \
    --json "$after_report"
  echo "    database backup: $backup_file"
  echo "    audit report:    $after_report"
elif [ "$passed_count" -gt 0 ]; then
  echo "    WARNING: $passed_count passed Active rows found but ARCHIVE_PASSED=$ARCHIVE_PASSED"
  echo "    dry-run report: $before_report"
else
  echo "    no passed Active deadlines found"
  echo "    audit report: $before_report"
fi

# One worker only. Two means two schedulers, which means every automatic email
# goes out twice.
workers=$(pgrep -fc gunicorn || true)
[ "$workers" -le 2 ] || echo "    WARNING: $workers gunicorn processes — expected 2 (master + one worker)"

# --------------------------------------------------------------- 7. verify
say "Verifying"
echo "    $cfg"

case "$cfg" in
  *auth_required*) echo "    backend is running the new code" ;;
  *) die "the API answered with the old /api/config — it did not restart on the new code.
       Check: sudo supervisorctl status $SERVICE
              tail -60 $REPO/logs/supervisor-err.log" ;;
esac

case "$cfg" in
  *'"auth_required":false'*)
    echo
    echo "    NOTE: no dashboard password is set, so there is no login screen and"
    echo "    everyone is treated as a local admin. Set LOP_DASHBOARD_PASSWORD and"
    echo "    LOP_ADMIN_PASSWORD in backend/.env, then run this script again." ;;
esac

# The World Bank scraper must begin at the official opportunities page.  The
# verifier also checks pagination, procurement-only filtering, field coverage,
# duplicates and leaked automation browser processes.
if [ "$RUN_WORLD_BANK_CHECK" = "1" ]; then
  say "Verifying World Bank from the canonical opportunities page"
  world_bank_report="$AUDIT_DIR/world-bank-$stamp.json"
  "$PYTHON" scripts/verify_source.py world_bank \
    --pages "$WORLD_BANK_PAGES" \
    --json "$world_bank_report"
  echo "    verification report: $world_bank_report"
else
  echo "    World Bank live verification skipped because RUN_WORLD_BANK_CHECK=$RUN_WORLD_BANK_CHECK"
fi

# A missing/expired DevelopmentAid login must be visible, but it must not block
# unrelated sources from being deployed.  Establish the session on a computer
# with a screen and push it to this backend, then status will report CONNECTED.
say "Checking the DevelopmentAid session"
if "$PYTHON" scripts/devaid_session.py status; then
  echo "    DevelopmentAid session is ready"
else
  echo "    WARNING: DevelopmentAid is not signed in on EC2."
  echo "    Connect it on localhost and push the session before running that scraper."
fi

curl -sI "http://localhost" | head -1

say "Done — hard-refresh the browser with Ctrl+Shift+R"
