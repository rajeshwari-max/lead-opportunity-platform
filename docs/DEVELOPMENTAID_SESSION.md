# DevelopmentAid: connecting a session, localhost to EC2

DevelopmentAid is the only priority source that needs a signed-in session, and
it is the one people get wrong — usually by trying to make it automatic. This
is the whole procedure, including the parts that are deliberately manual and
why.

---

## 1. The rule this whole document exists to keep

**The login is performed by a person, in a real browser, once. Only the result
travels.**

Their sign-in page is protected by reCAPTCHA. A scripted login is blocked by
design, and attempting one is what their terms restrict — so automating it
would put the account at risk rather than save anybody time. There is no
version of this that ends with credentials in the codebase.

Concretely, and without exception:

* No DevelopmentAid email or password appears in any file in this repository,
  in `.env`, in a script argument, in a log line, or in a commit message.
* Nothing types into their login form on a schedule.
* No CAPTCHA is solved, relayed, or worked around.
* The session file is a **live credential**. It is treated like a password: not
  printed, not pasted into a chat, not committed, not emailed.

If the session cannot be established by hand, the correct outcome is that the
source reports `AUTH_REQUIRED` and produces nothing — not that something
clever happens.

---

## 2. What actually moves between the machines

| | |
|---|---|
| Browser profile | `backend/data/devaid_profile/` — created by "Connect account", stays on the machine with a screen |
| Session file | `backend/data/devaid_session.json` — cookies + localStorage, the thing that travels |
| Ignored by git | `backend/data/` and, belt-and-braces, `devaid_session.json` and `*devaid_session*.json` at any path |

The second entry in that ignore list is not redundant. `backend/data/` is
ignored, but `devaid_session.py export` writes to **wherever it is run from**,
and one of these files has already reached the repository root once as an
empty file. This repository is public; a populated one would publish a working
session to anyone who looked.

---

## 3. On your laptop: establish the session

1. Start the app locally and open the dashboard.
2. Click **Connect account** for DevelopmentAid. A real Chrome window opens.
3. Sign in yourself — including the CAPTCHA. Complete any 2FA prompt.
4. Wait until you can see the tenders listing while signed in, then close the
   window.

Confirm it took:

```powershell
cd E:\lead-opportunity-platform\backend
.\.venv\Scripts\python.exe scripts\devaid_session.py status
```

You want all three lines true:

```
session file : ...\backend\data\devaid_session.json
  exists     : True
  connected  : True
checking the live site…
  signed in  : True
```

`signed in : False` with `exists : True` means the session lapsed. Repeat
steps 2–4; nothing else will fix it.

### If Chrome will not start

The profile directory holds a lock while a Chrome using it is open — including
one you forgot about, and including a crashed one. Close every Chrome window
that could be using the profile, then retry. If the lock persists, deleting
`backend/data/devaid_profile/` and reconnecting is safe: it costs one manual
sign-in and nothing else.

---

## 4. Move it to EC2

### The one-command way

```powershell
.\.venv\Scripts\python.exe scripts\devaid_session.py push
```

That exports, copies over SSH, imports on the server, and verifies against the
live site — and it cleans up the temporary copy on **both** machines whether or
not the transfer worked. Defaults target `ubuntu@15.207.68.78` and
`~/Deployment/lead-opportunity-platform/backend`; override with `--host`,
`--remote`, `--python`.

Do it as one command rather than three. The three-step version is the part
people get wrong or half-finish, and a half-done handoff looks exactly like a
working one until a scrape quietly returns nothing.

### The manual way, when `push` cannot reach the host

```powershell
.\.venv\Scripts\python.exe scripts\devaid_session.py export > devaid_session.json
scp devaid_session.json ubuntu@15.207.68.78:/tmp/devaid_session.json
```

Then on the server:

```bash
cd ~/Deployment/lead-opportunity-platform/backend
source .venv/bin/activate
python scripts/devaid_session.py import /tmp/devaid_session.json
rm -f /tmp/devaid_session.json          # not optional
```

And on the laptop, afterwards:

```powershell
Remove-Item devaid_session.json
```

Both deletions matter. That file is a working sign-in for the account, sitting
in a directory somebody will one day `git add -A`.

