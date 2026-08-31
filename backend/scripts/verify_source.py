"""Run ONE source against its verification contract and report pass or fail.

    python scripts/verify_source.py world_bank --pages 3
    python scripts/verify_source.py un_partner_portal --pages 5 --official-total 812
    python scripts/verify_source.py --all-priority --pages 2 --json verify.json

How this differs from the two tools next to it
----------------------------------------------
    validate_sources.py   which of the 85 sources are broken?   (the sweep)
    check_scraper.py      is what this ONE source produced correct?
    verify_source.py      does it MEET THE BAR, and say so in numbers?

check_scraper prints good measurements and leaves a person to decide whether
they are acceptable — so the answer changes with the reader, and "DevelopmentAid
is fine now" ends up being an opinion. This applies the thresholds in
services/verification.py and returns a non-zero exit code when a source misses
them, which is the difference between a report and a check.

Nothing is written. The database is opened read-only and only to answer "how
many of these rows are already stored"; pass --no-db to skip even that.

The coverage rule
-----------------
Coverage is unique / OFFICIAL TOTAL, and there is no way for this script to
invent the second number. Supply it with --official-total, taken from where
that source publishes it (the contract names the place for each). Without it
the report says `coverage: unproven` and names what would prove it — it does
NOT quietly divide our count by our count and print 100%.
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

from app.services import verification as V  # noqa: E402
from app.services.deadline_parser import DeadlineParser  # noqa: E402
from app.services.links import is_furniture, is_usable_link, link_kind  # noqa: E402
from app.services.notice_types import record_type_for  # noqa: E402
from app.services.opportunity_gate import is_opportunity  # noqa: E402
from app.services.source_manifest import contract_for as scope_for  # noqa: E402
from app.services.source_manifest import record_is_in_scope  # noqa: E402
from app.services.spam import is_spam  # noqa: E402

log = logging.getLogger("verify")
_deadlines = DeadlineParser()


# --------------------------------------------------------------- browsers

def _automation_browser(name: str, command: str) -> bool:
    """A scraper-owned browser, excluding an ordinary interactive Chrome."""
    name = (name or "").lower()
    command = (command or "").lower()
    return (
        "headless_shell" in name
        or "chrome-headless-shell" in name
        or "--headless" in command
        or "ms-playwright" in command
        or "playwright_chromiumdev_profile" in command
    )


def browser_count() -> int | None:
    """Automation-browser processes alive now, or None if unknowable.

    None, never 0. A leak check that did not run and a leak check that found
    nothing look identical in a report, and only one of them is evidence.

    An ordinary Chrome window can add renderer processes while a scrape runs;
    counting every Chrome process therefore reports a leak that belongs to the
    user's browsing session. Count only headless/Playwright command lines.

    psutil first, then the platform's own process list. The first verification
    run reported "not measured" for all eleven sources because psutil is not in
    the venv — an UNPROVEN on every row, which is the reading that trains
    people to skip the section. Falling back to `tasklist` and `pgrep` costs
    one subprocess and removes the excuse.
    """
    try:
        import psutil
    except ImportError:
        pass
    else:
        n = 0
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                command = " ".join(p.info.get("cmdline") or [])
            except Exception:                   # pragma: no cover - race
                continue
            if _automation_browser(name, command):
                n += 1
        return n

    import subprocess
    try:
        if sys.platform.startswith("win"):
            script = (
                "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
                "Where-Object { $_.Name -match 'chrome|chromium' } | "
                "ForEach-Object { $_.Name + \"`t\" + $_.CommandLine }"
            )
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=30).stdout
            return sum(1 for line in out.splitlines()
                       if _automation_browser(line.split("\t", 1)[0], line))
        out = subprocess.run(
            ["ps", "-e", "-o", "comm=,args="],
            capture_output=True, text=True, timeout=30).stdout
        return sum(1 for line in out.splitlines()
                   if _automation_browser(line.split(maxsplit=1)[0], line))
    except Exception:                           # noqa: BLE001
        # Genuinely could not tell. None, and the report says so.
        return None


# ------------------------------------------------------------------- crawl

async def crawl(scraper, pages: int, timeout: float):
    stop, pause = asyncio.Event(), asyncio.Event()
    pause.set()
    items: list = []
    fetched = 0
    note = ""
    timed_out = False
    exception = ""

    async def progress(event, payload):
        if event == "page_error":
            print(f"  !! page {payload.get('page')} {payload.get('url','')[:90]}")

    async def walk():
        nonlocal fetched
        iterator = scraper.crawl(stop, pause, progress)
        try:
            async for batch in iterator:
                fetched += 1
                items.extend(batch)
                print(f"  .. page {fetched}: {len(batch)} item(s)", flush=True)
                if fetched >= pages:
                    stop.set()
                    break
        finally:
            # Breaking an ``async for`` does not guarantee an async generator's
            # cleanup has finished before the next line measures browser
            # processes. Explicit close makes the leak check a real after-state.
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                await closer()

    try:
        await asyncio.wait_for(walk(), timeout=timeout)
    except asyncio.TimeoutError:
        stop.set()
        timed_out = True
        note = f"timed out after {timeout:.0f}s with {fetched} page(s)"
        print(f"  !! {note}")
    except Exception as exc:                                    # noqa: BLE001
        stop.set()
        exception = f"{type(exc).__name__}: {exc}"
        note = exception
        print(f"  !! {note}")
    return items, fetched, note, timed_out, exception


def classify_run(scraper, items, pages_fetched, timed_out, exception):
    """Name what happened, using the platform's own outcome taxonomy.

    The first verification run reported `outcome: unrecorded` for all eleven
    sources, so the two BLOCKING findings both read "no page was fetched
    (outcome: unrecorded)" — which is precisely the uninformative state the
    outcome taxonomy exists to replace. Devex not being reachable behind a
    paywall and Clean Air Fund's URL failing outright are different problems
    needing different people, and "unrecorded" says neither.

    This is a best-effort read from outside the scraper: it cannot see the HTTP
    status the way the ingest path can, so it distinguishes what it honestly
    can and leaves the rest to the log.
    """
    from app.services.scrape_outcome import Evidence, classify

    ev = Evidence(
        pages_fetched=pages_fetched,
        extracted=len(items),
        timed_out=timed_out,
        exception=exception,
        fetch_mode="browser" if getattr(scraper, "requires_js", False) else "http",
        auth_required=bool(getattr(scraper, "requires_login", False))
        and pages_fetched == 0,
    )
    outcome, code, message = classify(ev)
    return outcome.value, (code.value if code else ""), message


# ---------------------------------------------------------------- measuring

def measure(scraper, items, pages_fetched, seconds, official_total,
            before, after, use_db: bool) -> V.SourceVerification:
    """Turn one run's rows into the contract's numbers.

    The gates below are the REAL ones the ingest path runs, imported rather
    than re-implemented. A verification that models the pipeline instead of
    calling it verifies the model.
    """
    key = scraper.name
    r = V.SourceVerification(key=key, display_name=scraper.display_name)
    r.pages_fetched = pages_fetched
    r.runtime_s = seconds
    r.official_total = official_total
    r.browsers_before, r.browsers_after = before, after
    r.extracted = len(items)
    if not items:
        return r

    # --- unique --------------------------------------------------------------
    # By URL, because that is what the reader clicks and what deduplication
    # ultimately keys on. A row with no URL cannot collide with another, so it
    # counts as its own.
    seen: set[str] = set()
    unique_items = []
    for i in items:
        u = (i.opportunity_url or "").strip()
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        unique_items.append(i)
    r.unique = len(unique_items)
    r.duplicates = r.extracted - r.unique

    # --- completeness, measured over everything extracted --------------------
    n = r.extracted
    # Three link numbers, because they answer three different questions and
    # collapsing them scored DevNetJobsIndia as a failure for behaving
    # correctly. `kept` is what would reach a reader; the rows the scraper
    # drops for want of a link are loss, not badness.
    kept = [i for i in items if is_usable_link(i.opportunity_url, i.website)]
    dropped = n - len(kept)
    deep_all = sum(1 for i in items
                   if is_usable_link(i.opportunity_url, i.website)
                   and link_kind(i.opportunity_url) == "deep")
    r.deep_link_pct = 100.0 * deep_all / len(kept) if kept else 0.0
    r.deep_link_extracted_pct = 100.0 * deep_all / n
    r.link_loss_pct = 100.0 * dropped / n
    dated = [i for i in items if (i.deadline_raw or "").strip()]
    parsed = 0
    for i in dated:
        try:
            if (_deadlines.parse(i.deadline_raw, dayfirst=i.dayfirst)
                    or _deadlines.is_ongoing(i.deadline_raw)):
                parsed += 1
        except Exception:                                        # noqa: BLE001
            pass
    r.deadline_present_pct = 100.0 * len(dated) / n
    # Of the rows that CARRY a string — a source printing no dates is a fact
    # about the source; a string we cannot read is a defect in us.
    r.deadline_parse_pct = 100.0 * parsed / len(dated) if dated else 100.0
    r.organization_pct = 100.0 * sum(
        1 for i in items if (i.organization or "").strip()) / n
    r.furniture_rows = sum(
        1 for i in items if is_furniture(i.title or "", i.opportunity_url or ""))

    # --- what would actually be stored, and why the rest would not -----------
    contract = scope_for(key, scraper.display_name)
    curated = bool(getattr(scraper, "curated", False))
    # Bespoke parsers can reject a record while its structured source fields
    # are still available (for example a World Bank Contract Award). Preserve
    # those visible rejection counts instead of losing them before this report.
    excluded: dict[str, int] = dict(
        getattr(scraper, "rejection_counts", lambda: {})() or {})

    def drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    saved = 0
    for i in unique_items:
        if not is_usable_link(i.opportunity_url, i.website):
            drop("no usable link")
            continue
        rt = i.record_type or record_type_for(i.source_status or "")
        keep, why = record_is_in_scope(contract, rt, i.source_status)
        if not keep:
            # The reason verbatim, so "241 contract awards" reads differently
            # from "241 excluded" — and an inverted ratio later is a visible
            # signal that the source's vocabulary changed.
            drop(why or "out of scope")
            continue
        if is_spam(i.title, i.summary):
            drop("spam")
            continue
        ok, gate_why = is_opportunity(i.title, i.summary, i.opportunity_url,
                                      "", curated)
        if not ok:
            drop(f"opportunity gate: {gate_why}")
            continue
        saved += 1
    r.saved = saved
    r.excluded = excluded

    # --- already stored ------------------------------------------------------
    if use_db:
        try:
            from sqlalchemy import func, select

            from app.database.db import session_scope
            from app.database.models import Opportunity
            with session_scope() as db:
                stored = db.execute(
                    select(func.count(Opportunity.id))
                    .where(Opportunity.source_website == scraper.display_name)
                ).scalar_one()
            r.notes.append(f"{stored:,} row(s) already stored under "
                           f"{scraper.display_name!r}")
        except Exception as exc:                                 # noqa: BLE001
            r.notes.append(f"database not consulted: {type(exc).__name__}")

    return r


# ------------------------------------------------------------------ output

def show(r: V.SourceVerification) -> None:
    c = V.contract_for(r.key)
    d = r.as_dict()
    print("\n" + "=" * 78)
    print(f"{r.display_name}  ({r.key})")
    print("=" * 78)

    print("\nCOUNTS")
    print(f"  official total (source-reported) : "
          f"{r.official_total if r.official_total is not None else 'not supplied'}")
    print(f"  extracted                        : {r.extracted:,}")
    print(f"  unique                           : {r.unique:,}")
    print(f"  duplicates within the run        : {r.duplicates:,} "
          f"({r.duplicate_pct:.1f}%)")
    print(f"  would be stored                  : {r.saved:,}")
    print(f"  excluded                         : {r.excluded_total:,}")
    for reason, count in sorted(r.excluded.items(), key=lambda kv: -kv[1]):
        print(f"      {count:>6,}  {reason[:60]}")

    print("\nPAGINATION")
    print(f"  pages expected                   : "
          f"{r.pages_expected if r.pages_expected is not None else 'unknown'}")
    print(f"  pages fetched                    : {r.pages_fetched}")

    print("\nCOMPLETENESS")
    print(f"  stored rows opening the call     : {r.deep_link_pct:5.1f}%   "
          f"(bar {c.min_deep_link_pct:.0f}%)")
    print(f"  ... of everything extracted      : {r.deep_link_extracted_pct:5.1f}%")
    print(f"  dropped for want of a link       : {r.link_loss_pct:5.1f}%   "
          f"(bar {c.max_link_loss_pct:.0f}%)")
    print(f"  carry a deadline string          : {r.deadline_present_pct:5.1f}%")
    print(f"  ... that parses into a date      : {r.deadline_parse_pct:5.1f}%   "
          f"(bar {c.min_deadline_parse_pct:.0f}%)")
    print(f"  name an organisation             : {r.organization_pct:5.1f}%   "
          f"(bar {c.min_organization_pct:.0f}%)")
    print(f"  navigation furniture stored      : {r.furniture_rows}")

    print("\nCOVERAGE")
    print(f"  {d['coverage_pct']}"
          + (f"%  of {r.official_total:,}" if r.coverage_pct is not None else ""))
    print(f"  basis: {d['coverage_basis']}")

    print("\nOPERATIONAL")
    print(f"  runtime                          : {r.runtime_s:.0f}s   "
          f"(cap {c.max_runtime_s:.0f}s)")
    print(f"  browsers before / after          : "
          f"{r.browsers_before} / {r.browsers_after}")
    leaked = r.leaked_browsers
    print(f"  leaked                           : "
          f"{'not measured' if leaked is None else leaked}")

    print(f"\nACCESS LIMITATIONS\n  {c.access_limitations}")
    if r.notes:
        print("\nNOTES")
        for note in r.notes:
            print(f"  - {note}")

    findings = r.evaluate(c)
    print("\nVERDICT:", "PASS" if r.passed(c) else "FAIL")
    for f in findings:
        print(f"  {f}")
    if not findings:
        print("  every check in this source's contract was met.")


# -------------------------------------------------------------------- main

def resolve(name: str):
    from app.scrapers.registry import SCRAPER_REGISTRY, get_scrapers

    if name in SCRAPER_REGISTRY:
        return get_scrapers([name])[0]
    want = name.lower().replace(" ", "")
    hits = [n for n, cls in SCRAPER_REGISTRY.items()
            if want in n.lower() or want in cls.display_name.lower().replace(" ", "")]
    if len(hits) == 1:
        return get_scrapers(hits)[0]
    print(f"{name!r} matches {len(hits)} scraper(s). Registered names:\n  "
          + "\n  ".join(sorted(SCRAPER_REGISTRY)), file=sys.stderr)
    raise SystemExit(2)


def verify_one(name: str, args) -> V.SourceVerification:
    scraper = resolve(name)
    print(f"\n>>> {scraper.display_name}: up to {args.pages} page(s), "
          f"{args.timeout:.0f}s. Nothing is written.")
    before = browser_count()
    started = time.monotonic()
    items, fetched, note, timed_out, exception = asyncio.run(
        crawl(scraper, args.pages, args.timeout))
    seconds = time.monotonic() - started
    after = browser_count()
    r = measure(scraper, items, fetched, seconds, args.official_total,
                before, after, use_db=not args.no_db)
    outcome, code, message = classify_run(scraper, items, fetched, timed_out,
                                          exception)
    r.outcome = outcome
    if message:
        r.notes.append(f"{outcome}"
                       + (f" / {code}" if code else "") + f": {message}")
    if note:
        r.notes.append(note)
    if args.note:
        # The place to record "a person-established DevelopmentAid session was
        # used", which is what clears that source's precondition finding.
        r.notes.extend(args.note)
    # `--pages N` is a bound the operator chose, so falling short of it is only
    # meaningful when the source had more to give. Recorded, not guessed.
    if fetched and fetched < args.pages:
        r.notes.append(f"the walk ended after {fetched} of the {args.pages} "
                       f"page(s) allowed — either the source ran out, or it "
                       f"stopped early")
    show(r)
    return r


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", default="",
                    help="scraper name, e.g. world_bank")
    ap.add_argument("--all-priority", action="store_true",
                    help="every source in verification.PRIORITY_SOURCES")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--official-total", type=int, default=None,
                    help="the count the SOURCE reports. Without it, coverage "
                         "is reported as unproven rather than invented.")
    ap.add_argument("--note", action="append", default=[],
                    help="record a precondition, e.g. --note 'a "
                         "person-established DevelopmentAid session was used'")
    ap.add_argument("--no-db", action="store_true",
                    help="do not open the database even read-only")
    ap.add_argument("--json", default="", help="write the full result here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.source and not args.all_priority:
        ap.error("name a source, or pass --all-priority")
    if args.all_priority and args.official_total is not None:
        ap.error("--official-total applies to one source; run them separately")

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="  %(levelname)-7s %(name)s: %(message)s")

    import app.scrapers  # noqa: F401 — importing registers every plugin

    names = list(V.PRIORITY_SOURCES) if args.all_priority else [args.source]
    results = [verify_one(n, args) for n in names]

    if len(results) > 1:
        s = V.summarize(results)
        print("\n" + "=" * 78)
        print(f"{s['passed']} of {s['sources']} source(s) met their contract. "
              f"{s['blocking']} blocking finding(s). "
              f"{s['unproven_coverage']} source(s) could not prove coverage.")
        print("=" * 78)
        for r in results:
            print(f"  {'PASS' if r.passed() else 'FAIL'}  {r.display_name:<24} "
                  f"{r.unique:>6,} unique  "
                  f"coverage {r.as_dict()['coverage_pct']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps([r.as_dict() for r in results], indent=1, default=str),
            encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if all(r.passed() for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
