# Deep Dive — Classification, SMTP, Key Files, Queries, Running the API

*Companion to PROJECT_HANDBOOK.md. Every number here was measured against the
live database, not estimated.*

---

# Part A — Classification: exactly what happens

## A1. There are four separate classifiers

They run on every scraped row, independently, in `scraper_manager.py`:

| Classifier | File | Output | Type |
|---|---|---|---|
| **Category** | `services/classification.py` | Grant / RFP / Tender / Proposal | single-label |
| **Verticals** | `services/verticals.py` | Livelihood, Health, E4C, Climate, Worker Wellbeing, Innovative Finance | **multi-label** |
| **Work type** | `services/work_type.py` | Research / Implementation | single-label |
| **Study type** | `services/study_type.py` | Baseline / Endline / Evaluation… | single-label |

## A2. The algorithm — weighted keyword scoring

All four use the same idea. Here is the vertical classifier, which is the one
that matters most because it drives routing and visibility:

```python
_TITLE_WEIGHT = 3
_BODY_WEIGHT  = 1
_THRESHOLD    = 2

def classify_verticals(title: str, body: str = "") -> list[str]:
    matched = []
    for vertical in VERTICALS:
        score = 0
        for pat in _COMPILED[vertical]:      # compiled regexes
            if pat.search(title):
                score += _TITLE_WEIGHT       # +3
            if body and pat.search(body):
                score += _BODY_WEIGHT        # +1
            if score >= _THRESHOLD:
                break
        if score >= _THRESHOLD:
            matched.append(vertical)         # multi-label: no "best" pick
    return matched
```

**Why the weights are what they are.** A title is deliberate and short — every
word in it was chosen. A body is long and incidental; the word "health" can
appear once in a 400-word paragraph about something else entirely. So one title
hit (3) clears the threshold (2) on its own, one body hit (1) does not, and two
body hits (2) do.

**Why multi-label.** A solar-irrigation-for-smallholder-farmers grant is
genuinely both Climate/Sustainability **and** Livelihood. Forcing a single label
would make it invisible to one of the two teams who should see it.

### Keyword inventory: 436 compiled patterns

| Vertical | Patterns |
|---|---|
| Innovative Finance | 97 |
| Climate/Sustainability(ESG) | 84 |
| Worker Wellbeing | 82 |
| E4C (Evidence for Change) | 63 |
| Livelihood | 60 |
| Health | 50 |
| **Total** | **436** |

### The category classifier adds a hint

```python
TITLE_WEIGHT = 3;  BODY_WEIGHT = 1;  HINT_WEIGHT = 2
```

`hint` is what the source site claims ("this is our grants page"). It is worth 2
points — influential but **never absolute**, because a grants site does publish
RFPs. Highest score wins; ties break by declaration order.

---

## A3. Would machine learning classify better?

I measured the current performance rather than guessing.

### Current state, live data

```
ACTIVE ROWS: 11,799
  classified   : 6,309  (53%)
  unclassified : 5,490  (47%)   <- hidden from the dashboard

Of the classified rows:
  1 label : 2,989
  2 labels: 2,310
  3 labels:   789
  4+labels:   221
```

47% unclassified looks like a damning verdict on keywords. **It is not**, and
this is the most important thing to understand about the problem.

### Why those 5,490 rows are unclassified

```
unclassified active rows          : 5,490
  no summary text at all          : 3,075  (56%)
  no summary AND no eligibility   : 3,074  (56%)
  has a real summary (>200 chars) : 2,264  (41%)
```

**56% have no text beyond the title.** Their titles look like this:

```
Souter Charitable Trust — Funding Opportunity
Barbara Ward Children's Foundation — Funding Opportunity
Euro Quality Foundation — Funding Opportunity
Applications Invited for Jack Kimmel International Grant Program
```

There is **no topical signal in that text**. Not for keywords, not for a neural
network, not for a human. "Souter Charitable Trust — Funding Opportunity" does
not tell you whether it funds health or climate. No algorithm can extract
information that is not present.

**This is a data-collection problem, not a classification problem.** The fix is
to scrape the detail pages so those rows have summaries at all.

### Where ML genuinely would help — 2,264 rows

The other 41% *do* have rich text that keywords still missed:

