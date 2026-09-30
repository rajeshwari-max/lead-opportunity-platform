"""Did the 15 sources added on 2026-09-29 scrape EVERY row their site lists?

    python scripts/verify_batch_2026_09_29.py
    python scripts/verify_batch_2026_09_29.py --only samsstc_rfp leverforchange
    python scripts/verify_batch_2026_09_29.py --total samsstc_rfp=34

Run it where there is real internet — EC2 or the laptop. Nothing is written.

What it does
------------
For each source it runs the real scraper through verify_source.verify_one —
the same crawl, parser and opportunity gate that production uses — and
compares the unique rows found against the count THE SITE ITSELF states.
That comparison is the only honest answer to "did we get all of them": a crawl
that stops after page 1 still "succeeds", and the only thing that exposes it
is a number from outside our own code.

The totals below were read off each site on 2026-09-30. Sites change daily —
SAMS adds RFPs every week — so a mismatch means one of two things, and the
report says which to check:

    found < stated   a page was missed, or the parser skipped rows.
                     Re-read the site's own count first (the WHERE column says
                     where it is printed); if the site still says more than we
                     found, it is our defect.
    found > stated   the site grew since 2026-09-30, or the parser is picking
                     up navigation as rows. Look at the titles it printed.

Pass a fresh figure with --total name=N rather than editing this file.

Sources with no stated total
----------------------------
Six of the fifteen could not be read from the machine that wrote this (403 bot
walls, a robots.txt timeout, an https->http redirect loop), so nobody has seen
their pagination or their count. They are still run — the crawl shows how many
pages it walked and what it found — but coverage is reported as unproven, not
guessed. Read their totals off the site in a browser and pass them with --total.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# name -> (total the site states, where it states it). Read 2026-09-30.
STATED: dict[str, tuple[int | None, str]] = {
    "samsstc_rfp": (31, "'Showing 1–30 of 31' above the list on /rfp-tender/rfp-list"),
    "leverforchange": (19, "'Showing 1–12 of 19 results' on /open-calls/"),
    "isti_institutional_grants": (19, "'Total number of Record(s): 19' under the list"),
    "isti_international_grants": (11, "'Total number of Record(s): 11'"),
    "isti_individual_grants": (20, "'Total number of Record(s): 20'"),
    "isti_conference_grants": (7, "'Total number of Record(s): 7'"),
    "devinfo_rfps": (30, "count the entries — the page states no total; 30 on 2026-09-30"),
    "grandchallenges": (1, "count the cards on /grant-opportunities; 1 on 2026-09-30"),
    "globaleba_fund": (1, "one call per page; the 8th Small-Size Grants call"),
    # Not readable from where this was written. Supply with --total.
    "fire_biofin": (None, "the resource counter on the FIRE home page (435 in Sept 2026, unconfirmed)"),
    "wellcome_contracts": (None, "count the notices on the contract-opportunities page"),
    "investindia_rfp": (None, "count the RFPs on /request-for-proposal"),
    "eu_funding_portal_social": (None, "the results counter on the page, if any"),
    "developmentwala_rfps": (None, "the results counter on /rfps, if any"),
    "ai_opportunity_fund_apac": (None, "not a listing — 0 or 1 is the right answer"),
}

# Enough pages to reach every stated row plus the empty page that ends the
# walk. FIRE claims 49 pages, so the unmeasured ones get room for it.
PAGES = {"samsstc_rfp": 3, "leverforchange": 3,
         "isti_institutional_grants": 3, "isti_international_grants": 3,
         "isti_individual_grants": 3, "isti_conference_grants": 2,
         "devinfo_rfps": 2, "grandchallenges": 2, "globaleba_fund": 2}
DEFAULT_PAGES = 55


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=[], help="run just these sources")
    ap.add_argument("--total", action="append", default=[],
                    help="override a stated total: --total samsstc_rfp=34")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--no-db", action="store_true",
                    help="do not open the database even read-only")
    args = ap.parse_args()

    stated = {k: v[0] for k, v in STATED.items()}
    for item in args.total:
        name, _, n = item.partition("=")
        if name not in STATED or not n.isdigit():
            ap.error(f"--total wants name=N for one of: {', '.join(STATED)}")
        stated[name] = int(n)

    names = args.only or list(STATED)
    unknown = [n for n in names if n not in STATED]
    if unknown:
        ap.error(f"not in this batch: {', '.join(unknown)}")

    import app.scrapers  # noqa: F401 — registers every source
    from verify_source import verify_one  # the script beside this one

    rows = []
    for name in names:
        run_args = SimpleNamespace(
            pages=PAGES.get(name, DEFAULT_PAGES), timeout=args.timeout,
            official_total=stated[name], no_db=args.no_db,
            note=["batch check, stated total read 2026-09-30"], json="",
            quiet=True)
        try:
            r = verify_one(name, run_args)
            # A source that fetched nothing has not "missed rows" — it never
            # saw the page. Reporting MISSING there sends someone to debug
            # the parser when the problem is the network or a bot wall.
            if not getattr(r, "pages_fetched", 0):
                rows.append((name, None, stated[name],
                             "NOT FETCHED — no page came back (network, bot wall, "
                             "or robots); see the log lines above"))
            else:
                rows.append((name, r.unique, stated[name], None))
        except Exception as exc:                       # noqa: BLE001
            # One broken source must not hide the other fourteen.
            rows.append((name, None, stated[name], f"{type(exc).__name__}: {exc}"))

    print("\n" + "=" * 96)
    print(f"{'source':<28}{'found':>7}{'stated':>8}   verdict")
    print("-" * 96)
    missing = 0
    for name, found, total, error in rows:
        if error and error.startswith("NOT FETCHED"):
            verdict = error
            missing += 1
        elif error:
            verdict = f"CRASHED — {error[:60]}"
            missing += 1
        elif total is None:
            verdict = f"UNPROVEN — read the total from: {STATED[name][1]}"
        elif found == total:
            verdict = "ALL ROWS"
        elif found < total:
            verdict = f"MISSING {total - found} — recheck: {STATED[name][1]}"
            missing += 1
        else:
            verdict = f"{found - total} MORE than stated — site grew, or nav rows?"
        print(f"{name:<28}{'' if found is None else found:>7}"
              f"{'?' if total is None else total:>8}   {verdict}")
    print("=" * 96)
    print("found = unique rows the scraper extracted. Rows are counted BEFORE the "
          "dashboard's\nActive rule, so an undated ISTI scheme counts here even "
          "though it is not shown.")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
