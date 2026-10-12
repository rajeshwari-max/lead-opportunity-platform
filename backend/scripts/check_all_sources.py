"""Is every scraper working? One live check per source, plus its recorded health.

    python scripts/check_all_sources.py                    # every source, live
    python scripts/check_all_sources.py --states stale failing never_produced
    python scripts/check_all_sources.py --only packard opensociety
    python scripts/check_all_sources.py --no-live          # recorded health only

Run it on EC2 (it needs the internet) when no scrape is running. Nothing is
written to the database: each source is crawled for a couple of pages exactly
as production crawls it, the rows are counted and thrown away.

Results go to the screen and to data/debug/source_check_<date>.json and .csv.

Verdicts
--------
WORKING          live crawl returned rows.
WORKING, QUIET   rows came back but the dashboard says no NEW row for weeks —
                 the site simply has not posted anything new.
EMPTY            the site says it has nothing open right now.
BLOCKED          no page came back (bot wall, 403, timeout, DNS, login).
PARSER BROKEN    pages came back but 0 rows — the site layout has changed.
CRASHED          the scraper raised an error; the message says where.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SKIP_BY_DEFAULT = {"web_discovery"}   # paid search API, not a website


def _verdict(live: dict | None, health) -> str:
    if live is None:
        return health.state.upper().replace("_", " ")
    if live["exception"]:
        return "CRASHED"
    if live["rows"] > 0:
        quiet = health is not None and health.state in ("stale", "never_produced")
        return "WORKING, QUIET" if quiet else "WORKING"
    if live["outcome"] == "confirmed_empty":
        return "EMPTY"
    if live["pages"] == 0 or live["timed_out"]:
        return "BLOCKED"
    return "PARSER BROKEN"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--states", nargs="*", default=[],
                    help="only sources the dashboard marks with these states (stale failing never_produced unknown ok)")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=150.0, help="seconds per source")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even while a scrape holds the lease")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    import app.scrapers  # noqa: F401  registers every source
    from app.database.db import session_scope
    from app.scrapers.registry import SCRAPER_REGISTRY, get_scrapers
    from app.services import run_lock
    from app.services.scraper_health import source_health
    from verify_source import classify_run, crawl

    if not args.no_live and not args.force:
        st = run_lock.status()
        if st["held"] and not st["stale"]:
            print(f"A scrape is running ({st['holder']}). Run this after it finishes, or pass --force.")
            return 2

    with session_scope() as db:
        health = {h.source_key: h for h in source_health(db)}

    names = args.only or [n for n in SCRAPER_REGISTRY if n not in SKIP_BY_DEFAULT]
    if args.states:
        names = [n for n in names if n in health and health[n].state in args.states]
    print(f"Checking {len(names)} source(s): {args.pages} page(s), {args.timeout:.0f}s each. Nothing is written.\n")

    results = []
    for i, name in enumerate(names, 1):
        h = health.get(name)
        live = None
        if not args.no_live:
            scraper = get_scrapers([name])[0]
            print(f"[{i}/{len(names)}] {scraper.display_name} ...", flush=True)
            t0 = time.monotonic()
            try:
                items, pages, note, timed_out, exc = asyncio.run(crawl(scraper, args.pages, args.timeout))
            except Exception as e:                       # noqa: BLE001 — one source must not stop the rest
                items, pages, note, timed_out, exc = [], 0, str(e), False, f"{type(e).__name__}: {e}"
            outcome, code, message = classify_run(scraper, items, pages, timed_out, exc)
            live = {"rows": len({(x.opportunity_url or x.title) for x in items}), "pages": pages,
                    "timed_out": timed_out, "exception": exc, "outcome": outcome,
                    "message": (exc or note or message)[:240], "seconds": round(time.monotonic() - t0)}
        verdict = _verdict(live, h)
        results.append({
            "source": name,
            "name": h.display_name if h else name,
            "verdict": verdict,
            "live_rows": live["rows"] if live else None,
            "live_pages": live["pages"] if live else None,
            "live_seconds": live["seconds"] if live else None,
            "live_detail": live["message"] if live else "",
            "dashboard_state": h.state if h else "",
            "rows_in_db": h.total_rows if h else None,
            "days_since_new_row": h.days_since_saved if h else None,
            "last_outcome": h.last_outcome if h else "",
            "last_error": h.last_error_message if h else "",
            "url": h.listing_url if h else "",
        })
        r = results[-1]
        print(f"      -> {verdict}  rows={r['live_rows']} pages={r['live_pages']}  {r['live_detail'][:110]}", flush=True)

    order = ["CRASHED", "PARSER BROKEN", "BLOCKED", "EMPTY", "WORKING, QUIET", "WORKING"]
    results.sort(key=lambda r: (order.index(r["verdict"]) if r["verdict"] in order else -1, r["name"]))
    print("\n" + "=" * 110)
    print(f"{'source':<34}{'verdict':<17}{'live rows':>10}{'in DB':>8}{'days since new':>16}   detail")
    print("-" * 110)
    for r in results:
        print(f"{r['name'][:33]:<34}{r['verdict']:<17}{'' if r['live_rows'] is None else r['live_rows']:>10}"
              f"{'' if r['rows_in_db'] is None else r['rows_in_db']:>8}"
              f"{'' if r['days_since_new_row'] is None else r['days_since_new_row']:>16}   "
              f"{(r['live_detail'] or r['last_error'])[:60]}")
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("=" * 110)
    print("  ".join(f"{k}: {v}" for k, v in counts.items()))

    out = Path(__file__).resolve().parents[1] / "data" / "debug"
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"source_check_{date.today().isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0]) if results else ["source"])
        w.writeheader()
        w.writerows(results)
    print(f"\nwrote {stem}.json and .csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