| Title | Summary (excerpt) | Should be |
|---|---|---|
| Adidas Foundation Moving for Change | "Sport for Development projects that advance **gender equality**… focus is **girls and women**" | Worker Wellbeing / Livelihood |
| Barbara Ward Children's Foundation | "children who are **seriously or terminally ill**" | Health |
| Global Fund for Women | "we fund bold, ambitious… **gender** justice" | Worker Wellbeing |

Keywords missed these because the vocabulary is paraphrased —
"seriously or terminally ill" instead of "health", "girls and women" instead of
"gender equality". **This is exactly what ML is good at.**

### The honest verdict

| Question | Answer |
|---|---|
| Would ML classify better? | **Yes, on ~2,264 rows** — the 41% of unclassified rows that have real text but paraphrased vocabulary. |
| How much would total coverage improve? | From 53% to roughly **72%**, optimistically. Not to 95%. |
| What is the bigger win? | **Scraping detail pages** to give 3,075 rows any text at all. That is worth more than any model. |
| Should you do ML now? | **No — do the scraping fix first.** ML on rows with no text changes nothing. |

### If you do add ML later, the code is already shaped for it

```python
class Classifier(Protocol):
    """Interface for future ML/LLM classifiers."""
    def classify(self, title: str, description: str, hint: Category | None) -> Category: ...
```

`KeywordClassifier` implements this Protocol. A `MLClassifier` implementing the
same three-argument signature drops in with no other change.

### Trade-offs to state in the presentation

| | Keywords (current) | ML |
|---|---|---|
| Explainability | **Total** — you can point at the exact matched word | Opaque; "the model said so" |
| Debugging a wrong label | Edit one line in the inventory | Retrain, revalidate |
| Training data needed | None | ~500–1,000 hand-labelled examples |
| Handles paraphrase | No | **Yes** |
| Handles new vocabulary | Only after you add the word | **Yes** |
| Cost per row | ~0 | GPU/API cost, or inference latency |
| Time to build | An afternoon | Weeks, including labelling |

**The pragmatic middle path:** keep keywords as the fast first pass, and send
only the rows keywords fail to classify *and* which have >200 characters of text
to an LLM. That is 2,264 rows, one time, then a trickle. Cheap, and it leaves
the explainable path in place for the 53% that already work.

---

# Part B — SMTP and email automation, from first principles

## B1. What SMTP actually is

**SMTP = Simple Mail Transfer Protocol.** It is the standard for *sending* mail
between servers. Defined in 1982 and still the backbone of all email.

The essential distinction:

| Protocol | Direction | Used here? |
|---|---|---|
| **SMTP** | **Sending** mail out | **Yes** |
| IMAP | Reading mail, kept on the server | No |
| POP3 | Reading mail, downloaded off the server | No |

This app only ever *sends*. It never reads an inbox, so it needs SMTP only.

### The terminology

| Term | Meaning |
|---|---|
| **SMTP server** | The machine that accepts your message and relays it. Here: `smtp.gmail.com` |
| **Port 587** | The standard submission port for authenticated sending |
| **Port 465** | Older, implicitly encrypted (SMTPS). Not used here |
| **Port 25** | Server-to-server relay. Blocked by most cloud providers to limit spam |
| **STARTTLS** | Command that upgrades a plain connection to an encrypted one. Without it your password crosses the network in clear text |
| **MIME** | Multipurpose Internet Mail Extensions — how a message carries HTML, attachments and non-English characters. Raw SMTP only understands plain ASCII text |
| **`multipart/alternative`** | A MIME container holding the same message twice (plain text + HTML); the client picks what it can render |
| **App Password** | A 16-character Google-issued password for one application. Not your real password |
| **Envelope vs headers** | The envelope tells the server where to deliver; `To:`/`From:` headers are what you see. They can differ — that is how BCC works |

## B2. What actually happens, line by line

```python
def send_digest(member: TeamMember, opportunities: list[Opportunity]) -> None:
    # 1. Refuse early if not configured — a clear error beats a silent no-op
    if not is_configured():
        raise EmailNotConfiguredError("SMTP is not configured…")

    # 2. Build the MIME container
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(opportunities)} funding opportunities for you"
    msg["From"]    = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"]      = member.email

    # 3. Render the HTML body and attach it, declared as UTF-8
    msg.attach(MIMEText(_digest_html(member, opportunities), "html", "utf-8"))

    # 4. Connect, encrypt, authenticate, send, disconnect
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()                                   # plain -> encrypted
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
```

