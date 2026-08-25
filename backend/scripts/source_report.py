"""Every source, and how deep each one actually gets.

    python scripts/source_report.py                  # the full picture
    python scripts/source_report.py --only devaid,adb,unpp,undp,world
    python scripts/source_report.py --json report.json
    python scripts/source_report.py --list-only      # just the source list

Answers two different questions that are easy to confuse
--------------------------------------------------------
1. WHAT is configured — the registered sources, their URLs, whether they need a
   browser, whether they need a login, and how each is told to paginate. This
   comes from the code and is always accurate.

2. HOW MUCH each one actually reached — pages walked and rows found on the last
   run, against what is in the database now. This comes from the scrape_runs
   table, so it reflects what really happened rather than what was intended.

The second is the one that matters. A source can be configured perfectly and
still be returning page 1 only, because the site paginates in a way the crawler
cannot see, or because a subscription refuses to serve page 2. Both look like
success in the dashboard: rows arrive, no errors are logged.

`pages=1` on a source with thousands of listings is the signal to look for.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select                    # noqa: E402

import app.scrapers                                    # noqa: E402,F401 (registers)
from app.core.config import settings                   # noqa: E402
from app.database.db import session_scope              # noqa: E402
from app.database.models import Opportunity, ScrapeRun, Status  # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY     # noqa: E402
from app.services.links import is_usable_link, link_kind  # noqa: E402


def describe_pagination(cls) -> str:
    """How this source is told to walk past page 1, in a few words."""
    cfg = getattr(cls, "config", {}) or {}
    template = getattr(cls, "page_url_template", "") or cfg.get("page_url", "")
    module = cls.__module__.rsplit(".", 1)[-1]
    if module not in ("generic_listing", "abc"):
        return "own code"
    if template:
        kind = "offset" if "{offset}" in template else "page number"
        return f"URL template ({kind})"
    return "auto-detect only"


def source_rows() -> list[dict]:
    out = []
    for name, cls in SCRAPER_REGISTRY.items():
        cfg = getattr(cls, "config", {}) or {}
        module = cls.__module__.rsplit(".", 1)[-1]
        override = getattr(cls, "stale_page_streak_override", None)
        out.append({
            "name": name,
            "display": cls.display_name,
            "module": "config" if module in ("generic_listing", "abc") else module,
            "url": cls.start_url,
            "domain": urlparse(cls.start_url).netloc,
            "browser": bool(getattr(cls, "requires_js", False)
                            or getattr(cls, "prefer_js", False)),
            "login": bool(cfg.get("needs_login")
                          or name in ("developmentaid", "un_partner_portal")),
            "curated": bool(getattr(cls, "curated", False)),
            "pagination": describe_pagination(cls),
            "stop_rule": ("walk to the end" if override == 0 else
                          f"{settings.stale_page_streak} stale pages"
                          if override is None else f"{override} stale pages"),
        })
    out.sort(key=lambda r: r["display"].lower())
    return out


def measure(rows: list[dict]) -> None:
    """Fill each source in-place with what the database and run history say."""
    today = date.today()
    with session_scope() as db:
        totals = dict(db.execute(
            select(Opportunity.source_website, func.count(Opportunity.id))
            .group_by(Opportunity.source_website)).all())
        actives = dict(db.execute(
            select(Opportunity.source_website, func.count(Opportunity.id))
            .where(Opportunity.status == Status.ACTIVE)
            .group_by(Opportunity.source_website)).all())
        dated = dict(db.execute(
            select(Opportunity.source_website, func.count(Opportunity.id))
            .where(Opportunity.deadline.is_not(None))
            .group_by(Opportunity.source_website)).all())
        future = dict(db.execute(
            select(Opportunity.source_website, func.count(Opportunity.id))
            .where(Opportunity.deadline >= today)
            .group_by(Opportunity.source_website)).all())

        # Deep-link share, computed in Python because link_kind is a function.
        deep: dict[str, int] = defaultdict(int)
        for src, url, site in db.execute(
                select(Opportunity.source_website, Opportunity.opportunity_url,
                       Opportunity.website)):
            if url and is_usable_link(url, site or "") and link_kind(url) == "deep":
                deep[src] += 1

        # Last completed run per source.
        last: dict[str, ScrapeRun] = {}
        for run in db.execute(select(ScrapeRun).order_by(ScrapeRun.started_at)).scalars():
            last[run.source_website] = run
        # Deepest run ever seen — a source that once walked 40 pages and now
        # walks 1 has regressed, and the last run alone cannot show that.
        deepest = dict(db.execute(
            select(ScrapeRun.source_website, func.max(ScrapeRun.pages_scraped))
            .group_by(ScrapeRun.source_website)).all())

        for r in rows:
            key = r["display"]
            n = totals.get(key, 0)
            run = last.get(key)
            r.update(
                rows_total=n,
                rows_active=actives.get(key, 0),
                rows_dated=dated.get(key, 0),
                rows_future=future.get(key, 0),
                deep_pct=round(100 * deep.get(key, 0) / n, 1) if n else 0.0,
                dated_pct=round(100 * dated.get(key, 0) / n, 1) if n else 0.0,
                last_pages=(run.pages_scraped if run else 0),
                last_found=(run.found if run else 0),
                last_saved=(run.saved if run else 0),
                last_status=(run.status if run else "never run"),
                last_when=(run.started_at.strftime("%Y-%m-%d %H:%M")
                           if run and run.started_at else ""),
                deepest_pages=deepest.get(key, 0) or 0,
            )


def flags(r: dict) -> list[str]:
    """Short, specific warnings. Silence means nothing looked wrong."""
    out = []
    if r["last_status"] == "never run":
        out.append("never run")
        return out
    if r["last_found"] == 0:
        out.append("last run found NOTHING")
    elif r["last_pages"] <= 1:
        out.append("only page 1 was walked")
    if r["deepest_pages"] > max(r["last_pages"], 1) * 3 and r["deepest_pages"] > 3:
        out.append(f"regressed: once reached {r['deepest_pages']} pages")
    if r["rows_total"] and r["deep_pct"] < 50:
        out.append(f"only {r['deep_pct']:.0f}% link to a specific call")
    if r["rows_total"] and r["dated_pct"] < 10:
        out.append("almost no deadlines")
    if r["rows_total"] and r["rows_future"] == 0:
        out.append("nothing still open")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated name fragments")
    ap.add_argument("--json", default="", help="write the full report here")
    ap.add_argument("--list-only", action="store_true",
                    help="just the configured source list, no database read")
    args = ap.parse_args()

    rows = source_rows()
    if args.only:
        want = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        rows = [r for r in rows if any(
            w in r["name"].lower() or w in r["display"].lower()
            or w in r["domain"].lower() for w in want)]
        if not rows:
            print(f"nothing matched {args.only!r}", file=sys.stderr)
            return 2

    print(f"\n{'='*118}\nCONFIGURED SOURCES ({len(rows)})\n{'='*118}")
    print(f"{'SOURCE':32} {'HOW':14} {'BROWSER':8} {'LOGIN':6} {'PAGINATION':22} {'STOPS AFTER':18}")
    print("-" * 118)
    for r in rows:
        print(f"{r['display'][:31]:32} {r['module'][:13]:14} "
              f"{'yes' if r['browser'] else '-':8} {'yes' if r['login'] else '-':6} "
              f"{r['pagination'][:21]:22} {r['stop_rule'][:17]:18}")
    print(f"\n{'domains':>10}: {len({r['domain'] for r in rows})} distinct")

    if args.list_only:
        return 0

    measure(rows)

    print(f"\n{'='*118}\nHOW DEEP EACH ONE ACTUALLY GOT\n{'='*118}")
    print(f"{'SOURCE':32} {'PAGES':>6} {'FOUND':>7} {'SAVED':>7} {'IN DB':>8} "
          f"{'OPEN':>7} {'DEEP%':>6} {'DATED%':>7}  LAST RUN")
    print("-" * 118)
    for r in sorted(rows, key=lambda x: -x["rows_total"]):
        print(f"{r['display'][:31]:32} {r['last_pages']:>6} {r['last_found']:>7} "
              f"{r['last_saved']:>7} {r['rows_total']:>8} {r['rows_future']:>7} "
              f"{r['deep_pct']:>6} {r['dated_pct']:>7}  {r['last_when']} "
              f"{r['last_status']}")

    print(f"\n{'='*118}\nWHAT TO LOOK AT\n{'='*118}")
    any_flag = False
    for r in sorted(rows, key=lambda x: -x["rows_total"]):
        f = flags(r)
        if f:
            any_flag = True
            print(f"  {r['display'][:34]:36} {'; '.join(f)}")
    if not any_flag:
        print("  nothing flagged")

    shallow = [r for r in rows
               if r["last_status"] != "never run" and r["last_found"] > 0
               and r["last_pages"] <= 1]
    if shallow:
        print(f"\n  {len(shallow)} source(s) returned rows but walked only ONE page.")
        print("  That is either a site with a single page of listings, or "
              "pagination the crawler cannot see.")
        print("  Check the site by hand: if it has a page 2, the source needs a "
              "\"page_url\" template in sources.json.")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
