# Lead Scanning Platform — EC2 Deployment Guide

Written to match the PH-EBS deployment pattern: Nginx serves the built React
files, Supervisor keeps a Gunicorn process alive, and the two halves deploy
independently.

Steps marked **[fresh box]** are only needed if this is a new EC2 instance. If
you are deploying alongside PH-EBS, Nginx and Supervisor are already installed
and configured — skip those and nothing about `ebs-api-backend` is touched.

---

## Overview

| | PH-EBS | Lead Scanning Platform |
|---|---|---|
| Backend | Django REST Framework (WSGI) | FastAPI (**ASGI**) |
| Worker class | Gunicorn default | `uvicorn.workers.UvicornWorker` |
| Database | MongoDB | SQLite file on disk |
| Supervisor program | `ebs-api-backend` | `lead-scanning-api` |
| Internal port | (existing) | `127.0.0.1:8001` |
| Web root | `/var/www/ebs-react-frontend/dist` | `/var/www/lead-scanning/dist` |
| Frontend | React (Vite) | React (Vite) — identical procedure |

```
Browser
   │
   ▼
Nginx  (leads.yourdomain.com)
   ├──────────────► /var/www/lead-scanning/dist     static React
   └── /api/ ─────► 127.0.0.1:8001  Gunicorn + UvicornWorker
                          │
                          ▼
                    FastAPI  ──► SQLite  (backend/data/opportunities.db)
                          │
                          └────► Playwright / Chromium  (scrapers)
```

### The one difference that matters

The API **must run with a single worker.** The app starts an APScheduler
instance and runs scrapers as in-process threads, so every extra Gunicorn worker
is another full copy of both. Four workers would mean four simultaneous scrapes
of the same sources, four digest emails to every team member per schedule tick,
and four processes fighting over one SQLite write lock.

This is already set in `deploy/gunicorn.conf.py`. Do not raise it.

---

## 1. Prerequisites

- Ubuntu EC2 instance, **t3.small or larger**. Chromium needs roughly 1 GB while
  scraping; a t3.micro will be OOM-killed mid-run.
- At least 8 GB free disk. The database is ~65,000 rows and grows.
- Security group allowing inbound **80** and **443** from anywhere, and **22**
  from your IP. Port 8001 must **not** be open — Nginx is the only way in.
- A DNS **A record** for `leads.yourdomain.com` pointing at the instance's
  Elastic IP. Create this before requesting a certificate.

---

## 2. Install system packages **[fresh box]**

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx supervisor git curl

# Node 20 for the Vite build
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

node -v && npm -v && python3 -V
```

---

## 3. Get the code

```bash
mkdir -p /home/ubuntu/Deployment
cd /home/ubuntu/Deployment
git clone <your-repo-url> lead-scanning
cd lead-scanning
mkdir -p logs
```

The directory `/home/ubuntu/Deployment/lead-scanning` is referenced by every
config file in `deploy/`. If you put it elsewhere, update the paths in
`deploy/gunicorn.conf.py`, `deploy/supervisor-lead-scanning.conf` and
`deploy/deploy.sh`.

---

## 4. Backend environment

```bash
cd /home/ubuntu/Deployment/lead-scanning/backend
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Playwright browsers

The scrapers drive a real browser for JavaScript-rendered sources. This is the
step most commonly missed — the API starts perfectly without it and only fails
later, when a JS source is scraped.

```bash
playwright install chromium
sudo $(pwd)/.venv/bin/playwright install-deps chromium
```

`install-deps` needs sudo because it installs system shared libraries
(`libnss3`, `libatk`, fonts). Without them Chromium exits immediately with a
missing-library error that does not name the scraper.

### Configuration

```bash
cp .env.example .env
nano .env
```

Set at minimum:

```ini
LOP_SMTP_USER=you@gmail.com
LOP_SMTP_PASSWORD=<16-char Gmail App Password>

# Must be the public URL, or every Approve button in every digest points at a
# host the recipient cannot reach.
LOP_PUBLIC_BASE_URL=https://leads.yourdomain.com

# Where the dashboard UI lives. On EC2 Nginx serves the UI and the API from the
# same host, so this is the same value and could be left blank — set it anyway
# so it is explicit. (In local development they differ: API :8000, UI :5173.)
LOP_DASHBOARD_URL=https://leads.yourdomain.com

# Generate once and paste. Losing it invalidates approval links in emails
# already sent.
LOP_APPROVAL_SECRET=<python3 -c "import secrets;print(secrets.token_urlsafe(32))">

# This instance scrapes, so it is NOT a read-only mirror.
LOP_READ_ONLY=false
```

