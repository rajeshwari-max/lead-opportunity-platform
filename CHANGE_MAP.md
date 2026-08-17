# Change Map — "They want X changed. Where do I go?"

*Point at anything on the dashboard and this tells you the file, the function,
and whether the backend needs touching too.*

---

## The one rule that answers 80% of requests

Every visible thing has **two possible homes**:

| The request is about… | Change the… | Restart needed? |
|---|---|---|
| How it **looks** — wording, colour, order, how many are shown | **Frontend** (`.tsx`) | No — rebuild frontend |
| **Which data** appears — the filter, the count, the calculation | **Backend** (`.py`) | Yes — restart the API |

**Ask yourself: "is the data already on screen, or does the server have to send
something different?"** If the number 8 is on screen and they want 15, the
server has to send 15 — that's backend. If they want the same 8 in red instead
of amber, that's frontend.

---

# Worked example: "Change the Upcoming Deadlines card"

This is the example you asked for, done end to end. Follow the same five steps
for anything else.

## Step 1 — Find the words on screen

Search the frontend for text you can see:

```bash
cd frontend
grep -rn "Upcoming Deadlines" src/
```
```
src/components/ChartsRow.tsx:110:      <ChartCard title="Upcoming Deadlines">
```

**Always start here.** Visible text is the fastest route into unfamiliar code.

## Step 2 — Read what's there

`frontend/src/components/ChartsRow.tsx`, line 110:

```tsx
<ChartCard title="Upcoming Deadlines">
  <ul className="max-h-[230px] space-y-2 overflow-y-auto pr-1">
    {stats.upcoming_deadlines.map((o) => (
      <li key={o.id} className="flex items-center justify-between gap-3 text-sm">
        <a href={o.opportunity_url} target="_blank" rel="noreferrer"
           className="truncate hover:text-primary hover:underline" title={o.title}>
          {o.title}
        </a>
        <span className="shrink-0 text-xs font-medium text-amber-500">
          {formatDate(o.deadline)}
        </span>
      </li>
    ))}
  </ul>
</ChartCard>
```

Three things to notice, because they generalise:

- `stats.upcoming_deadlines` — the data **comes from the server**. The component
  only displays it.
- `text-amber-500`, `text-sm` — Tailwind classes. Appearance lives here.
- `formatDate(o.deadline)` — a helper in `src/lib/utils.ts`.

## Step 3 — Decide: frontend or backend?

| They ask for | Where | Why |
|---|---|---|
| "Make the dates red" | Frontend | Pure styling |
| "Say 'Closing Soon' instead" | Frontend | Just the label |
| "Show the funder name too" | Frontend | `o.organization` is already in the data |
| "Show 15 instead of 8" | **Backend** | Server only sends 8 |
| "Only the next 30 days" | **Backend** | The query decides which rows |
| "Include ongoing ones" | **Backend** | They're excluded by the query |

## Step 4a — If it's frontend

**"Make the dates red and show the funder"** — edit that one block:

```tsx
<li key={o.id} className="flex items-center justify-between gap-3 text-sm">
  <div className="min-w-0">
    <a href={o.link} target="_blank" rel="noreferrer"
       className="block truncate hover:text-primary hover:underline" title={o.title}>
      {o.title}
    </a>
    {/* NEW: the funder was already in the payload, just never rendered */}
    <span className="text-xs text-muted-foreground">{o.organization}</span>
  </div>
  {/* CHANGED: amber -> red */}
  <span className="shrink-0 text-xs font-medium text-red-500">
    {formatDate(o.deadline)}
  </span>
</li>
```

Then:
```bash
cd frontend && npm run dev      # check at localhost:5173
```

## Step 4b — If it's backend

**"Show 15 instead of 8."** Find where the data is built:

```bash
cd backend
grep -rn "upcoming_deadlines" app/
```
```
app/services/filter_service.py:307:  upcoming_deadlines=[OpportunityOut.model_validate(o) for o in upcoming],
app/schemas/opportunity.py:163:     upcoming_deadlines: list[OpportunityOut]
```

`filter_service.py` around line 287:

```python
upcoming_ids = select(active.c.id).where(active.c.deadline >= date.today())
upcoming = self.db.execute(
    select(Opportunity)
    .where(Opportunity.id.in_(upcoming_ids))
    .order_by(Opportunity.deadline.asc()).limit(8)   # <- change 8 to 15
).scalars().all()
```

**"Only the next 30 days":**