---

## 5. Confirm the server has it

```bash
cd ~/Deployment/lead-opportunity-platform/backend
source .venv/bin/activate
python scripts/devaid_session.py status
```

`import` already verifies against the live site rather than trusting that the
JSON parsed — an expired session is still well-formed JSON, and reporting
success on one puts you back to a dashboard claiming a working account while
every scrape returns nothing. That is the specific failure this design exists
to prevent, so **do not treat "installed N cookies" as success**; wait for
`Connected.`

---

## 6. Verify the source itself, not just the session

A live session proves you can reach the site. It does not prove the scraper
reads it correctly.

```bash
pgrep -fc "chrome|chromium" || echo 0          # baseline FIRST

python scripts/verify_source.py developmentaid --pages 3 \
  --note "a person-established DevelopmentAid session was used"

pgrep -fc "chrome|chromium" || echo 0          # must match the baseline
```

The `--note` is not decoration. Without it the report records the session
precondition as **unproven**, because a verification nobody can reproduce is
not one.

Coverage needs the site's own number, and only a signed-in person can read it:
open the tenders search with the open filter (`statuses=3`) and no keyword,
read the result count, then pass it in.

```bash
python scripts/verify_source.py developmentaid --pages 5 --official-total 12874 \
  --note "a person-established DevelopmentAid session was used"
```

Without `--official-total` the report says `coverage: unproven` and names what
would prove it. It does **not** divide our count by our count and print 100%.

---

## 7. What the run is allowed to do

The archive walk is the setting that produced 779,856 records found against
55,013 saved in a single scheduled run. It is off by default and must stay off.

| Setting | Default | Meaning |
|---|---|---|
| `LOP_DEVAID_INCLUDE_ARCHIVE` | `false` | Walk the historical archive. One-off backfills only, never on a schedule. |
| `LOP_DEVAID_MAX_SLICES` | `600` | Search partitions per section |
| `LOP_DEVAID_MAX_DURATION_S` | `1800` | 30 min per section |
| `LOP_DEVAID_MAX_RECORDS` | `20000` | Rows handed off per section |

All three caps are one object (`services/walk_budget.py`) and the run reports
which one it hit. A run that stopped at a cap is bounded, not broken — but a
run that hits the same cap every night is a run that is not finishing, and the
number to change is the one it names.

High in-run duplication is **expected here** and is not a defect: the walk
partitions the catalogue into overlapping searches, so the same tender
legitimately arrives several times. Its verification contract allows 60%
duplicates for that reason, and only the unique count means anything.

---

## 8. When it stops working

| Symptom | What it is | What to do |
|---|---|---|
| `signed in : False` on either machine | The session lapsed. They expire; this is normal. | Re-do §3 and §4. Nothing else works. |
| `AUTH_REQUIRED` in the run outcome | No session was installed on the machine that ran it | §4 |
| `SESSION_EXPIRED` | A session was installed and has since lapsed | §3, then §4 |
| `BLOCKED` | A bot wall, not a login problem | Stop. Do not work around it — that is the line this document is about. |
| Rows arrive but links are wrong | A parser problem, not a session problem | `tests/test_parser_fixtures.py`, the DevelopmentAid section |
| Chromium processes survive the run | A browser-lifecycle leak | RUNBOOK §8 |

**Rotate the session whenever** somebody leaves who had access, the file has
been copied anywhere it should not have been, or you are unsure. Rotating costs
one manual sign-in.

---

## 9. Things not to do, and what to do instead

| Don't | Do |
|---|---|
| Put the email and password in `.env` so the server can log in | Establish the session by hand and push it (§3, §4) |
| Automate the CAPTCHA, or use a solving service | Nothing. This is the line. |
| Paste the session JSON into a chat, an issue, or a commit | Move it with `push`, or `scp` and delete both copies |
| Leave `LOP_DEVAID_INCLUDE_ARCHIVE=true` after a backfill | Set it back to `false` in the same sitting |
| Report the source as empty when the session is missing | `AUTH_REQUIRED` — the two mean different things and only one needs a person |
| Claim a coverage figure from our own row count | `--official-total`, read off the site by a signed-in person, or `unproven` |
