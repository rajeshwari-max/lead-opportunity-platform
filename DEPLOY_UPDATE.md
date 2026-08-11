# Updating the dashboard on EC2

Run these on the EC2 box (`ssh -i your-key.pem ubuntu@15.207.68.78`).
Copy-paste one block at a time.

---

## 1. Pull the new code

```bash
cd /home/ubuntu/lead-opportunity-platform
git pull
```

If `git pull` reports "Already up to date" but nothing changes on the site,
you are almost certainly hitting the stale-process problem from before — see
step 6.

---

## 2. Rebuild the frontend

```bash
cd ~/Deployment/lead-opportunity-platform/frontend
npm install
npm run build
```

Now publish it. **Run this as one block** — never the `rm` on its own. Emptying
the web root without immediately refilling it leaves Nginx with no `index.html`,
and every visitor gets `403 Forbidden` until the copy happens.

```bash
cd ~/Deployment/lead-opportunity-platform/frontend

WEBROOT=$(sudo nginx -T 2>/dev/null | grep -m1 -oP '(?<=root\s)[^;]+')
echo "Nginx web root: $WEBROOT"

# Refuse to continue if the build didn't produce anything — otherwise the rm
# below would wipe a working site and replace it with nothing.
test -f dist/index.html || { echo "NO BUILD — stop, do not run the rm"; exit 1; }

sudo rm -rf "$WEBROOT"/* && sudo cp -r dist/* "$WEBROOT"/
sudo chown -R www-data:www-data "$WEBROOT"
sudo chmod -R 755 "$WEBROOT"

ls -lah "$WEBROOT"        # must list index.html and assets/
curl -I http://localhost  # must be 200; 403 means the copy didn't happen
```

### If you already see 403 Forbidden

It means the web root is empty. Check, then refill:

```bash
ls -lah /var/www/lead-opportunity-platform     # only "." and ".." = empty
cd ~/Deployment/lead-opportunity-platform/frontend
sudo cp -r dist/* /var/www/lead-opportunity-platform/
sudo chown -R www-data:www-data /var/www/lead-opportunity-platform
curl -I http://localhost
```

Nothing is lost when this happens — the build lives in `frontend/dist` and the
database is untouched. It is only the published copy that went missing.

---

## 3. Restart the backend

```bash
sudo supervisorctl restart lead-api
sleep 8
sudo supervisorctl status lead-api
```

On startup the API now runs a deadline audit automatically. Confirm it fired:

```bash
tail -40 /home/ubuntu/lead-opportunity-platform/backend/logs/app.log | grep -i "deadline\|link repair"
```

Expect something like `deadline audit: sentinels_cleared=148 expired=1674`.
**Only one gunicorn worker may run** — more than one means duplicate
schedulers and duplicate emails:

```bash
pgrep -af gunicorn | wc -l    # should be 2 (one master + one worker)
```

---

## 4. Re-scrape the four sources that were broken

The World Bank pagination fix and the title cleanup only affect **new** scrapes.
Sign in as admin, then:

```bash
curl -s -X POST http://127.0.0.1:8001/api/scrape \
  -H 'Content-Type: application/json' \
  -b "lop_session=<paste your session cookie>" \
  -d '{"sources":["world_bank","adb_tenders","un_partner_portal","undp_procurement"]}'
```

Or just press **Scrape** in the dashboard with those four selected — easier.

---

## 5. Clear the leftovers already in the database

```bash
cd /home/ubuntu/lead-opportunity-platform/backend
source .venv/bin/activate

python scripts/clean_spam.py            # dry run, ~370 rows
python scripts/clean_spam.py --apply

python scripts/clean_non_english.py     # dry run, ~12 rows remain
python scripts/clean_non_english.py --apply
```

Both print what they would remove before you commit to it. Run the dry run
first and read the list.

---

## 6. If nothing seems to change

Twice now the cause has been an orphaned gunicorn still holding port 8001 and
serving week-old code:

```bash
sudo ss -lptn 'sport = :8001'
pgrep -af gunicorn
sudo pkill -f gunicorn
sudo supervisorctl restart lead-api
```

---

## What changed in this update

| Area | Change |
|---|---|
| **Expert Pool** | Non-admins now see only the list of experts and their counts. Connect, Reconnect, Refresh and the session download/upload controls are hidden **and** the matching API routes reject non-admins, so hiding the buttons isn't the only defence. |
| **World Bank pagination** | `os=` is a row offset, not a page number. It was being fed 2, 3, 4… so each "next page" moved down by a single row — nine of every ten results were repeats and the source gave up after 38 rows. Now `os=10, 20, 30…`. |
| **Junk rows** | "Skip to main content", "Procurement Policy", "Projects & Operations" and similar site chrome were being stored as opportunities. Now rejected. |
| **UNDP titles** | Every one of its 588 rows was stored as "Title <the real title>" — the field label leaked into the link text. Now stripped. |
| **UN Partner Portal links** | Pointed at `/api/public/export/projects/N/`, which downloads a file instead of opening the call. Rewritten to `/landing/opportunities/N/`. Existing rows are fixed on startup. |
| **DevelopmentAid** | Now uses your own filtered search URLs, including `languages=92` — DevelopmentAid's English filter. This stops non-English listings **at the source** rather than discarding them after they're stored. Overridable via `LOP_DEVAID_GRANTS_URL` / `LOP_DEVAID_TENDERS_URL`. |
| **Deadlines** | `9999-12-31` ("no closing date") and anything more than 3 years out are treated as "no deadline" rather than a real date, and Active/Expired is recomputed at startup. |

---

## Still outstanding — you should know about these

**None of the four new sources produce a deadline.** All 648 of their rows show
"Ongoing". The deadline is on the detail page in a layout the generic parser
doesn't recognise. This needs one selector per source, and I could not work them
out from here because the sandbox can't reach those sites — the pages need to be
inspected from a machine that can load them.

**Country is empty on all four too**, for the same reason.

**DevelopmentAid still won't scrape from EC2.** Cloudflare blocks the
datacentre IP. The workable options are to ask DevelopmentAid for API access,
or keep DevelopmentAid scraping on your PC and merge periodically with
`scripts/merge_db.py`. I'm not willing to help defeat the bot protection.