```python
from datetime import timedelta

upcoming_ids = select(active.c.id).where(
    active.c.deadline >= date.today(),
    active.c.deadline <= date.today() + timedelta(days=30),   # NEW
)
```

Then restart:
```bash
uvicorn app.main:app --reload --port 8000   # --reload restarts on save in dev
```

## Step 5 — If you added a NEW field, change three files

Adding something the server doesn't yet send — say `days_left` — touches three
places, **and they must agree**:

**1. Backend schema** — `app/schemas/opportunity.py`
```python
class StatsOut(BaseModel):
    ...
    upcoming_deadlines: list[OpportunityOut]
    days_until_next: int = 0        # NEW
```

**2. Backend logic** — `app/services/filter_service.py`
```python
return StatsOut(
    ...,
    days_until_next=(upcoming[0].deadline - date.today()).days if upcoming else 0,
)
```

**3. Frontend type** — `frontend/src/lib/types.ts`
```typescript
export interface Stats {
  ...
  upcoming_deadlines: Opportunity[];
  days_until_next: number;          // NEW — must match the backend name exactly
}
```

Then use it in the component. **Miss step 3 and TypeScript fails the build** —
which is the point. A mismatch becomes a compile error instead of `undefined`
appearing on the page.

---

# The lookup table — every visible element

## Top bar

| What you see | File | Notes |
|---|---|---|
| "Lead Scanning Platform" + logo | `components/Header.tsx` | |
| Search box | `components/Header.tsx` | Writes `filters.search`; backend does FTS5 in `filter_service._search_ids()` |
| CSV / Excel buttons | `components/Header.tsx` | Columns are backend: `services/export_service.py` `_COLUMNS` |
| "Last updated" | `components/Header.tsx` | From `stats.last_scraped` |
| Dark-mode toggle | `components/Header.tsx` | |
| Name, admin badge, logout | `components/UserMenu.tsx` | Identity from `GET /api/config` |

## KPI cards

| What you see | File | Backend |
|---|---|---|
| The 5 cards | `components/StatCards.tsx` | `filter_service.stats()` |
| "Active Opportunities" number | `StatCards.tsx` | `StatsOut.total_active` |
| "New Today" | `StatCards.tsx` | `StatsOut.todays_new` |
| What a click filters to | `StatCards.tsx` | Sets `FilterState` |

## Charts row

| What you see | File | Backend |
|---|---|---|
| Donut "By Category" | `components/ChartsRow.tsx` | `StatsOut.by_category` |
| Bars "By Region" | `ChartsRow.tsx` | `StatsOut.by_region` |
| Bars "By Vertical" | `ChartsRow.tsx` | `StatsOut.by_vertical` |
| **"Upcoming Deadlines"** | `ChartsRow.tsx` **line 110** | `filter_service.py` **line 287** |
| Chart colours | `ChartsRow.tsx` | Recharts `fill=` props |

## Filters sidebar

| What you see | File | Backend |
|---|---|---|
| Whole panel | `components/FiltersSidebar.tsx` | `GET /api/filters` |
| Which sections and their order | `FiltersSidebar.tsx` **line 34** | — |
| Options inside each | — | `filter_service.facets()` |
| "Clear" | `FiltersSidebar.tsx` | Resets to `emptyFilters` |
| Deadline date pickers | `FiltersSidebar.tsx` | `deadline_after` / `deadline_before` |

To **add a filter section**, edit this list:
```tsx
{ key: "categories", title: "Category",       options: facets.categories },
{ key: "verticals",  title: "Vertical",       options: facets.verticals },
{ key: "sources",    title: "Source Website", options: facets.sources, searchable: true },
```

## Opportunities table

| What you see | File | Notes |
|---|---|---|
| Everything about the table | `components/OpportunitiesTable.tsx` | The biggest component |
| A column's content | Its `col.accessor(...)` block | |
| Column **order** | Order in the `columns` array | |
| Column **width** | `size:` on that column | |
| Column **name** | `header:` on that column | |
| Source / Type dropdowns | Same file, in `CardHeader` | |
| "Approved only" button | Same file, `CardHeader` | |
| ₹ INR toggle | Same file; rates in `lib/money.ts` | |
| Row expansion panel | Same file, the `isOpen &&` block | |
| Checkbox column | Same file, `col.display({ id: "select" })` | |
| "Email these" bar | `components/SendSelectionBar.tsx` | `POST /api/opportunities/send` |
| Rows per page | Same file, page-size `<select>` | |
| **Which rows appear at all** | — | **`filter_service._base_statement()`** |

