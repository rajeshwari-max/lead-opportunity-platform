"""Deep check of ONE source: run its real crawl and show every row it produced.

    python scripts/check_scraper.py un_partner_portal
    python scripts/check_scraper.py un_partner_portal --pages 3 --json unpp.json
    python scripts/check_scraper.py adb --rows 50 --timeout 300

How this differs from scripts/validate_sources.py
-------------------------------------------------
validate_sources answers "which of the 77 sources are broken?" — one page each,
three sample rows, a verdict column. It is the sweep.

This answers the next question, the one you actually act on: "is what this ONE
source produced CORRECT?" So it walks several pages, prints every field of every
row, and separates the two failures that a verdict cannot tell apart:

  * the scrape produced nothing            -> a fetch, login or markup problem
  * the scrape produced rows that are wrong -> titles that are navigation,
    links that open a listing instead of the call, deadlines that never parsed

Logging is INFO by default and NOT optional. For sources behind a login the
useful information — which session was used, what the portal's own API answered,
why a page came back empty — is all logged, and hiding it leaves you with a
table of zeroes and no reason for them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scrapers.registry import SCRAPER_REGISTRY, get_scrapers  # noqa: E402
from app.services.deadline_parser import DeadlineParser  # noqa: E402
from app.services.links import is_furniture, is_usable_link, link_kind  # noqa: E402

_deadlines = DeadlineParser()


def resolve(name: str):
    """Find one scraper by exact name, then by substring of name or display."""
    if name in SCRAPER_REGISTRY:
        return get_scrapers([name])[0]
    want = name.lower().replace(" ", "")
    hits = [n for n, c in SCRAPER_REGISTRY.items()
            if want in n.lower() or want in c.display_name.lower().replace(" ", "")]
    if len(hits) == 1:
        return get_scrapers(hits)[0]
    if not hits:
        print(f"No scraper matches {name!r}. Registered names:\n  "
              + "\n  ".join(sorted(SCRAPER_REGISTRY)), file=sys.stderr)
        raise SystemExit(2)
    print(f"{name!r} matches several: {', '.join(sorted(hits))}", file=sys.stderr)
    raise SystemExit(2)


async def run(scraper, pages: int, timeout: float):
    stop, pause = asyncio.Event(), asyncio.Event()
    pause.set()
    collected: list = []
    page_count = 0

    async def progress(event, payload):
        if event in ("page_start", "page_error"):
            print(f"  .. {event} page={payload.get('page')} "
                  f"{payload.get('url', '')[:110]}", flush=True)

    async def walk():
        nonlocal page_count
        async for batch in scraper.crawl(stop, pause, progress):
            page_count += 1
            collected.extend(batch)
            print(f"  .. page {page_count}: {len(batch)} item(s)", flush=True)
            if page_count >= pages:
                stop.set()
                break

    try:
        await asyncio.wait_for(walk(), timeout=timeout)
    except asyncio.TimeoutError:
        stop.set()
        print(f"  !! timed out after {timeout:.0f}s "
              f"(got {page_count} page(s) so far)")
    except Exception as exc:                                    # noqa: BLE001
        stop.set()
        print(f"  !! {type(exc).__name__}: {exc}")
    return collected, page_count


def report(scraper, items, page_count, rows: int) -> dict:
    print("\n" + "=" * 100)
    print(f"{scraper.display_name}  ({scraper.name})")
    print(f"start_url : {scraper.start_url}")
    print(f"pages     : {page_count}")
    print(f"items     : {len(items)}")
    print("=" * 100)

    if not items:
        print("\nNOTHING WAS PRODUCED.")
        print("  * page_count 0  -> the fetch itself failed: DNS, a block, a "
              "browser that never started, or a login wall.")
        print("  * page_count >0 -> the page arrived and the parser found "
              "nothing in it. Look in backend/data/debug/ for the saved HTML.")
        return {"name": scraper.name, "items": 0, "pages": page_count}

    deep = dated = parsed_dates = junk = 0
    urls: list[str] = []
    for i in items:
        if is_usable_link(i.opportunity_url, i.website) and \
                link_kind(i.opportunity_url) == "deep":
            deep += 1
        if (i.deadline_raw or "").strip():
            dated += 1
            try:
                if (_deadlines.parse(i.deadline_raw, dayfirst=i.dayfirst)
                        or _deadlines.is_ongoing(i.deadline_raw)):
                    parsed_dates += 1
            except Exception:
                pass
        if is_furniture(i.title or "", i.opportunity_url or ""):
            junk += 1
        if i.opportunity_url:
            urls.append(i.opportunity_url)

    n = len(items)
    print(f"\nlinks to a specific opportunity : {deep}/{n} ({100*deep/n:.0f}%)")
    print(f"carry a deadline string         : {dated}/{n} ({100*dated/n:.0f}%)")
    print(f"  ... that actually parses      : {parsed_dates}/{n} "
          f"({100*parsed_dates/n:.0f}%)")
    print(f"navigation furniture stored     : {junk}")
    print(f"duplicate URLs                  : {len(urls) - len(set(urls))}")
    print(f"blank organisation              : "
          f"{sum(1 for i in items if not (i.organization or '').strip())}")

    print(f"\nFIRST {min(rows, n)} ROW(S) IN FULL")
    print("-" * 100)
    for idx, i in enumerate(items[:rows], 1):
        print(f"{idx:>3}. {i.title[:92]}")
        print(f"     url      : {i.opportunity_url[:110]}  [{link_kind(i.opportunity_url)}]")
        print(f"     org      : {i.organization[:60]:<60} country: {i.country[:30]}")
        print(f"     deadline : {i.deadline_raw or '(none)':<28} "
              f"assume_active={i.assume_active}")
        if i.vertical:
            print(f"     sector   : {i.vertical[:92]}")
        if i.summary:
            print(f"     summary  : {i.summary[:92]}")
    print("-" * 100)

    # A short, honest verdict. The thresholds are the ones validate_sources
    # uses, so the two tools cannot disagree about the same source.
    problems = []
    if junk:
        problems.append(f"{junk} row(s) are site furniture, not opportunities")
    if 100 * deep / n < 50:
        problems.append("more than half the links open a listing, not the call")
    if dated and parsed_dates < dated:
        problems.append(f"{dated - parsed_dates} deadline string(s) do not parse "
                        "into a date, so those rows never expire")
    if len(urls) != len(set(urls)):
        problems.append("the same URL appears under more than one title")
    print("\nVERDICT:", "LOOKS CORRECT" if not problems else "NEEDS WORK")
    for p in problems:
        print("  -", p)

    return {
        "name": scraper.name, "display_name": scraper.display_name,
        "pages": page_count, "items": n, "deep": deep, "dated": dated,
        "deadlines_parsed": parsed_dates, "junk": junk,
        "dup_url": len(urls) - len(set(urls)), "problems": problems,
        "rows": [i.model_dump() for i in items],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="scraper name, e.g. un_partner_portal")
    ap.add_argument("--pages", type=int, default=2, help="pages to walk (default 2)")
    ap.add_argument("--rows", type=int, default=15, help="rows to print in full")
    ap.add_argument("--timeout", type=float, default=240.0, help="seconds overall")
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--quiet", action="store_true",
                    help="warnings only (hides the lines that explain a failure)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="  %(levelname)-7s %(name)s: %(message)s")

    import app.scrapers  # noqa: F401  — importing registers every plugin

    scraper = resolve(args.source)
    print(f"Running {scraper.display_name} for up to {args.pages} page(s), "
          f"{args.timeout:.0f}s. Nothing is written to the database.\n")
    started = time.monotonic()
    items, page_count = asyncio.run(run(scraper, args.pages, args.timeout))
    result = report(scraper, items, page_count, args.rows)
    result["seconds"] = round(time.monotonic() - started, 1)
    print(f"\nfinished in {result['seconds']}s")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, default=str),
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