```bash
chmod 600 .env      # contains an app password
```

`.env` is gitignored and must never be committed.

### Initialise the database

```bash
cd /home/ubuntu/Deployment/lead-scanning/backend
python -c "from app.database.db import init_db; init_db(); print('schema ready')"
```

To carry your existing data across instead of starting empty, copy the SQLite
file up from your PC **before** starting the service:

```bash
# from your Windows machine
scp -i key.pem E:\lead-opportunity-platform\backend\data\opportunities.db \
    ubuntu@<elastic-ip>:/home/ubuntu/Deployment/lead-scanning/backend/data/
```

Stop the service first if it is already running — copying over a live SQLite
file can capture a half-written transaction. Also copy `opportunities.db-wal`
and `-shm` if they exist, or checkpoint the database before copying.

---

## 5. Supervisor

```bash
sudo cp /home/ubuntu/Deployment/lead-scanning/deploy/supervisor-lead-scanning.conf \
        /etc/supervisor/conf.d/lead-scanning.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status
```

Expected — and note `ebs-api-backend` is untouched:

```
ebs-api-backend      RUNNING   pid 1234, uptime 12 days
lead-scanning-api    RUNNING   pid 5678, uptime 0:00:12
```

Confirm the API answers before involving Nginx:

```bash
curl -s http://127.0.0.1:8001/api/config
# {"read_only":false}
```

If it does not start:

```bash
tail -50 /home/ubuntu/Deployment/lead-scanning/logs/supervisor-err.log
```

**Check for a port clash first.** If Django's Gunicorn already holds 8001,
change `bind` in `deploy/gunicorn.conf.py` and `proxy_pass` in the Nginx block
to match:

```bash
sudo ss -ltnp | grep 800
```

---

## 6. Frontend build

```bash
cd /home/ubuntu/Deployment/lead-scanning/frontend
npm ci
npm run build
```

No API URL configuration is needed. The app calls `/api/...` relative to its own
origin, and Nginx proxies that to the backend — so it is same-origin and CORS
never comes into play.