The conversation on the wire, simplified:

```
app  → gmail : EHLO            "hello, what can you do?"
gmail→ app   : 250-STARTTLS    "I support encryption"
app  → gmail : STARTTLS        "encrypt from here on"
        ...TLS handshake — everything after this is encrypted...
app  → gmail : AUTH LOGIN      username + app password
gmail→ app   : 235 Accepted
app  → gmail : MAIL FROM / RCPT TO / DATA <the MIME message>
gmail→ app   : 250 OK, queued
app  → gmail : QUIT
```

`smtplib` is in Python's standard library — no package to install.

### Why an App Password, not your real one

Google blocks plain password login for apps. An App Password:
- is scoped to one application
- can be revoked without changing your account password
- does not defeat 2-factor authentication on your account

It still grants send access, so it belongs in `.env` (gitignored) and **never**
in the repository.

## B3. The full automation flow

```
APScheduler (inside the FastAPI process)
   │
   ├── 09:00 daily ─────────────────────────────────┐
   │                                                 │
   ▼                                                 ▼
_daily_digest_and_reminders()              send_due_reminders()
   │                                                 │
   ▼                                                 ▼
for each active member with auto_send=True    deadlines at 10/7/2 days
   │                                                 │
   ▼                                                 │
MatchingService.matches_for(member)                  │
   • status = Active                                 │
   • deadline >= today OR NULL                       │
   • keywords LIKE title/summary/vertical/eligibility│
   • category IN member.categories                   │
   • verticals LIKE any of member.verticals          │
   • NOT IN sent_log  ← the anti-duplicate memory    │
   │                                                 │
   ▼                                                 ▼
email_service.send_digest()  ◄───────────────────────┘
   • group by region, South Asia first
   • render HTML under a 95 KB budget
   • sign an HMAC Approve link per opportunity
   • smtplib → STARTTLS → login → send
   │
   ▼
MatchingService.mark_sent()   ← only after a successful send
   writes to sent_log so it is never sent twice
```

### Three details that are easy to get wrong

**1. Mark as sent only after success.** If SMTP fails and you have already
written to `sent_log`, that opportunity is lost forever — it will never be
retried. The write happens strictly after the send returns.

**2. Gmail clips messages over 102 KB.** The renderer targets 95 KB and, if the
content exceeds it, retries with progressively smaller per-region caps
(25 → 15 → 10 → 6 → 4 → 2 rows). Better a trimmed email than a clipped one.

**3. Gmail strips `id` attributes.** The in-email region jump links use
`<a name="r3">`, not `id="r3"`. `name` is deprecated HTML and is used here for
exactly that reason — it survives Gmail's sanitiser, `id` does not.

### Reminders

```python
REMINDER_OFFSETS = (10, 7, 2)   # days before the deadline
```

`reminder_log` has a UNIQUE constraint on `(member_id, opportunity_id,
days_before)`, so a 10-day reminder physically cannot be sent twice — enforced by
the database, not by application logic.

---

# Part C — The important files

## C1. Backend — the five that matter

| File | What it is |
|---|---|
| **`app/main.py`** | **The entry point.** Creates the FastAPI app, mounts routes, runs the auth middleware, starts the scheduler and startup backfills. **Read this first.** |
| **`app/api/routes.py`** | **Every endpoint.** All 38 in one file. Thin controllers only — no business logic. |
| **`app/database/models.py`** | **Every table.** The five SQLAlchemy models. |
| **`app/core/config.py`** | **Every setting.** All tunables and secrets, overridable by `LOP_*` env vars. |
| **`app/services/scraper_manager.py`** | **The engine.** Orchestrates crawling and the normalise→classify→dedupe→save pipeline. |

Everything else is a service module doing one job: `email_service`,
`filter_service`, `matching_service`, `verticals`, `geography`, `links`…

**Tracing any request:** `routes.py` (which endpoint) → the service it calls →
`models.py` (what it touches).

## C2. Frontend — the four that matter

| File | What it is |
|---|---|
| **`src/App.tsx`** | **The root.** Owns all shared state and decides login vs dashboard. |
| **`src/lib/api.ts`** | **Every backend call.** One function per endpoint. Change the API, change this file. |
| **`src/lib/types.ts`** | **The contract.** TypeScript mirrors of the Pydantic schemas. |
| **`src/components/OpportunitiesTable.tsx`** | The main table — the largest and most feature-dense component. |

