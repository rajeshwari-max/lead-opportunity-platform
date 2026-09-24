[CmdletBinding()]
param(
    [string]$HostName = "10.0.1.189",
    [string]$UserName = "ubuntu",
    [string]$KeyPath = "C:\Users\rajes\Downloads\cg-bd-agent.pem",
    [string]$ExpectedCommit = "a2eb1a8",
    [string]$RemoteProject = "/home/ubuntu/Deployment/lead-opportunity-platform"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Run([scriptblock]$Command, [string]$Failure) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw $Failure }
}

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $ProjectDir "backend"
$Python = Join-Path $BackendDir ".venv\Scripts\python.exe"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Transfer = Join-Path $BackendDir "data\developmentaid-transfer-$Stamp.db"
$Remote = "$UserName@$HostName"
$RemoteTransfer = "/home/$UserName/developmentaid-transfer-$Stamp.db"

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "EC2 key not found: $KeyPath"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Local Python environment not found: $Python"
}

Step "Checking SSH access and the EC2 checkout"
$Preflight = @'
set -euo pipefail
PROJECT_DIR="$1"
EXPECTED="$2"
test -d "$PROJECT_DIR/.git"
test -d "$PROJECT_DIR/backend"
test -d "$PROJECT_DIR/frontend"
cd "$PROJECT_DIR"
if [ -n "$(git status --porcelain)" ]; then
  echo "EC2 checkout has uncommitted changes; deployment stopped." >&2
  git status --short >&2
  exit 20
fi
sudo supervisorctl status lead-scanning-api
df -h "$PROJECT_DIR" | tail -1
git fetch origin main
git merge-base --is-ancestor "$EXPECTED" origin/main
echo "EC2 preflight passed."
'@
$Preflight | & ssh -i $KeyPath -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new `
    $Remote "bash -s -- '$RemoteProject' '$ExpectedCommit'"
if ($LASTEXITCODE -ne 0) {
    throw "EC2 preflight failed. Confirm that port 22 allows your current IP and that the server checkout is clean."
}

Step "Creating a fresh active DevelopmentAid transfer"
Run {
    & $Python (Join-Path $BackendDir "scripts\snapshot_db.py") `
        --output $Transfer --only-source DevelopmentAid --active-only
} "Could not create the local transfer snapshot."

Step "Backing up EC2, pulling the release, testing, and deploying"
$Deploy = @'
set -euo pipefail
PROJECT_DIR="$1"
EXPECTED="$2"
BACKEND_DIR="$PROJECT_DIR/backend"
cd "$PROJECT_DIR"
git pull --ff-only origin main
ACTUAL=$(git rev-parse --short HEAD)
git merge-base --is-ancestor "$EXPECTED" HEAD || {
  echo "Required release $EXPECTED is not present in deployed HEAD $ACTUAL" >&2
  exit 21
}
echo "Deploying HEAD $ACTUAL (contains release $EXPECTED)."
cd "$BACKEND_DIR"
mkdir -p data/deployment-backups
./.venv/bin/python scripts/snapshot_db.py \
  --output "data/deployment-backups/pre-classification-$(date +%Y%m%d-%H%M%S).db"
./.venv/bin/python -m pytest \
  tests/test_opportunity_quality.py \
  tests/test_classification_model.py \
  tests/test_active_rule.py \
  tests/test_parser_fixtures.py \
  tests/test_unclassified_section.py -q
cd "$PROJECT_DIR"
bash deploy/deploy.sh
sudo supervisorctl status lead-scanning-api
curl --retry 30 --retry-connrefused --retry-delay 3 --max-time 10 \
  -fsS http://127.0.0.1:8001/api/config >/dev/null
echo "Code deployment passed."
'@
$Deploy | & ssh -i $KeyPath $Remote "bash -s -- '$RemoteProject' '$ExpectedCommit'"
if ($LASTEXITCODE -ne 0) { throw "Code deployment failed; the EC2 database backup was retained." }

Step "Uploading the filtered DevelopmentAid transfer"
Run {
    & scp -i $KeyPath $Transfer "${Remote}:$RemoteTransfer"
} "Transfer upload failed."

Step "Dry-running and applying the duplicate-safe import"
$Import = @'
set -euo pipefail
PROJECT_DIR="$1"
TRANSFER="$2"
BACKEND_DIR="$PROJECT_DIR/backend"
cd "$BACKEND_DIR"
test -f "$TRANSFER"
echo "--- import preview ---"
./.venv/bin/python scripts/merge_db.py \
  --source "$TRANSFER" --only-source DevelopmentAid --active-only --dry-run
echo "--- applying import ---"
./.venv/bin/python scripts/merge_db.py \
  --source "$TRANSFER" --only-source DevelopmentAid --active-only
echo "--- idempotence proof ---"
VERIFY=$(./.venv/bin/python scripts/merge_db.py \
  --source "$TRANSFER" --only-source DevelopmentAid --active-only --dry-run)
printf '%s\n' "$VERIFY"
printf '%s\n' "$VERIFY" | grep -Eq 'genuinely new[[:space:]]*:[[:space:]]*0$'
sudo supervisorctl restart lead-scanning-api
curl --retry 30 --retry-connrefused --retry-delay 3 --max-time 10 \
  -fsS http://127.0.0.1:8001/api/config >/dev/null
rm -f -- "$TRANSFER"
echo "Import passed, a repeated import found zero new rows, and the API is healthy."
'@
$Import | & ssh -i $KeyPath $Remote "bash -s -- '$RemoteProject' '$RemoteTransfer'"
if ($LASTEXITCODE -ne 0) {
    throw "Import or verification failed. The EC2 merge backup and transfer file were retained."
}

Step "Deployment complete"
Write-Host "Release commit : $ExpectedCommit"
Write-Host "Dashboard      : http://$HostName/"
Write-Host "Local transfer : $Transfer"
Write-Host "The EC2 importer skipped canonical and cross-source duplicates and proved the import is idempotent."
