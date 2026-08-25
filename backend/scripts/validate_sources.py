"""Live per-source scrape validation. Run this on the machine that scrapes.

    python scripts/validate_sources.py                    # every source
    python scripts/validate_sources.py --only bond,adb    # a few
    python scripts/validate_sources.py --timeout 180      # slower sites
    python scripts/validate_sources.py --json report.json # machine-readable

Why it exists
-------------
"Is this source scraping correctly?" cannot be answered from the code. The
scraper can be perfectly written and still return nothing, because the site
moved its listing page, added a bot check, or changed its markup. The only
honest test is to fetch the real page from the real network position and run
the real parser over it.

So this runs each scraper's ACTUAL crawl path — including Playwright rendering
for JS sources — takes the first page of results, and measures it. Nothing is
saved to the database; this is a read-only probe.

What each verdict means
-----------------------
  OK        parsed items, links look like real opportunity pages
  WEAK      parsed items, but the quality checks are poor — usually a listing
            page being scraped as if it were opportunities, or no deadlines
  NO_ITEMS  page fetched, parser returned nothing. Either the markup moved
            (a scraper fix) or the page genuinely has no open calls today
            (nothing to fix). The samples and the log line separate these.
  NO_FETCH  the page never arrived — DNS, TLS, proxy or a hard block. This is
            the one that most often means "the URL is dead, send a fresh one".
  BLOCKED   the site refused us (403/429/503, or a bot-check page title)
  ERROR     the fetch or the parse raised
  TIMEOUT   exceeded --timeout for this source

Read the columns, not just the verdict:
  items      opportunities parsed off page 1
  deep%      share whose link points at a specific opportunity rather than a
             section index. A low number here is the "opens the wrong page"
             complaint, measured.
  dline%     share carrying a deadline. 0% means every row becomes an
             undated "Ongoing" that nothing can ever expire.
  junk       rows that are navigation furniture ("Skip to main content")
  dupurl     rows sharing one URL with a different title
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scrapers.registry import get_scrapers  # noqa: E402
from app.services.links import is_furniture, is_usable_link, link_kind  # noqa: E402

BOT_TITLES = ("just a moment", "attention required", "access denied",
              "checking your browser", "verifying you are human")


async def dump_page_html(scraper, out_dir: Path, timeout: float) -> str:
    """Save the raw page-1 HTML the scraper actually receives.

    Measuring tells you a source is broken; it cannot tell you what to change.
    The markup does. With the real HTML in hand the right item/title/deadline
    selectors can be written exactly, instead of guessed at from a screenshot —
    which matters most for the boards configured with no selectors at all, where
    the parser is harvesting every link on the page and picking up navigation.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{scraper.name}.html"
    try:
        html = await asyncio.wait_for(
            asyncio.to_thread(_render, scraper, timeout), timeout + 30)
        target.write_text(html or "", encoding="utf-8", errors="replace")
        return f"{target} ({len(html or '')} bytes)"
    except Exception as exc:                                    # noqa: BLE001
        return f"FAILED: {type(exc).__name__}: {exc}"[:200]