## C3. Database

| File | What |
|---|---|
| `backend/data/opportunities.db` | **The entire database.** One SQLite file. |
| `backend/app/database/db.py` | Engine, session factory, `init_db()`, WAL mode |
| `backend/app/database/models.py` | Table definitions |

---

# Part D — Database queries: what they are and how to run them

## D1. Where the queries live

There is **no raw SQL** in this project except the FTS5 search. Queries are
written in SQLAlchemy and translated to SQL for you.

| File | Queries it owns |
|---|---|
| `services/filter_service.py` | Listing, filtering, facets, stats |
| `services/matching_service.py` | Who receives which opportunity |
| `services/reminder_service.py` | Which deadlines are due |
| `services/export_service.py` | CSV / Excel rows |

Example — SQLAlchemy on the left, the SQL it generates on the right:

```python
stmt = select(Opportunity).where(
    Opportunity.status == Status.ACTIVE,
    or_(Opportunity.deadline >= date.today(), Opportunity.deadline.is_(None)),
)
```
```sql
SELECT * FROM opportunities
WHERE status = 'Active'
  AND (deadline >= '2026-08-12' OR deadline IS NULL);
```

## D2. See the SQL your Python actually generates

```bash
cd backend
python -c "
from sqlalchemy import select, or_
from datetime import date
from app.database.models import Opportunity, Status
stmt = select(Opportunity).where(
    Opportunity.status == Status.ACTIVE,
    or_(Opportunity.deadline >= date.today(), Opportunity.deadline.is_(None)))
print(stmt)
"
```

To log **every** query the app runs, set `echo=True` on the engine in
`app/database/db.py`. Excellent for learning; far too noisy to leave on.

## D3. Query the database directly

```bash
cd backend
sqlite3 data/opportunities.db
```

Useful meta-commands:

```sql
.tables                       -- list tables
.schema opportunities         -- full CREATE TABLE
.headers on                   -- column names in output
.mode column                  -- aligned columns
.quit
```

### Queries worth demonstrating live

```sql
-- 1. Size of the dataset
SELECT COUNT(*) FROM opportunities;

-- 2. Active vs expired
SELECT status, COUNT(*) FROM opportunities GROUP BY status;

-- 3. Which sources produce the most
SELECT source_website, COUNT(*) AS n
FROM opportunities GROUP BY source_website ORDER BY n DESC LIMIT 10;

-- 4. Classification coverage — the number from Part A
SELECT
  SUM(CASE WHEN verticals = '' OR verticals IS NULL THEN 1 ELSE 0 END) AS unclassified,
  SUM(CASE WHEN verticals <> '' THEN 1 ELSE 0 END)                     AS classified
FROM opportunities WHERE status = 'Active';

-- 5. Multi-label distribution
SELECT LENGTH(verticals) - LENGTH(REPLACE(verticals, ',', '')) + 1 AS labels,
       COUNT(*)
FROM opportunities
WHERE status = 'Active' AND verticals <> ''
GROUP BY labels;

-- 6. Closing in the next 7 days
SELECT title, organization, deadline FROM opportunities
WHERE status = 'Active' AND deadline BETWEEN DATE('now') AND DATE('now','+7 days')
ORDER BY deadline;

-- 7. Prove deduplication works — must return zero rows
SELECT unique_id, COUNT(*) c FROM opportunities
GROUP BY unique_id HAVING c > 1;

-- 8. Scrape history
SELECT source_website, found, saved, errors, status
FROM scrape_runs ORDER BY started_at DESC LIMIT 10;

-- 9. Email audit — what each member has received
SELECT m.name, COUNT(*) AS emails_received
FROM sent_log s JOIN team_members m ON m.id = s.member_id
GROUP BY m.name;

-- 10. Full-text search
SELECT title FROM opportunities
WHERE id IN (SELECT rowid FROM opportunities_fts WHERE opportunities_fts MATCH 'health*')
LIMIT 10;
```

**Query 7 is the one to show in a presentation.** Zero rows returned is a live
proof that deduplication works, and it is the system's most important guarantee.

