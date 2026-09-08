# Automatic EC2 integrity and DevelopmentAid import

## What changes

The API performs identity maintenance before serving requests on startup and at
00:05 Asia/Kolkata daily, independently of the scraping schedule. It repeats
after post-scrape repairs. A missed daily run is coalesced; startup catches up
after downtime. Supervisor must keep the API running (the deployed configuration
uses one worker). Failures are logged, and startup integrity failure prevents
serving a partially migrated database.

Scraping, maintenance and imports use the same computed identity. DevelopmentAid
uses its notice type and numeric notice ID, regardless of title/slug changes.
Tracking parameters and fragments do not make a new notice. Different notice IDs
remain distinct even if their titles match. UNDP titles distinguish lots sharing
a negotiation link. For sources without a detail URL, the fallback is source,
title and organisation; ambiguous or incorrectly extracted source data cannot be
proved identical simply from a similar title.

Legacy IDs are migrated atomically. Duplicate rows receive `merged:` IDs and
remain available for historical references, but are excluded from the dashboard,
including its archive and exports. Approvals, human labels and sent history are
preserved. The deadline audit never reactivates these superseded records.
Identity changes get a verified SQLite backup under backend/data/integrity-backups.
These backups are retained; monitor disk space and archive old backups as needed.

The active dashboard applies the closing-date condition on every request, even
before the daily job. Closing today remains active through the India-time day;
dates before today cannot appear active. Unknown dates stay outside the normal
active view. Expired legitimate records remain available in Archive.

## 1. Push from Windows PowerShell

```powershell
cd E:\lead-opportunity-platform
git add backend/app/main.py backend/app/services/actionable.py backend/app/services/data_integrity.py backend/app/services/deadline_audit.py backend/app/services/deduplication.py backend/app/services/filter_service.py backend/app/services/scheduler.py backend/app/services/scraper_manager.py
git add backend/scripts/merge_db.py backend/scripts/snapshot_db.py backend/tests/test_daily_integrity.py backend/tests/test_scheduler_manual_mode.py docs/ec2-integrity-and-developmentaid.md
git diff --cached --stat
git commit -m "Enforce automatic opportunity identity and deadline maintenance"
git push origin main
```

Only commit these files. Do not add the database, secrets, unrelated verify.json,
or Word documents. The previous one-off dedupe_exact script is not needed for
this automatic workflow. Do not run the old title-based deletion or rekey scripts.

## 2. Deploy in the Ubuntu EC2 terminal

Run one command at a time and stop on errors:

```bash
cd ~/Deployment/lead-opportunity-platform
git pull --ff-only origin main
cd backend
.venv/bin/python -m pytest tests/test_daily_integrity.py tests/test_scheduler_manual_mode.py -q
cd ..
sudo supervisorctl restart lead-scanning-api
```

If pytest is not installed on EC2, the local tests already cover this change;
do not install development packages just to restart production. This release
adds no runtime dependencies and needs no frontend rebuild. The first start
backs up and reconciles legacy keys before the API becomes ready; allow time
for the database scan. Restart when no manual scrape is underway.

```bash
sudo supervisorctl status lead-scanning-api
curl --retry 60 --retry-connrefused --retry-delay 5 --max-time 10 -fsS http://127.0.0.1:8001/api/config
```

For logs, inspect the stdout_logfile/stderr_logfile in the live Supervisor
configuration. Successful runs log `Data integrity:` with rekeyed, merged and
expired counts. The scheduler logs `00:05 Asia/Kolkata`. No cron setup is needed.

## 3. Upload the prepared DevelopmentAid snapshot

Prepared locally: backend/data/developmentaid-ec2-integrity-transfer.db.
It contains 5,792 DevelopmentAid opportunity rows with Active status and a
deadline on/after the export day. This is not the count of new EC2 notices.

In Windows PowerShell, replace the SSH-key placeholder with the same PEM key
used to connect to this EC2 instance:

```powershell
scp -i "C:\PATH\TO\YOUR-EC2-KEY.pem" "E:\lead-opportunity-platform\backend\data\developmentaid-ec2-integrity-transfer.db" ubuntu@15.207.68.78:~/developmentaid-transfer.db
```

The snapshot is a database backup and includes other application tables; keep it
private. The import below reads only the selected opportunity records and does
not import team members, credentials, or notification history.

## 4. Import only new DevelopmentAid notices on EC2

After the new backend has started successfully:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
.venv/bin/python scripts/merge_db.py --source ~/developmentaid-transfer.db --only-source DevelopmentAid --active-only --dry-run
```

Then apply the same import:

```bash
.venv/bin/python scripts/merge_db.py --source ~/developmentaid-transfer.db --only-source DevelopmentAid --active-only
```

The importer backs up the destination, recalculates identity on both sides,
serializes writes with SQLite, and inserts only unseen identities. It leaves
existing EC2 records and approvals unchanged. Repeating the import is safe;
it should report zero new records for the same file. It rechecks dates on the
import day, so rows expiring between export and import are skipped.

For later transfers, create a fresh snapshot with a new filename:

```powershell
cd E:\lead-opportunity-platform\backend
.venv\Scripts\python.exe scripts/snapshot_db.py --output data/developmentaid-next-transfer.db --only-source DevelopmentAid --active-only
```

Upload and import it using the same pattern. Daily server maintenance is
automatic; transferring new laptop data still requires moving that file to EC2.
