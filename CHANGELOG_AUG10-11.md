# What changed, 10–11 August 2026

Everything below is committed and pushed. The final commit is `1f24966`.

---

## 1. Login sessions and named users

**What you asked for:** a proper login where the user can see which name and ID
they are signed in as, a logout option, and a direct dashboard link in the email.

**What was built** — `backend/app/core/auth.py` (new file):

Sessions are HMAC-SHA256 signed tokens carrying the person's identity, stored in
an HttpOnly cookie so page JavaScript cannot read them.

```python
def make_session_token(email: str, name: str, is_admin: bool) -> str:
    payload = {"email": email, "name": name, "admin": bool(is_admin),
               "exp": int(time.time()) + SESSION_DAYS * 86400}
```

Signature comparison uses `hmac.compare_digest`, not `==`, so a wrong signature
can't be discovered one byte at a time from response timing.

**Two passwords, deliberately:**

| | Set via | Unlocks |
|---|---|---|
| Dashboard | `LOP_DASHBOARD_PASSWORD` | Read opportunities, approve/undo, filters, export |
| Admin | `LOP_ADMIN_PASSWORD` | Everything above **plus** scraper, team routing, email schedule, Expert Pool controls |

Signing in with the admin password gives you both. Signing in with the dashboard
password gives you the first row only.

**Login also checks the team list.** An email not in `team_members` is refused,
so the password alone isn't enough — this is why `scripts/seed_team.py` must run
before you switch the gate on, or you lock yourself out.

**Accepted result:** `LoginScreen.tsx` (the nicer UI you asked for after the
first version), `UserMenu.tsx` showing name + email with a Logout item, and the
digest email carrying an "Open the dashboard" button pointing at
`settings.dashboard_url`.

> **This is the one piece not yet active on EC2.** `auth_required()` returns
> `bool(settings.dashboard_password)`. With no password set it is `False`, the
> API reports `"name":"Local","is_admin":true`, and the app skips the login
> screen on purpose. Section 8 turns it on.

---

## 2. Who sees what — user view vs admin view

| Panel | Signed-in user | Admin |
|---|---|---|
| Opportunities table, filters, charts, KPIs | ✅ | ✅ |
| Approve / Undo | ✅ | ✅ |
| CSV / Excel export | ✅ | ✅ |
| Scraper Control | ❌ hidden | ✅ |
| Team routing | ❌ | ✅ |
| Automatic email schedule | ❌ | ✅ |
| **Expert Pool — the list of experts and counts** | ✅ | ✅ |
| **Expert Pool — Connect / Reconnect / Refresh / session up/download** | ❌ hidden | ✅ |

Your instruction was: *"whatever in expert pool to users only the list of
experts should be visible nothing else."*

Done in two layers, because hiding a button is not a restriction:

- `ExpertsCard.tsx` takes an `isAdmin` prop; every control is gated on it.
- `routes.py` — `/experts/refresh`, `/devaid/status`, `/devaid/connect`,
  `/devaid/session/export` and `/devaid/session/import` now carry
  `Depends(require_admin)` and reject non-admins outright.

`GET /experts` stays open — that is exactly the list users should see.

Why the controls are restricted at all: they act on one shared DevelopmentAid
account. A viewer pressing Refresh spends the team's search quota; a viewer
uploading a session replaces everyone's.

**Note:** while no password is set you are "Local" admin, so you will still see
all the buttons. That is correct behaviour, not a failed deploy.

---

## 3. DevelopmentAid scraping

**The change:** the scraper now uses your own filtered search URLs.

```python
_DEVAID_FILTERS = "hiddenAdvancedFilters=0&sort=deadline.desc&sectors=100,7,11,87&languages=92"
```

`languages=92` is DevelopmentAid's **English filter**. This is the important
part: it stops non-English listings *at the source* rather than storing them and
discarding them afterwards. Overridable without touching code via
`LOP_DEVAID_GRANTS_URL` and `LOP_DEVAID_TENDERS_URL` (added to `config.py`).

**Deadline sentinel fixed.** DevelopmentAid writes `9999-12-31` to mean "no
closing date". It was being parsed as a real date, so 148 rows claimed a
deadline nearly 8,000 years away. `deadline_audit.py` treats `9999-12-31`,
`0001-01-01`, `1970-01-01`, `1900-01-01` and anything beyond 3 years out as "no
deadline" — those rows now display "Ongoing", which is what they are.

**Also cleared:** 1,674 rows sat at `status='Active'` with a deadline already
past. Active/Expired is now recomputed at every startup. The audit is
idempotent — a second run reports all zeros.

**Still unresolved, and I want to be straight about it:** DevelopmentAid will
not scrape from EC2. Cloudflare blocks the datacentre IP. The workable options
are to request API access from DevelopmentAid, or keep DevelopmentAid scraping
on your PC and merge periodically with `scripts/merge_db.py`. I won't help
defeat the bot protection.

---

## 4. The four new sources

| Source | Rows | State |
|---|---|---|
| UNDP Procurement | 588 | Paginating correctly |
| World Bank | 38 | **Was broken** — fixed, needs re-scrape |
| ADB Tenders | 12 | Template correct; low count unexplained |
| UN Partner Portal | 10 | One page only; template correct, likely an SPA route |

**The World Bank bug.** Its URL parameter `os=` is a **row offset**, not a page
index. It was configured as `{page}`, so pagination requested `os=2`, `os=3`,
`os=4` — sliding the window down by a *single row* each time. Nine of every ten
results were repeats, the stale-page counter tripped almost immediately, and the
source stopped after 38 rows while reporting success.

