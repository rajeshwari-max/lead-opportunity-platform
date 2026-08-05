# Automatic emails — how it works

Everything the platform sends, when it sends it, and where to change it.

Controlled from the **Automatic Emails** panel in the dashboard (right-hand
column, above Team & Lead Routing). No file editing, no restart.

---

## 1. What gets sent

| | Trigger | Contains | Goes to |
|---|---|---|---|
| **Daily digest** | Every day at the configured time | Opportunities matching that person that they have never been sent | Members marked **Auto** |
| **Deadline reminders** | Same daily run | Opportunities closing in exactly 10 / 7 / 2 days | Only the member who was actually sent that opportunity |
| **On-scrape digest** | Immediately after a scrape finishes | Newly found matches only | Members marked **Auto** |
| **Manual send** | The **Send** button | Same as the daily digest | One member |
| **Manual resend** | The **Resend** button | Everything currently matching, including previously sent | One member |

---

## 2. The daily run

One job, `daily-digest`, fires at the configured time and does two things in
order:

```
09:00  ──►  dispatch_service.send_to_all_active()
              for each active member with auto_send:
                  matches_for(member)          # excludes anything already sent
                  send_digest(...)             # grouped by region
                  mark_sent(...)               # only after SMTP succeeds
       ──►  reminder_service.send_due_reminders()
              for each offset in [10, 7, 2]:
                  opportunities closing in exactly that many days
                  that this member was previously sent
                  and has not already been reminded about at this offset
```

**Matching** — a member receives an opportunity when their keywords appear in
its title/summary/vertical/eligibility, its category is in their category list
(empty = all), and it belongs to one of their verticals (empty = all).

**`mark_sent` happens after the SMTP call succeeds**, never before. A failed
send leaves the opportunity unsent so tomorrow's run retries it, rather than
recording it as delivered and losing it silently.

---

## 3. Reminders

`REMINDER_OFFSETS` defaults to **10, 7, 2** days before the deadline, and the
active values come from the dashboard.

Ten days is the first nudge deliberately: seven already forces a rushed
decision on anything needing a written proposal, and ten leaves room to decide
whether to bid at all.

Three rules keep reminders from becoming noise:

1. **Only to people who received the opportunity.** A reminder for something you
   were never sent is meaningless, so reminders join through the sent log.
2. **Exactly N days, not "N or fewer".** `deadline == today + N` — otherwise an
   opportunity closing in 3 days would re-trigger the 10-day reminder every
   single day.
3. **Once per offset, ever.** `ReminderLog` has a unique constraint on
   (member, opportunity, days_before), so a restart or a manual re-run cannot
   send the same nudge twice.

---

## 4. New opportunities

With **"Send as soon as a scrape finishes"** on, `post_scrape_hook` runs when a
scrape completes and sends only genuinely new matches — `matches_for()` excludes
anything already in the sent log, so a scrape that finds nothing new sends
nothing at all.

With it off, new opportunities simply wait for the next daily email. Nothing is
lost either way; the switch only decides *when*.

---

## 5. Who receives what

Each team member has two flags:

- **Active** — off means they receive nothing at all.
- **Auto / Manual** — the toggle beside their name. **Auto** includes them in
  the daily and on-scrape emails. **Manual** means they only ever get a
  deliberate Send or Resend.

Every email is filtered to that person's own keywords, categories and
verticals, so two members rarely receive the same list.

---

## 6. Changing the settings

Dashboard → **Automatic Emails**:

| Control | Effect |
|---|---|
| Send a daily email | Master switch. Off removes the scheduled job entirely. |
| Every day at | Send time. Applied immediately — the next run recalculates on save. |
| Remind this many days before | Toggle any of 14 / 10 / 7 / 3 / 2 / 1. |
| Send as soon as a scrape finishes | On-scrape digest on/off. |
| **Run now** | Fires the daily digest and reminders immediately. |

Settings persist in `backend/data/email_settings.json`. `.env` only supplies the
initial values on first run; after that the dashboard wins, so a UI change is
never silently reverted by an environment variable.

**Run now** is the fastest way to confirm the whole chain works without waiting
until tomorrow morning. It reports how many members were emailed, how many
opportunities went out, and how many reminders were sent.

---

## 7. A bug this replaced

Automatic email had never actually worked on any instance with a scrape
schedule set.

`scheduler.start()` installed the `daily-digest` job and then, a few lines
later, restored the saved scrape schedule by calling `configure()` — which
began with `remove_all_jobs()`. That call removed *every* job, including the
digest that had just been added.

Nothing surfaced the loss. The Schedule card kept showing correct scrape times,
the scraper kept running on schedule, and the digest simply never fired. The
symptom was only ever a negative: emails that didn't arrive.

`configure()` now removes only the scrape job by id.

---

## 8. Requirements

SMTP must be configured or nothing sends — the panel shows a warning when it
isn't. In `backend/.env`:

```ini
LOP_SMTP_USER=you@gmail.com
LOP_SMTP_PASSWORD=<16-character Gmail App Password>
LOP_PUBLIC_BASE_URL=https://your-host        # Approve buttons point here
LOP_DASHBOARD_URL=http://localhost:5173      # "view in dashboard" links
```

`LOP_DASHBOARD_URL` matters in development only, where the API (:8000) and the
UI (:5173) are different origins. In production Nginx serves both from one host
and it can be left blank.

**The scheduler is in-process.** It only fires while the backend is actually
running at that moment. On a laptop that sleeps overnight the 09:00 run is
missed — the scrape schedule catches up on a missed run, the digest does not.
On the EC2 deployment this is a non-issue since the server runs continuously.

---

## 9. Where the code lives

| File | Role |
|---|---|
| `app/services/email_settings.py` | Persisted settings, seeded from `.env` |
| `app/services/scheduler.py` | `daily-digest` cron job, `apply_email_settings()` |
| `app/services/dispatch_service.py` | Digest send loop, `post_scrape_hook` |
| `app/services/reminder_service.py` | Deadline offsets, `ReminderLog` idempotency |
| `app/services/matching_service.py` | Who matches what, `mark_sent` upsert |
| `app/services/email_service.py` | The HTML — region grouping, size budget, buttons |
| `app/api/routes.py` | `GET/PUT /api/email/settings`, `POST /api/email/run-now` |
| `frontend/src/components/AutoEmailPanel.tsx` | The dashboard panel |

---

## 10. Troubleshooting

**Nothing arrives.** Check the panel for the SMTP warning, then press **Run
now** — it reports exactly what it did. `"Nothing new to send"` means the
matching worked and everything had already been sent.

**One person gets nothing.** They are probably set to **Manual**, or inactive,
or their keywords match nothing. The "N new" chip beside their name shows what
they would receive.

**Reminders don't arrive.** Reminders only go to someone who was previously
sent that opportunity. A brand-new opportunity closing in 10 days generates a
digest entry, not a reminder.

**Emails stopped after changing the scrape schedule.** That was the bug in
section 7 — fixed, but if you are running an older build, this is the symptom.
