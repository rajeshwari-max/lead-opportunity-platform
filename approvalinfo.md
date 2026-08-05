# Approval — implementation notes

How the Approve button works, in the dashboard and in the digest email: what was
added, where it lives, and why each decision was made the way it was.

Approval is the human sign-off that gates everything downstream. Only approved
opportunities are meant to reach the retrieval layer and, after that, the
agentic layer — so this is a trust boundary, not just a UI control.

---

## 1. Flow at a glance

```
                    ┌──────────────────────────────┐
                    │  Opportunity row (unapproved)│
                    └───────────────┬──────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │                                           │
    ┌─────────▼──────────┐                    ┌───────────▼───────────┐
    │ Dashboard button   │                    │ Digest email button   │
    │ POST /opportunities│                    │ GET /approve/{token}  │
    │      /{id}/approve │                    │                       │
    │                    │                    │                       │
    │ guarded by         │                    │ authorised by HMAC    │
    │ require_writable   │                    │ signature in the token│
    │ → 403 on the       │                    │ → works everywhere,   │
    │   read-only mirror │                    │   including the mirror│
    └─────────┬──────────┘                    └───────────┬───────────┘
              │                                           │
              └─────────────────────┬─────────────────────┘
                                    │
                    ┌───────────────▼──────────────┐
                    │ approval_service.set_approved│
                    │  approved / _at / _by        │
                    └───────────────┬──────────────┘
                                    │
                    ┌───────────────▼──────────────┐
                    │ "Approved only" view          │
                    │ GET /opportunities?approved=  │
                    │ → the curated hand-off set    │
                    └──────────────────────────────┘
```

---

## 2. Database — three columns, not one

**`backend/app/database/models.py`**

```python
approved:    Mapped[bool]           = mapped_column(default=False, index=True)
approved_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
approved_by: Mapped[str]            = mapped_column(String(320), default="")
```

A single boolean would have made the button work. Three columns exist because
approval is the gate into the agentic layer, and when something wrong gets
through, the first question asked is *who approved this, and when*. That is
unanswerable after the fact unless it is recorded at the moment of the click.

`approved` is indexed because the "Approved only" view filters on it directly.

### Migration

**`backend/app/database/db.py`** → `_run_migrations()`

SQLite cannot add columns through `create_all`, so each is added conditionally
and the whole function stays safe to re-run on every startup:

```python
cols = columns("opportunities")
if "approved" not in cols:
    conn.exec_driver_sql(
        "ALTER TABLE opportunities ADD COLUMN approved BOOLEAN NOT NULL DEFAULT 0")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_opportunities_approved ON opportunities(approved)")
if "approved_at" not in cols:
    conn.exec_driver_sql("ALTER TABLE opportunities ADD COLUMN approved_at DATETIME")
if "approved_by" not in cols:
    conn.exec_driver_sql(
        "ALTER TABLE opportunities ADD COLUMN approved_by VARCHAR(320) NOT NULL DEFAULT ''")
```

The default is `0` deliberately. All existing rows start unapproved — a
migration must never grant approval retroactively, because that would hand the
agentic layer a "curated" set that nobody actually curated.

---

## 3. Signing — `backend/app/services/approval_service.py` (new)

This module is what makes approval-by-email safe.

The problem it solves: a naive `/approve?id=123` endpoint would let anyone
holding the public dashboard URL walk the id range and approve the entire
database. The read-only mirror exists precisely so that people can look without
touching, and an unauthenticated approve endpoint would hand that power straight
back.

### Token format

```python
payload = {"id": opportunity_id, "by": email, "exp": now + 30 days}
body    = base64url(json(payload))
sig     = base64url(hmac_sha256(secret, body))
token   = f"{body}.{sig}"
```

Three properties matter:

| Property | Why |
|---|---|
| Names exactly one opportunity id | The token cannot be repurposed for any other row |
| Expires (`TOKEN_TTL_SECONDS`, 30 days) | Digests get read late; a link that dies over a long weekend pushes people back to hunting the row down by hand, which defeats the point |
| Verified with `hmac.compare_digest` | Not `==` — a wrong signature cannot be discovered one byte at a time by timing the responses |

### Verification

```python
def read_token(token: str) -> dict:
    body, sig = token.split(".", 1)          # malformed → InvalidToken
    expected = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise InvalidToken("bad signature")
    payload = json.loads(_b64d(body))
    if int(payload.get("exp", 0)) < time.time():
        raise InvalidToken("this approval link has expired")
    return payload
```

### Applying the decision

`set_approved()` keeps the **first** attribution:

```python
if approved and not opp.approved:
    opp.approved_at = datetime.now(timezone.utc)
    opp.approved_by = by or "dashboard"
elif not approved:
    opp.approved_at = None
    opp.approved_by = ""
opp.approved = approved
```