## Right-hand panels

| What you see | File | Backend |
|---|---|---|
| Scraper Control | `components/ScraperPanel.tsx` | `POST /api/scrape`, `GET /api/progress` |
| Schedule dropdown | `ScraperPanel.tsx` | `PUT /api/schedule` → `services/scheduler.py` |
| Automatic Emails | `components/AutoEmailPanel.tsx` | `services/email_settings.py` |
| Reminder day chips | `AutoEmailPanel.tsx` | `reminder_service.REMINDER_OFFSETS` |
| Team & Lead Routing | `components/TeamPanel.tsx` | `/api/team` |
| Expert Pool | `components/ExpertsCard.tsx` | `/api/experts` |

## Login page

| What you see | File |
|---|---|
| The whole screen | `components/LoginScreen.tsx` |
| Who may sign in | `app/core/auth.py` → `domain_allowed()` |

## The digest email

| What you see | File |
|---|---|
| Everything in the email | `app/services/email_service.py` |
| Subject line | `send_digest()` |
| Region grouping / order | `_REGION_ORDER`, `_group_by_region()` |
| Row layout | `_digest_rows()` |
| Approve button | `_approve_cell()` |
| **Who receives what** | `app/services/matching_service.py` |
| Send time | `app/services/scheduler.py` |

---

# Common requests → exact answer

### "Show more/fewer upcoming deadlines"
`backend/app/services/filter_service.py` line 291 → `.limit(8)`

### "Add a column to the table"
`frontend/src/components/OpportunitiesTable.tsx` → new `col.accessor("field", {...})`
in the `columns` array, positioned where you want it. If the field isn't in
`Opportunity` in `lib/types.ts`, add it there **and** to `OpportunityOut` in
`backend/app/schemas/opportunity.py`.

### "Change the vertical names"
`backend/app/services/verticals.py` → the `VERTICAL_*` constants.
**Then run a backfill**, or every existing row keeps the old label and filtering
by the new name silently returns nothing.

### "Add a keyword to a vertical"
`backend/app/services/keyword_inventory.py`. `backfill_verticals()` re-runs at
startup and re-classifies **every** row, so a restart applies it retroactively.

### "Change the email send time"
Dashboard → Automatic Emails. No code. (Default lives in `config.py`
`digest_hour`.)

### "Add a new website to scrape"
`backend/app/scrapers/sources.json` → one JSON entry. No code needed unless it
has a login wall, POST pagination or heavy JavaScript.

### "Change the reminder days"
`backend/app/services/reminder_service.py` → `REMINDER_OFFSETS = (10, 7, 2)`

### "Change what CSV exports"
`backend/app/services/export_service.py` → `_COLUMNS` **and** the matching row
tuple below it. They must stay in the same order.

### "Change a colour"
Find the component, edit the Tailwind class: `text-amber-500` → `text-red-500`.
Theme-wide colours are CSS variables in `frontend/src/index.css`.

### "Sort by something else by default"
`frontend/src/lib/types.ts` → `emptyFilters.sort_by`.

---

# The workflow, every time

```bash
# 1. Find it by its visible text
grep -rn "the words on screen" frontend/src/

# 2. Frontend or backend? Is the data already there, or must the server change?

# 3. Edit

# 4. Check locally
cd frontend && npm run dev                            # UI change
cd backend && uvicorn app.main:app --reload --port 8000   # API change

# 5. Type-check before committing — catches a frontend/backend mismatch
cd frontend && npx tsc --noEmit

# 6. Ship
git add -A && git commit -m "what changed" && git push
# then on EC2:  bash deploy/update.sh
```

---

# Three traps

**1. Changing the frontend when the data isn't there.** If `stats.upcoming_deadlines`
holds 8 items, no amount of `.tsx` editing shows 15. Check the payload first:
```bash
curl http://localhost:8000/api/stats | python -m json.tool | head -40
```

**2. Renaming a field on one side only.** Rename it in the backend schema and
the frontend still asks for the old name. `npx tsc --noEmit` catches it — run it
before every commit.

**3. Changing classification rules without a backfill.** New keywords only apply
to rows scraped *after* the change unless `backfill_verticals()` re-runs. It
runs at startup, so restarting the API is usually enough — but if filtering by a
renamed vertical returns nothing, this is why.
