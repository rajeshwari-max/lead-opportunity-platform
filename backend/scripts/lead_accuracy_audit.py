"""Check, and with --apply repair, what users can see: wrong dates, duplicates.

    python scripts/lead_accuracy_audit.py                     # report only
    python scripts/lead_accuracy_audit.py --samples 10
    python scripts/lead_accuracy_audit.py --apply --backup data/backups/x.db

Looks only at leads the ordinary dashboard shows (strict_actionable_clause:
Active, a real closing date, not passed).

--apply (database backed up first; nothing is deleted):
  run_together_title   the title is cut back to its own text (before "Ref No").
  date_disagrees_raw   the date is re-read from the source's own date text
                       with the row's own day/month convention; the lead is
                       archived if that date has passed. Never a date a person
                       corrected by hand.
  duplicate_title      copies with the same title, funder AND closing date are
                       folded into one: the copy a person has saved, reviewed,
                       given feedback on or sent to Wrike is kept, otherwise the
                       most recently seen. The others are hidden the way the
                       nightly duplicate pass hides them (unique_id
                       "merged:<its identity>", which the scraper then skips).
                       Same title with different dates stays: different calls.
  junk_title           a title with almost no words (a phone number): archived.
Left for a person: shared_source_date (fixed by re-scraping the source, which
now corrects stored dates), far_future.

Checks, each one a defect that has actually reached the dashboard:

  shared_source_date   one source, one closing date, on most of its live
                       leads. Until 2026-11-17 every UNDP notice carried the
                       closing date of the notice at the top of the page.
  date_disagrees_raw   the stored date is not what the source's own date text
                       says when read again (day/month swap, old parser).
  far_future           closes more than three years from today.
  run_together_title   a title that is several table cells glued together
                       ("... Ref No ... Deadline ...").
  duplicate_title      the same title from the same funder shown more than once.
  passed_but_active    status Active with a date that has passed. Hidden by
                       the dashboard query, archived by the nightly audit;
                       listed so a stalled audit is visible.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                                  # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity, Status            # noqa: E402
from app.services.actionable import (application_today,        # noqa: E402
                                     strict_actionable_clause)
from app.services.deadline_parser import DeadlineParser        # noqa: E402

_GLUED = re.compile(r"\b(Ref\.?\s*No|Deadline|Posted|Closing date)\b.*\d", re.I)
_CUT = re.compile(r"\s+(?:Ref\.?\s*No\.?|UNDP Office/Country|Process|Deadline|Posted)\b.*$",
                  re.I | re.S)
_NORM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORM.sub(" ", (text or "").casefold()).strip()


def audit(db, today=None) -> dict[str, list[tuple]]:
    today = today or application_today()
    rows = db.execute(
        select(Opportunity.id, Opportunity.source_website, Opportunity.title,
               Opportunity.organization, Opportunity.deadline,
               Opportunity.deadline_raw, Opportunity.deadline_convention,
               Opportunity.deadline_confidence)
        .where(strict_actionable_clause(today))
    ).all()
    found: dict[str, list[tuple]] = defaultdict(list)
    parser = DeadlineParser()

    by_source: dict[str, list] = defaultdict(list)
    for r in rows:
        by_source[r.source_website].append(r)
    for source, items in by_source.items():
        dates = defaultdict(list)
        for r in items:
            dates[r.deadline].append(r)
        day, same = max(dates.items(), key=lambda kv: len(kv[1]))
        if len(same) >= 25 and len(same) >= 0.5 * len(items):
            found["shared_source_date"].append(
                (source, f"{len(same)} of {len(items)} live leads close on {day}"))

    for r in rows:
        raw = (r.deadline_raw or "").strip()
        if raw and (r.deadline_confidence or "") != "human":
            again = parser.parse(raw, dayfirst=(r.deadline_convention != "monthfirst"))
            if again is not None and again != r.deadline:
                found["date_disagrees_raw"].append(
                    (r.id, r.source_website, f"stored {r.deadline}, text {raw!r} reads {again}",
                     (r.title or "")[:70]))
        if r.deadline and r.deadline > today + timedelta(days=3 * 365):
            found["far_future"].append((r.id, r.source_website, str(r.deadline),
                                        (r.title or "")[:70]))
        if _GLUED.search(r.title or ""):
            found["run_together_title"].append((r.id, r.source_website, (r.title or "")[:110]))
        if sum(ch.isalpha() for ch in (r.title or "")) < 4:
            found["junk_title"].append((r.id, r.source_website, (r.title or "")[:60]))

    groups = defaultdict(list)
    for r in rows:
        key = (_norm(r.title), _norm(r.organization))
        if key[0]:
            groups[key].append(r)
    for (title, _), items in groups.items():
        if len(items) > 1:
            found["duplicate_title"].append(
                (len(items), ", ".join(sorted({i.source_website for i in items}))[:80],
                 ", ".join(str(i.deadline) for i in items)[:60], title[:70]))

    passed = db.execute(
        select(Opportunity.id, Opportunity.source_website, Opportunity.deadline)
        .where(Opportunity.status == Status.ACTIVE, Opportunity.deadline < today)
    ).all()
    for r in passed:
        found["passed_but_active"].append((r.id, r.source_website, str(r.deadline)))
    found["_visible"] = [(len(rows),)]
    return found


def _referenced(db, ids: list[int]) -> set[int]:
    """Leads a person has touched: saved/viewed, applied, feedback, Wrike."""
    from app.database.models import (ApplicationJourney, HumanFeedback,
                                     LeadActivity, WrikeTaskLink)
    out: set[int] = set()
    for model in (LeadActivity, ApplicationJourney, HumanFeedback, WrikeTaskLink):
        out |= set(db.execute(select(model.opportunity_id)
                              .where(model.opportunity_id.in_(ids))).scalars())
    return out


def repair(db, today=None) -> dict[str, int]:
    """Fix what can be fixed safely. Nothing is deleted. Returns counts."""
    today = today or application_today()
    done = defaultdict(int)
    rows = db.execute(select(Opportunity).where(strict_actionable_clause(today))).scalars().all()
    parser = DeadlineParser()

    for o in rows:
        if _GLUED.search(o.title or ""):
            cut = _CUT.sub("", o.title or "").strip(" -|:")
            if len(cut) >= 10 and cut != o.title:
                o.title = cut[:500]
                done["run_together_title"] += 1
        if sum(ch.isalpha() for ch in (o.title or "")) < 4:
            o.status = Status.EXPIRED
            done["junk_title"] += 1
            continue
        raw = (o.deadline_raw or "").strip()
        if raw and (o.deadline_confidence or "") != "human":
            again = parser.parse(raw, dayfirst=(o.deadline_convention != "monthfirst"))
            if again is not None and again != o.deadline:
                o.deadline = again
                o.deadline_confidence = "parsed"
                if again < today:
                    o.status = Status.EXPIRED
                done["date_disagrees_raw"] += 1
    db.flush()

    live = [o for o in rows if o.status == Status.ACTIVE]
    groups = defaultdict(list)
    for o in live:
        key = (_norm(o.title), _norm(o.organization), o.deadline)
        if key[0]:
            groups[key].append(o)
    clusters = [g for g in groups.values() if len(g) > 1]
    touched = _referenced(db, [o.id for g in clusters for o in g]) if clusters else set()
    for group in clusters:
        keeper = max(group, key=lambda o: (o.id in touched,
                                           o.last_seen or o.date_scraped or datetime.min,
                                           -o.id))
        for o in group:
            if o is keeper:
                continue
            if o.id in touched:
                continue          # a person's lead is never hidden
            # The identity is kept after the prefix, so the scraper can
            # recognise this copy when it sees it again and skip it.
            o.unique_id = f"merged:{o.unique_id}"
            done["duplicate_title"] += 1
    return dict(done)


def _backup(target: str) -> None:
    import sqlite3

    from sqlalchemy.engine import make_url

    from app.core.config import settings
    source = make_url(settings.database_url).database
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {path}")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, \
            sqlite3.connect(path) as dst:
        src.backup(dst)
    print(f"database backup: {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--apply", action="store_true", help="repair what can be repaired")
    ap.add_argument("--backup", default="", help="backup file, required with --apply")
    args = ap.parse_args()
    if args.apply and not args.backup:
        ap.error("--apply needs --backup <file>")
    if args.apply:
        _backup(args.backup)
        with session_scope() as db:
            fixed = repair(db)
        print("Repaired: " + (", ".join(f"{k} {v}" for k, v in sorted(fixed.items()))
                              or "nothing needed repair"))
        print("Re-checking after the repair:\n")
    with session_scope() as db:
        found = audit(db)
    print(f"Leads users can see now: {found.pop('_visible')[0][0]:,}")
    names = ("shared_source_date", "date_disagrees_raw", "far_future",
             "run_together_title", "duplicate_title", "junk_title",
             "passed_but_active")
    problems = 0
    for name in names:
        items = found.get(name, [])
        problems += len(items)
        print(f"\n{name}: {len(items)}")
        for item in items[:max(0, args.samples)]:
            print("   ", " | ".join(str(x) for x in item))
    print("\nNo problems found." if not problems else
          "\nshared_source_date and far_future need a person; run with --apply "
          "--backup <file> to repair the rest." if not args.apply else
          "\nWhat is left needs a person (or a re-scrape of the source).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