Re-clicking an already-approved row — a forwarded email, a double tap — does not
rewrite who made the decision. The first sign-off is the decision.

---

## 4. Two endpoints, two trust models

**`backend/app/api/routes.py`**

```python
@router.post("/opportunities/{opportunity_id}/approve",
             response_model=OpportunityOut,
             dependencies=[Depends(require_writable)])      # dashboard

@router.get("/approve/{token}", response_class=Response)     # email
```

**The dashboard endpoint** goes through `require_writable`, so the read-only
cloud mirror returns **403**. This is the "shared view is view-only" rule: a
stranger holding the public link cannot change what the team has approved.

**The email endpoint is deliberately exempt from that guard.** A valid signature
proves the request carries a link that this installation generated, for one
named opportunity, addressed to one team member. That is a stronger claim than
"this request did not arrive at the mirror". Gating it on `read_only` would only
have stopped your own team from approving from their inbox, which is the whole
feature.

It returns a small HTML confirmation page rather than JSON, because the person
clicking is in a mail client, not a script:

```python
def _approval_page(heading: str, detail: str, ok: bool, url: str = "") -> str: ...
```

| Outcome | Status | Page |
|---|---|---|
| Valid token, row exists | 200 | "Approved" + the title + a link to the opportunity + **Undo** |
| Tampered / malformed token | 400 | "Link not valid — bad signature" |
| Expired token | 400 | "Link not valid — this approval link has expired" |
| Row deleted since the email | 404 | "Not found" |

### Undo

```python
@router.get("/approve/{token}/undo", response_class=Response)
```

Approving from an email is a single click with no confirmation step, so a
mis-click is easy and the landing page is the only chance to take it back. It
therefore carries an **"Undo — I clicked this by mistake"** button.

The *same token* undoes the approval. That is safe: possession of the token
already granted the power to approve, and undoing is strictly less powerful than
approving. No second secret is needed.

The undo page is symmetrical — undoing by mistake is as possible as approving by
mistake — so it offers **"Approve it after all"**, pointing back at the original
token. The two pages bounce between each other indefinitely, and because
`set_approved()` re-stamps attribution when it transitions from unapproved to
approved, re-approving restores `approved_by` rather than leaving it blank.

Undo works on the read-only mirror for the same reason approval does: the
signature, not the host, is the authorisation.

---

## 5. The email button

**`backend/app/services/email_service.py`** — a new `_approve_cell()` plus an
**Action** column in the digest table.

```python
def _approve_cell(o: Opportunity, member: TeamMember) -> str:
    if o.approved:
        return '<span style="color:#059669;...">✓ Approved</span>'
    url = approve_url(o.id, member.email)
    return ('<table cellpadding="0" cellspacing="0" ...>'
            f'<tr><td style="background:#4f46e5;border-radius:6px;">'
            f'<a href="{url}" style="display:inline-block;padding:7px 14px;...">Approve</a>'
            '</td></tr></table>')
```

The button is a one-cell `<table>` rather than a styled `<a>` because Outlook
ignores padding on inline anchors and would collapse it into bare underlined
text. Rows already approved show `✓ Approved` instead of a button, so a digest
never invites someone to approve the same thing twice.

The link is attributed to the recipient — `approve_url(o.id, member.email)` —
so `approved_by` records the actual person, not just "dashboard".

---

## 6. The dashboard button

**`frontend/src/components/OpportunitiesTable.tsx`**

A display column plus local state:

```tsx
const [pendingApproval, setPendingApproval] = useState<Record<number, boolean>>({});
const [failedApproval, setFailedApproval]   = useState<number | null>(null);

const approved = pendingApproval[o.id] ?? o.approved;   // local overlay on server value
```

The click updates local state immediately so the button responds without waiting
for a refetch of the page. On failure the local entry is **deleted**, so the row
snaps back to whatever the server actually holds, and "Couldn't save" appears
under the button:

```tsx
catch {
  setPendingApproval((m) => { const { [o.id]: _dropped, ...rest } = m; return rest; });
  setFailedApproval(o.id);
}
```

A button that stays green after a failed write is worse than no button at all —
you would believe something had been approved when it had not.

### Undo

The green button has always toggled, but that was only discoverable by guessing.
An approved row now shows an explicit **↩ Undo** link beneath the button:

```tsx
{approved && (
  <button type="button" onClick={() => toggleApproval(o)} className="...">
    <Undo2 className="h-3 w-3" /> Undo
  </button>
)}
```

Both controls call the same `toggleApproval`, so undo inherits the optimistic
update and the roll-back-on-failure behaviour. The tooltip on the green button
now reports *who* approved it and *when*, rather than spending itself explaining
that clicking again undoes.

### Read-only behaviour

When `readOnly` is true the column renders static text (`✓ Approved` or `—`)
with no button, so the mirror never presents a control that would 403. The flag
comes from `GET /api/config` and is passed down from `App.tsx`.

