"""Clean the dashboard: closed calls, dead links, non-opportunities, duplicates.

    python scripts/clean_dashboard.py                 # report only, changes nothing
    python scripts/clean_dashboard.py --apply         # do it
    python scripts/clean_dashboard.py --apply --vacuum  # ...and reclaim disk space
    python scripts/clean_dashboard.py --samples 20    # show more examples per pass

Dry run is the default and prints exactly what --apply would remove, with real
examples from your own data. Nothing here is guessed: each pass reports the rows
it matched so you can disagree with it before anything is deleted.

The six passes, in order, and why each exists
---------------------------------------------
1. DEADLINES      Recompute Active/Expired from the date. Clears sentinel dates
                  (9999-12-31 and friends). Retires undated "Ongoing" rows that
                  no working scrape has seen for LOP_ONGOING_MAX_AGE_DAYS.
                  Archives, never deletes — closed calls stay in the archive.

2. FURNITURE      Delete rows that are page furniture, not calls: "Skip to main
                  content", "Navigation breadcrumbs", bare email addresses.

3. NO LINK        Delete rows with no link to the opportunity itself. These are
                  the entries that opened a search engine: services/links.py
                  used to hand them a DuckDuckGo query when they had no real
                  URL, so they looked clickable and led nowhere.

4. NOT AN         Delete rows that fail services/opportunity_gate.py — news
   OPPORTUNITY    posts, programme landing pages, "our grantees" cards and
                  section headings that the link-harvesting scrapers stored as
                  fundable calls. Rows from curated boards (UN Partner Portal,
                  ADB, UNDP Procurement, World Bank, DevelopmentAid...) are only
                  tested for furniture and page type, never for vocabulary.

5. DUPLICATES     Delete near-duplicates. The unique_id fingerprint includes the
                  URL and the deadline, so the SAME call scraped with a
                  tracking parameter, under two source names, or after its date
                  was re-parsed, hashes differently and both rows survive. This
                  groups on normalised title + organisation and keeps the best
                  row in each group.

6. OLD ARCHIVE    Delete rows Expired for more than LOP_EXPIRED_PURGE_DAYS
                  (default 90). Closed calls are worth keeping for a while; they
                  are not worth keeping forever. Set the setting to 0 to skip.

Order matters: junk is removed before duplicates are counted, so a duplicate
group is not "resolved" by keeping the junk copy.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select                      # noqa: E402

import app.scrapers                                        # noqa: E402,F401 (registers)
from app.core.config import settings                       # noqa: E402
from app.database.db import init_db, session_scope         # noqa: E402
from app.database.models import Opportunity, Status        # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY         # noqa: E402
from app.services.links import is_furniture, is_usable_link  # noqa: E402
from app.services.opportunity_gate import is_opportunity   # noqa: E402


def curated_sources() -> set[str]:
    """Display names of sources whose pages contain only opportunities.

    Read from the scrapers themselves rather than hard-coded here, so a source
    that gains or loses `curated` does not need this script edited too.
    """
    out = set()
    for cls in SCRAPER_REGISTRY.values():
        if getattr(cls, "curated", False):
            out.add(cls.display_name)
    return out


def _norm_title(value: str) -> str:
    """Normalised title for duplicate grouping.

    Punctuation, case and whitespace are dropped because they are exactly what
    differs between two scrapes of one call ("RFP: Water Study" vs "RFP - Water
    Study"). Reference numbers are NOT dropped — two notices differing only by
    their reference are two different notices.
    """
    t = re.sub(r"\s+", " ", (value or "")).strip().lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _score(opp: Opportunity) -> tuple:
    """Higher is better. Decides which row in a duplicate group survives.

    Preference order, most important first:
      a real deep link > a link at all > none
      a clean URL > one carrying tracking parameters
      has a deadline > undated
      Active > Expired
      longer summary (more context for the reader)
      newest scrape

    The tracking-parameter rule matters more than it looks: the commonest cause
    of a duplicate pair is the same call linked once plainly and once with
    ?utm_source=... appended, and the two hash differently. Without this the
    survivor is decided by scrape order, so half the dashboard's links end up
    carrying someone else's campaign parameters.
    """
    from app.services.links import link_kind

    url = opp.opportunity_url or ""
    has_link = bool(url)
    deep = has_link and link_kind(url) == "deep"
    clean = "?" not in url or not re.search(
        r"[?&](utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|source)=", url, re.IGNORECASE)
    # A float, not a datetime: date_scraped is tz-aware on new rows and naive on
    # ones written before the column was, and comparing the two raises.
    scraped = (opp.date_scraped.timestamp() if opp.date_scraped else 0.0)
    return (
        deep, has_link, clean, opp.deadline is not None,
        opp.status is Status.ACTIVE, len(opp.summary or ""), scraped,
    )


def _fmt(rows, limit: int) -> str:
    out = []
    for opp in rows[:limit]:
        out.append(f"      [{opp.source_website[:22]:22}] {(opp.title or '')[:62]}")
        out.append(f"        {(opp.opportunity_url or '(no link)')[:96]}")
    if len(rows) > limit:
        out.append(f"      ... and {len(rows) - limit:,} more")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually change the database (default: report only)")
    ap.add_argument("--vacuum", action="store_true",
                    help="run VACUUM afterwards to reclaim disk space (slow, "
                         "needs free space equal to the database size)")
    ap.add_argument("--samples", type=int, default=8,
                    help="examples to print per pass (default 8)")
    ap.add_argument("--skip", default="",
                    help="comma-separated passes to skip: deadlines, furniture, "
                         "nolink, gate, duplicates, oldarchive")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(levelname)-7s %(message)s")
    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}
    init_db()

    curated = curated_sources()
    today = date.today()
    mode = "APPLYING" if args.apply else "DRY RUN — nothing will be changed"
    print(f"\n{mode}\n" + "=" * 92)
    print(f"curated sources (exempt from the vocabulary test): "
          f"{', '.join(sorted(curated)) or 'none'}")

    # ---------------------------------------------------------------- pass 1
    if "deadlines" not in skip:
        print("\n1. DEADLINES — recompute status, clear sentinels, retire stale Ongoing")
        if args.apply:
            from app.services.deadline_audit import audit_deadlines
            stats = audit_deadlines()
            print(f"   sentinels cleared : {stats['sentinels_cleared']:,}")
            print(f"   newly expired     : {stats['expired']:,}")
            print(f"   reactivated       : {stats['reactivated']:,}")
            print(f"   stale Ongoing     : {stats['stale_ongoing']:,}  (archived)")
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=settings.ongoing_max_age_days)
            with session_scope() as db:
                wrong = past = undated_stale = 0
                for opp in db.execute(select(Opportunity)).scalars():
                    if opp.deadline is not None:
                        should = Status.EXPIRED if opp.deadline < today else Status.ACTIVE
                        if opp.status != should:
                            wrong += 1
                        if opp.deadline < today and opp.status is Status.ACTIVE:
                            past += 1
                    elif opp.status is Status.ACTIVE:
                        seen = getattr(opp, "last_seen", None) or opp.date_scraped
                        if seen and (seen.replace(tzinfo=timezone.utc)
                                     if seen.tzinfo is None else seen) < cutoff:
                            undated_stale += 1
                print(f"   status wrong for the stored date : {wrong:,}")
                print(f"     ...of which past-dated but still Active : {past:,}")
                print(f"   undated 'Ongoing' unseen > {settings.ongoing_max_age_days}d "
                      f": {undated_stale:,}")
                print("   (all archived, none deleted)")

    # ------------------------------------------------------- passes 2, 3, 4
    with session_scope() as db:
        all_rows = list(db.execute(select(Opportunity)).scalars())
        total_before = len(all_rows)
        print(f"\nrows in the database: {total_before:,}")

        furniture: list[Opportunity] = []
        nolink: list[Opportunity] = []
        notopp: list[Opportunity] = []
        reasons: dict[str, int] = defaultdict(int)
        by_source: dict[str, int] = defaultdict(int)

        for opp in all_rows:
            title, url = opp.title or "", opp.opportunity_url or ""
            if "furniture" not in skip and is_furniture(title, url):
                furniture.append(opp)
                continue
            if "nolink" not in skip and not is_usable_link(url, opp.website or ""):
                nolink.append(opp)
                continue
            if "gate" not in skip:
                keep, why = is_opportunity(
                    title, opp.summary or "", url,
                    str(getattr(opp.category, "value", opp.category) or ""),
                    opp.source_website in curated)
                if not keep:
                    notopp.append(opp)
                    reasons[why] += 1
                    by_source[opp.source_website] += 1

        if "furniture" not in skip:
            print(f"\n2. FURNITURE — not opportunities at all: {len(furniture):,} row(s)")
            print(_fmt(furniture, args.samples))
        if "nolink" not in skip:
            print(f"\n3. NO LINK — nothing to open, previously shown as a web "
                  f"search: {len(nolink):,} row(s)")
            print(_fmt(nolink, args.samples))
        if "gate" not in skip:
            print(f"\n4. NOT AN OPPORTUNITY: {len(notopp):,} row(s)")
            for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                print(f"      {n:>7,}  {why}")
            print("   worst sources:")
            for src, n in sorted(by_source.items(), key=lambda kv: -kv[1])[:10]:
                print(f"      {n:>7,}  {src}")
            print(_fmt(notopp, args.samples))

        doomed = {id(o): o for o in furniture + nolink + notopp}

        # ------------------------------------------------------------ pass 5
        dupes: list[Opportunity] = []
        if "duplicates" not in skip:
            groups: dict[tuple, list[Opportunity]] = defaultdict(list)
            for opp in all_rows:
                if id(opp) in doomed:
                    continue          # junk must not win a duplicate contest
                key = (_norm_title(opp.title),
                       re.sub(r"\s+", " ", (opp.organization or "")).strip().lower())
                if key[0]:
                    groups[key].append(opp)
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                rows.sort(key=_score, reverse=True)
                dupes.extend(rows[1:])   # keep the best, drop the rest
            print(f"\n5. DUPLICATES — same call stored more than once: "
                  f"{len(dupes):,} row(s) to remove")
            print("   (the best copy of each is kept: real deep link > any link, "
                  "clean URL > tracking parameters,\n    dated > undated, "
                  "Active > Expired, longer summary, then newest)")
            print(_fmt(dupes, args.samples))

        # ------------------------------------------------------------ pass 6
        old: list[Opportunity] = []
        purge_days = settings.expired_purge_days
        if "oldarchive" not in skip and purge_days:
            horizon = today - timedelta(days=purge_days)
            for opp in all_rows:
                if id(opp) in doomed:
                    continue
                if opp.status is Status.EXPIRED and opp.deadline and opp.deadline < horizon:
                    old.append(opp)
            print(f"\n6. OLD ARCHIVE — closed more than {purge_days} days ago: "
                  f"{len(old):,} row(s)")
            print(_fmt(old, args.samples))

        # ------------------------------------------------------------ apply
        remove_ids = {o.id for o in furniture + nolink + notopp + dupes + old}
        print("\n" + "=" * 92)
        print(f"TOTAL TO REMOVE: {len(remove_ids):,} of {total_before:,} rows "
              f"({100 * len(remove_ids) / max(total_before, 1):.1f}%)")
        print(f"REMAINING      : {total_before - len(remove_ids):,}")

        if not args.apply:
            print("\nDry run — nothing was changed. Re-run with --apply to do it.")
            print("Disagree with a pass? Skip it: --skip gate,duplicates")
            return 0

        ids = sorted(remove_ids)
        for i in range(0, len(ids), 500):
            db.execute(delete(Opportunity).where(Opportunity.id.in_(ids[i:i + 500])))
        print(f"\nDeleted {len(ids):,} row(s).")

    if args.vacuum:
        # Outside the session: VACUUM cannot run inside a transaction.
        print("Running VACUUM (this takes a while on a large database)...")
        from app.database.db import engine
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM")
        print("VACUUM done — the file on disk should now be smaller.")

    print("\nDone. Reload the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