Templates now accept both dialects:

```python
size = int(self.config.get("page_size") or 10)
nxt = template.format(page=page_number + 1, offset=page_number * size)
```

World Bank is set to `os={offset}` with `page_size: 10`, so it now walks
`os=10, 20, 30…`.

**Junk rows rejected.** "Skip to main content", "Procurement Policy", "Projects
& Operations" and similar institutional chrome were being stored as
opportunities — they are plain anchors of plausible length inside the listing
region, so nothing rejected them. Added `_SECTION_TITLE` and ~20 phrases to
`_NAV_WORDS`, anchored so a real call that merely contains the word
("Procurement of Assistive Technology…") survives.

**UNDP titles.** All 588 rows were stored as `Title <the real title>` — the
field label leaked into the anchor text. `clean_title()` strips it.

**UN Partner Portal links.** Pointed at `/api/public/export/projects/N/`, which
downloads a file instead of opening the call. `canonical_link()` rewrites them
to `/landing/opportunities/N/`, applied both at scrape time and to existing rows
at startup.

---

## 5. English-only and classified-only

Your instruction: *"I just want everything in English"* and *"it shouldn't be
visible"* — no toggles, no buttons.

In `filter_service.py` these are unconditional, not parameters:

- Rows whose title contains Hebrew, Arabic, Cyrillic, CJK, Thai or Devanagari
  characters are excluded.
- Rows with no vertical are excluded.

Effect on the live dashboard: **13,510 active → 7,598 shown.**

---

## 6. Deploy reliability

Three separate deploys today appeared to succeed while the server kept serving
old code. All three causes are now fixed:

1. **`tsconfig.tsbuildinfo` was tracked in git.** Every server build rewrote it,
   so `git pull` aborted with "local changes would be overwritten" — and the
   build and restart that followed ran against stale code. Now untracked and
   gitignored.
2. **`rm` without its `cp`.** The web root was emptied and never refilled, so
   Nginx served a bare `403 Forbidden`. Now chained and guarded by a check that
   the build actually produced `dist/index.html`.
3. **Wrong service name.** `supervisorctl restart lead-api` returned "no such
   process" — the program is `lead-scanning-api`. Nothing ever restarted.

`deploy/update.sh` now performs the whole sequence and **stops** on any failure
instead of continuing against stale code.

---

## 7. Security note

`seed_team.py` had your colleagues' work email addresses hard-coded, and this
repository is public. It now reads `scripts/team.txt`, which is gitignored. No
passwords were ever committed.

---

## 8. Deploy — the exact sequence

### On the PC (PowerShell)

```powershell
cd E:\lead-opportunity-platform
git status
git push
```

### On EC2 — one command from now on

```bash
cd ~/Deployment/lead-opportunity-platform && ./deploy/update.sh
```

### Then turn the login on (once)

```bash
cd ~/Deployment/lead-opportunity-platform/backend
```
```bash
cat > scripts/team.txt <<'EOF'
Rajeshwari, rajeshwarichaubey092@gmail.com
Raghu, raghu@catalysts.org
Nitin, nitin@catalysts.org
Swasti, swasti@catalysts.org
CMS Info, info_cms@catalysts.org
EOF
```
```bash
source .venv/bin/activate && python scripts/seed_team.py --apply
```
```bash
read -rsp "Dashboard password: " P && echo && echo "LOP_DASHBOARD_PASSWORD=$P" >> .env && unset P
```
```bash
read -rsp "Admin password: " A && echo && echo "LOP_ADMIN_PASSWORD=$A" >> .env && unset A
```
```bash
printf 'LOP_DASHBOARD_URL=http://15.207.68.78\nLOP_PUBLIC_BASE_URL=http://15.207.68.78\n' >> .env
```
```bash
sudo supervisorctl restart lead-scanning-api && sleep 8 && curl -s http://127.0.0.1:8001/api/config
```

`auth_required` must read `true`. Then Ctrl+Shift+R in the browser.

### Finally, re-scrape

Sign in as admin and press **Scrape** with World Bank, ADB Tenders, UN Partner
Portal and UNDP Procurement selected. Sections 4's fixes only affect new runs.

---

## 9. Verified live on 11 Aug

| Change | Live | Evidence |
|---|---|---|
| New `/api/config` | ✅ | Six fields; previously `{"read_only":false}` alone |
| English + classified filter | ✅ | 7,598 shown, down from 13,510 |
| Deadline audit | ✅ | Earliest Active deadline is today; the 1,674 past-deadline rows are gone |
| Expert Pool gate | ✅ | Code live; invisible to you because you are admin |
| UNPP link rewrite | ✅ | Runs at startup |
| World Bank pagination | ⏳ | Needs re-scrape |
| UNDP title cleanup | ⏳ | Needs re-scrape |
| DevelopmentAid English filter | ⏳ | Needs re-scrape |
| Login screen | ❌ | Needs the `.env` password — section 8 |

---

## 10. Known gaps

**None of the four new sources return a deadline or a country.** All 648 rows
show "Ongoing". The data sits on their detail pages in a layout the generic
parser doesn't recognise; each needs one CSS selector. I could not derive them
because my sandbox cannot reach those sites — they have to be inspected from a
machine that can load them. This is the main remaining item.

**DevelopmentAid cannot scrape from EC2** (section 3).

**~370 spam rows and ~12 non-English rows** are still stored, though filtered
out of view. `scripts/clean_spam.py` and `scripts/clean_non_english.py` remove
them — both dry-run by default, `--apply` to commit.