**A `t3.micro` will often be OOM-killed during `npm run build`.** If that
happens, either build on a larger instance type or add swap:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
```

Copy the build into the web root:

```bash
sudo mkdir -p /var/www/lead-scanning/dist
sudo rm -rf /var/www/lead-scanning/dist/*
sudo cp -r dist/* /var/www/lead-scanning/dist/
sudo chown -R www-data:www-data /var/www/lead-scanning
```

As in the PH-EBS guide: `npm run build` **does not deploy anything**. It only
produces `dist/`. The copy is the deployment.

---

## 7. Nginx

```bash
sudo cp /home/ubuntu/Deployment/lead-scanning/deploy/nginx-leads.conf \
        /etc/nginx/sites-available/leads
sudo nano /etc/nginx/sites-available/leads     # set server_name
sudo ln -s /etc/nginx/sites-available/leads /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Because the block sets `server_name`, Nginx routes by Host header and the two
sites coexist. PH-EBS keeps serving its own domain unchanged.

Verify the right root is in effect:

```bash
sudo nginx -T | grep -A2 "server_name leads"
```

---

## 8. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d leads.yourdomain.com
```

Certbot rewrites the block to listen on 443 and adds an HTTP redirect. Renewal
is automatic via a systemd timer; confirm with `sudo certbot renew --dry-run`.

Once HTTPS is live, make sure `LOP_PUBLIC_BASE_URL` in `.env` uses **https://**,
then `sudo supervisorctl restart lead-scanning-api`. Approval links are built
from that value, and an `http://` link on an HTTPS-only host will fail.

---

## 9. Scheduling scrapes

Set the schedule in the dashboard's Scraper panel — it persists across restarts
and is driven by APScheduler inside the single worker. No cron entry is needed.

Start with one source to confirm the browser stack works end to end before
scheduling everything:

```bash
curl -X POST http://127.0.0.1:8001/api/scrape \
     -H 'Content-Type: application/json' \
     -d '{"sources":["fundsforngos"],"verticals":[]}'

curl -s http://127.0.0.1:8001/api/progress
```

Then watch the log:

```bash
tail -f /home/ubuntu/Deployment/lead-scanning/logs/*.log
```

### DevelopmentAid will not work here

DevelopmentAid's grants and tenders sit behind a login, and that session is
established by signing in through a **visible browser window**. A headless EC2
instance has no display, so it cannot complete that step. The other ~80 sources
scrape normally.

Options, in order of preference:

1. Keep running DevelopmentAid on your PC and copy the database up periodically.
2. Run the scrape on your PC and let EC2 handle everything else.

Do not attempt to script the credential login — that is exactly the automation
their subscription terms restrict, and it risks your organisation's account.

---

## 10. Routine deployment

After the first setup, use the script:

```bash
cd /home/ubuntu/Deployment/lead-scanning
git pull

./deploy/deploy.sh              # both halves
./deploy/deploy.sh backend      # Python changed
./deploy/deploy.sh frontend     # React changed
```

It refuses to restart on a failed import and refuses to reload on an invalid
Nginx config, so a bad deploy leaves the running site alone.

### By hand

**Backend** (Python changed):

```bash
sudo supervisorctl restart lead-scanning-api
sudo supervisorctl status lead-scanning-api
```

**Frontend** (React changed):

```bash
cd ~/Deployment/lead-scanning/frontend
npm run build
sudo rm -rf /var/www/lead-scanning/dist/*
sudo cp -r dist/* /var/www/lead-scanning/dist/
sudo nginx -t && sudo systemctl reload nginx
```

Then hard-refresh: **Ctrl + Shift + R**. Vite emits content-hashed bundles, so
a normal refresh can keep serving the cached old one.

---

## 11. Verifying a deployment

| Check | Command / action |
|---|---|
| Process alive | `sudo supervisorctl status lead-scanning-api` |
| Gunicorn running | `ps -ef \| grep gunicorn` — expect the `.venv/bin/gunicorn` path |
| API answering | `curl -s http://127.0.0.1:8001/api/config` |
| Through Nginx | `curl -sI https://leads.yourdomain.com/api/config` |
| Correct bundle | DevTools → Network → the `index-XXXX.js` filename changed |
| Data present | `curl -s '.../api/stats' \| head -c 200` |

---

## 12. Troubleshooting

**Backend changes not visible.** Confirm you edited the deployment directory,
not a local copy; restart Supervisor; check the response in the Network tab.
Same checklist as PH-EBS.

**Frontend changes not visible.** Confirm `npm run build` succeeded, that
`dist/` holds the new files, that they were copied into `/var/www/lead-scanning/dist`,
that Nginx reloaded, and hard-refresh. If DevTools still shows the old
`index-XXXX.js`, the copy did not happen.

**502 Bad Gateway.** The API is down or on a different port.
`sudo supervisorctl status`, then `logs/supervisor-err.log`.

**504 Gateway Timeout on export or send.** A member with thousands of matches
takes a while. The block already allows 300s; raise `proxy_read_timeout` if
needed.

**"database is locked".** Something else is writing to the SQLite file —
usually a stray `uvicorn` started by hand. `ps -ef | grep uvicorn` and kill it;
Supervisor should own the only process.

**Scrape starts then dies.** Almost always Chromium. Check free memory during a
run (`free -h`), and confirm `playwright install-deps chromium` was run.

**Emails send but Approve buttons do nothing.** `LOP_PUBLIC_BASE_URL` is still
`localhost`. Fix `.env`, restart, and resend using the Resend button.

---

## 13. Backups

The entire application state is one SQLite file. Back it up on a schedule:

```bash
sudo crontab -e
```

```cron
0 2 * * * sqlite3 /home/ubuntu/Deployment/lead-scanning/backend/data/opportunities.db \
  ".backup '/home/ubuntu/backups/opportunities-$(date +\%F).db'" \
  && find /home/ubuntu/backups -name 'opportunities-*.db' -mtime +14 -delete
```

Use `.backup` rather than `cp` — it is safe against a database being written to
at that moment, which a plain copy is not.

---

## Key takeaways

- Backend and frontend deploy independently, exactly as with PH-EBS.
- FastAPI needs `UvicornWorker`; the WSGI default will not start it.
- **One worker only** — more would duplicate the scheduler and the digest emails.
- `npm run build` does not deploy. The copy into `/var/www` is the deployment.
- Playwright needs both `install chromium` and `install-deps chromium`.
- DevelopmentAid cannot be scraped from a headless server; keep it on your PC.