---

## 7. Reading the approved set back

A filter (`approved=true`) and an **Approved only** toggle beside the table
title. In **`backend/app/services/filter_service.py`** it *replaces* the base
query rather than narrowing it:

```python
if getattr(f, "approved", False):
    stmt = select(Opportunity).where(Opportunity.approved.is_(True))
elif getattr(f, "archived", False):
    ...
```

**This was a real bug caught during testing.** The default query restricts rows
to open deadlines, so approved rows were disappearing from the approved view as
their deadlines passed. The approved set is a curation artifact and a hand-off
record — not a view of what is currently biddable — so it deliberately ignores
the live/archived split.

Every other filter still applies below this branch, so "Approved only" composes
normally with country, vertical, source and search.

---

## 8. Configuration

**`backend/app/core/config.py`**

```python
public_base_url: str = "http://localhost:8000"
approval_secret: str = ""
```

`public_base_url` exists because relative links do not work in mail clients; the
email needs an absolute URL that recipients can actually reach.

`approval_secret` has **no default value on purpose**. A hard-coded default
would be a published key, and anyone could then mint valid approval links for
the entire database. It is generated once and written to
`backend/data/.approval_secret`:

```python
def _load_or_create_approval_secret() -> str:
    key_file = BASE_DIR / "data" / ".approval_secret"
    ... read it if present, else secrets.token_urlsafe(32), write with chmod 0600
```

Regenerating it per boot would silently invalidate the approval links in every
digest already sitting in people's inboxes, which is why it is persisted rather
than held in memory. `backend/data/` is already gitignored, so the key is never
committed.

---

## 9. Deployment notes

Two environment variables need setting on Render before the next digest goes
out. Both are in `backend/.env.example`.

| Variable | Why it matters |
|---|---|
| `LOP_PUBLIC_BASE_URL` | Must be the Render URL. Left at `localhost`, every Approve button in every email points at a host the recipient cannot reach, and silently does nothing. |
| `LOP_APPROVAL_SECRET` | Render's free tier has no persistent disk, so the generated key is wiped on each deploy. Without an explicit value, every deploy invalidates the approval links in emails already sent. |

`LOP_READ_ONLY=true` stays as it is on the mirror: it disables scraping,
scheduling and the dashboard's own Approve button, while signed email links
continue to work.

---

## 10. Verification performed

Tested against a copy of the live database (64,649 rows):

- Migration adds all three columns; **0 rows pre-approved**.
- Approve → `approved=true`, `approved_by` and `approved_at` populated.
- Un-approve → flag cleared and attribution reset.
- Approved-only view returns both a live row and a past-deadline row (the
  regression above), and composes correctly with a country filter.
- Email link approves and attributes to the recipient's address; re-approving
  keeps the original attribution.
- Tampered signature → 400. Swapped payload → 400. Garbage → 400. Empty → 400.
  Expired token → 400. Missing row → 404.
- Read-only mode: dashboard POST → **403**; signed email link → **200**;
  signed undo link → **200**.
- Undo round trip: approve → undo → re-approve, with the flag and attribution
  correct at each step and `approved_by` restored on re-approval.
- Tampered undo token → 400. Expired undo token → 400.
- Digest renders one Approve button per unapproved row and `✓ Approved` for
  approved ones.
- Frontend typechecks clean (`tsc --noEmit`, exit 0).

---

## 11. File inventory

**Backend**

| File | Change |
|---|---|
| `app/services/approval_service.py` | **New.** Token minting/verification, `set_approved()` |
| `app/database/models.py` | `approved`, `approved_at`, `approved_by` |
| `app/database/db.py` | Additive migration + index |
| `app/schemas/opportunity.py` | Fields on `OpportunityOut`, new `ApprovalRequest`, `approved` filter |
| `app/api/routes.py` | Approve + undo endpoints, confirmation pages, `approved` query param |
| `app/services/filter_service.py` | Approved-set branch in `_base_statement` |
| `app/services/email_service.py` | `_approve_cell()` + Action column |
| `app/core/config.py` | `public_base_url`, `approval_secret`, key generator |
| `.env.example` | **New** (was referenced by the SMTP error message but absent) |

**Frontend**

| File | Change |
|---|---|
| `src/components/OpportunitiesTable.tsx` | Approve column, explicit Undo link, optimistic state, Approved-only toggle |
| `src/lib/api.ts` | `api.approve()`, `approved` query param |
| `src/lib/types.ts` | `approved` / `approved_at` / `approved_by`, `FilterState.approved` |
| `src/App.tsx` | Passes `readOnly` to the table |

---

## 12. What this sets up next

`approved = true` is now the queryable boundary between the dashboard and
everything after it. The vector-store step documented separately can select on
exactly this flag — with `approved_at` available as a watermark, so a sync job
can pick up only what has been approved since it last ran, and `approved_by`
carried through as metadata.