### If you prefer a GUI
**DB Browser for SQLite** (free, sqlitebrowser.org) — open
`backend/data/opportunities.db`, browse and run SQL visually. Good for a demo.

---

# Part E — Running and testing every API

## E1. Start the system locally

Two terminals.

**Terminal 1 — backend:**
```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on Linux)
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — frontend:**
```bash
cd frontend
npm install
npm run dev
```

Dashboard on `http://localhost:5173`, API on `http://localhost:8000`.
Vite proxies `/api` to `:8000`, which is why the browser sees one origin.

## E2. The best way to explore the API — Swagger

```
http://localhost:8000/docs
```

FastAPI generates this from your type hints. **Every endpoint is listed, with its
parameters, and a "Try it out" button that executes a real request.** No Postman
needed, and nothing to maintain — it cannot drift from the code.

`http://localhost:8000/redoc` is the same content in a reference layout.

## E3. Calling endpoints from the command line

```bash
# health
curl http://localhost:8000/api/health

# capability + identity probe
curl http://localhost:8000/api/config

# first page of opportunities
curl "http://localhost:8000/api/opportunities?page=1&page_size=5"

# filtered
curl "http://localhost:8000/api/opportunities?verticals=Health&categories=Grant"

# search
curl "http://localhost:8000/api/opportunities?search=climate"

# facet values (narrowed by the filters you pass)
curl "http://localhost:8000/api/filters?verticals=Health"

# KPI numbers
curl http://localhost:8000/api/stats

# team
curl http://localhost:8000/api/team

# live scrape progress
curl http://localhost:8000/api/progress
```

### Endpoints that need a session

```bash
# sign in, keep the cookie
curl -c cookies.txt -X POST http://localhost:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@catalysts.org","password":"YOUR_PASSWORD"}'

# reuse it
curl -b cookies.txt -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"sources":["world_bank"]}'

# email a chosen set
curl -b cookies.txt -X POST http://localhost:8000/api/opportunities/send \
  -H "Content-Type: application/json" \
  -d '{"opportunity_ids":[1,2,3],"member_ids":[1]}'
```

On Windows PowerShell use `Invoke-RestMethod` instead:

```powershell
Invoke-RestMethod http://localhost:8000/api/opportunities?page_size=3 | ConvertTo-Json -Depth 3
```

## E4. Testing a scraper on its own

```bash
cd backend
python -c "
import asyncio, sys; sys.path.insert(0,'.')
import app.scrapers
from app.scrapers.registry import get_scrapers
sc = get_scrapers(['world_bank'])[0]
print(sc.display_name, '->', sc.start_url)
"
```

## E5. Testing classification interactively

This is a strong live demo — it shows the reasoning, not just the answer:

```bash
cd backend
python -c "
from app.services.verticals import classify_verticals
from app.services.work_type import classify_work_type
for t in ['Solar irrigation for smallholder farmers',
          'Baseline survey on maternal health outcomes',
          'Souter Charitable Trust — Funding Opportunity']:
    print(f'{t[:45]:47} -> {classify_verticals(t)} | {classify_work_type(t)}')
"
```

The third line returns `[]`, which demonstrates the data problem from Part A far
better than any slide.

## E6. Testing email without sending

```bash
cd backend
python -c "
from app.services import email_service
print('SMTP configured:', email_service.is_configured())
"
```

To preview the HTML without sending, call `_digest_html(member, opportunities)`
and write the result to a file, then open it in a browser.

---

# Part F — Two-minute summary for the presentation

- **Classification is 436 weighted regexes across 6 verticals**, multi-label,
  title hits worth 3× body hits.
- **53% of active rows classify.** Of the 47% that don't, **56% have no text at
  all** — that is a scraping gap, not a model gap. ML would help on the
  remaining 41%, lifting coverage to roughly 72%. **Fix the scraping first.**
- **SMTP is the send-only mail protocol.** The app connects to `smtp.gmail.com`
  on port 587, upgrades to encryption with STARTTLS, authenticates with a
  revocable App Password, and sends a MIME HTML message. It never reads mail.
- **`sent_log` and `reminder_log` prevent duplicate emails at the database
  level**, and rows are marked sent only after SMTP succeeds.
- **Read `main.py`, `routes.py`, `models.py`, `config.py`,
  `scraper_manager.py`** and you have the backend.
- **`/docs` is a live, self-updating, executable API reference** generated from
  the type hints.