def _render(scraper, timeout: float) -> str:
    """The page as the SCRAPER sees it — same browser, same session.

    This used to fetch with a bare httpx call for non-JS sources, which carries
    no cookies. For a source behind a login that produced a dump of the public
    marketing page while the scraper itself was seeing something else entirely —
    a capture that answers a question nobody asked. Going through
    site_auth.open_context means the dump reflects the real fetch, including
    your Chrome session where the source uses one.
    """
    from playwright.sync_api import sync_playwright

    from app.scrapers import site_auth

    with sync_playwright() as pw:
        context = site_auth.open_context(pw, scraper.name, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(scraper.start_url, timeout=int(timeout * 1000),
                      wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            return page.content()
        finally:
            try:
                context.close()
            except Exception:
                pass


async def probe(scraper, timeout: float) -> dict:
    """Run one scraper's real crawl, keep only the first page, measure it."""
    out = {
        "name": scraper.name, "display_name": scraper.display_name,
        "start_url": scraper.start_url, "requires_js": bool(scraper.requires_js),
        "verdict": "ERROR", "items": 0, "deep_pct": 0.0, "deadline_pct": 0.0,
        "junk": 0, "dup_url": 0, "error": "", "seconds": 0.0, "samples": [],
    }
    stop = asyncio.Event()
    pause = asyncio.Event()
    pause.set()
    batch: list = []
    pages_yielded = 0
    started = time.monotonic()

    async def progress(_event, _payload):
        return None

    async def run():
        nonlocal pages_yielded
        async for page in scraper.crawl(stop, pause, progress):
            pages_yielded += 1
            batch.extend(page)
            stop.set()          # one page is enough for a probe
            break

    try:
        await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        stop.set()
        out["verdict"] = "TIMEOUT"
        out["error"] = f"no first page within {timeout:.0f}s"
        out["seconds"] = round(time.monotonic() - started, 1)
        return out
    except Exception as exc:                                    # noqa: BLE001
        stop.set()
        msg = f"{type(exc).__name__}: {exc}"
        out["error"] = msg[:300]
        low = msg.lower()
        if any(t in low for t in BOT_TITLES) or "403" in msg or "429" in msg:
            out["verdict"] = "BLOCKED"
        out["seconds"] = round(time.monotonic() - started, 1)
        return out

    out["seconds"] = round(time.monotonic() - started, 1)
    out["items"] = len(batch)
    if not batch:
        # Distinguish "the page never arrived" from "the page arrived and the
        # parser found nothing". BaseScraper swallows fetch failures and simply
        # yields no pages, so without this a DNS failure, a proxy block and a
        # moved listing page all read identically as NO_ITEMS — and only one of
        # those three is a scraper bug.
        out["verdict"] = "NO_ITEMS" if pages_yielded else "NO_FETCH"
        if not pages_yielded:
            out["error"] = ("crawl yielded no page at all — the fetch failed "
                            "(DNS/TLS/proxy/blocked), see the log line above")
        return out

    deep = sum(1 for i in batch
               if is_usable_link(i.opportunity_url, i.website)
               and link_kind(i.opportunity_url) == "deep")
    dated = sum(1 for i in batch if (i.deadline_raw or "").strip())
    junk = sum(1 for i in batch if is_furniture(i.title or "", i.opportunity_url or ""))
    urls = [i.opportunity_url for i in batch if i.opportunity_url]
    dup = len(urls) - len(set(urls))

    out.update(
        deep_pct=round(100 * deep / len(batch), 1),
        deadline_pct=round(100 * dated / len(batch), 1),
        junk=junk, dup_url=dup,
        samples=[{"title": (i.title or "")[:80], "url": (i.opportunity_url or "")[:120],
                  "deadline_raw": (i.deadline_raw or "")[:40]} for i in batch[:3]],
    )

    # A source "works" only if what it produced is usable, not merely non-empty.
    # Deadlines are scored leniently: plenty of funders genuinely publish rolling
    # calls, so 0% dated is a flag to look at, not proof of a fault on its own.
    if junk or out["deep_pct"] < 50:
        out["verdict"] = "WEAK"
    else:
        out["verdict"] = "OK"
    return out


async def main_async(args) -> int:
    scrapers = get_scrapers(None)
    if args.only:
        want = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        # Substring match, because the registry name ("cleanairfund") and the
        # display name ("Clean Air Fund") rarely agree and nobody should have to
        # guess which one this wants.
        scrapers = [s for s in scrapers if any(
            w in s.name.lower() or w in s.display_name.lower().replace(" ", "")
            or w in s.display_name.lower() for w in want)]
        if not scrapers:
            print(f"No scraper matched {args.only!r}", file=sys.stderr)
            return 2

    print(f"Probing {len(scrapers)} source(s), {args.timeout:.0f}s each, "
          f"{args.concurrency} at a time. Nothing is written to the database.\n")
    gate = asyncio.Semaphore(args.concurrency)
    results: list[dict] = []

    async def one(s):
        async with gate:
            r = await probe(s, args.timeout)
            if args.dump_html:
                r["html_dump"] = await dump_page_html(s, Path(args.dump_html), args.timeout)
                print(f"  {'saved':9} {s.display_name[:34]:36} {r['html_dump']}", flush=True)
            results.append(r)
            print(f"  {r['verdict']:9} {r['display_name'][:34]:36} "
                  f"items={r['items']:<4} deep={r['deep_pct']:<5} "
                  f"dline={r['deadline_pct']:<5} {r['error'][:60]}", flush=True)

    await asyncio.gather(*(one(s) for s in scrapers))

    results.sort(key=lambda r: (r["verdict"] != "OK", r["display_name"].lower()))
    print("\n" + "=" * 104)
    print(f"{'VERDICT':9} {'SOURCE':34} {'items':>6} {'deep%':>6} {'dline%':>7} "
          f"{'junk':>5} {'dup':>4} {'secs':>6}  note")
    print("=" * 104)
    for r in results:
        print(f"{r['verdict']:9} {r['display_name'][:33]:34} {r['items']:>6} "
              f"{r['deep_pct']:>6} {r['deadline_pct']:>7} {r['junk']:>5} "
              f"{r['dup_url']:>4} {r['seconds']:>6}  {r['error'][:40]}")

    from collections import Counter
    tally = Counter(r["verdict"] for r in results)
    print("\nTALLY:", dict(tally))
    broken = [r["display_name"] for r in results
              if r["verdict"] in ("NO_ITEMS", "NO_FETCH", "BLOCKED", "ERROR", "TIMEOUT")]
    if broken:
        print(f"\nNEEDS A FRESH URL OR A FIX ({len(broken)}):")
        for b in broken:
            print("   -", b)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated scraper names")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per source")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--json", default="", help="write the full report here")
    ap.add_argument("--verbose", action="store_true", help="debug-level logging")
    ap.add_argument("--dump-html", default="", metavar="DIR",
                    help="also save each source's raw page-1 HTML here, so the "
                         "right selectors can be written from the real markup")
    args = ap.parse_args()
    import logging
    # INFO, not WARNING. The lines that explain a verdict — which session was
    # used, what HTTP status came back, how many result blocks rendered — are all
    # logged at INFO, so hiding them left the table saying NO_FETCH with no way
    # to see why without going to the log file.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="  %(levelname)-7s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
