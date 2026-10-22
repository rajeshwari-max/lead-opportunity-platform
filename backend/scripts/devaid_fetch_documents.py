"""Fetch summaries, eligibility and official documents for DevelopmentAid
leads already in the database, by opening each one in the signed-in browser.

    python scripts/devaid_fetch_documents.py --dry-run      # how many, no browser
    python scripts/devaid_fetch_documents.py --limit 50     # try a small batch
    python scripts/devaid_fetch_documents.py                # all open leads
    python scripts/devaid_fetch_documents.py --all          # closed ones too

Which leads: DevelopmentAid rows whose documents were never read, or hold
only the website's footer file. Leads already read with no document found
are skipped unless --retry-empty. Re-running resumes where it stopped.

Safe by construction:
- uses the same signed-in browser profile and page reader as the scraper,
  and takes the same run lease, so it cannot run beside a scrape;
- stops by itself at --until (default 21:45 India time) so the 22:00 nightly
  scrape is not blocked;
- only fills empty summary/eligibility and replaces empty or footer-only
  document lists; never deletes a row or overwrites a real document list;
- one page at a time with a pause (--delay) — be gentle with the account.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select, update  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.db import session_scope  # noqa: E402
from app.database.models import Opportunity  # noqa: E402
from app.services.actionable import actionable_clause  # noqa: E402
from app.services.site_chrome import is_site_chrome_document  # noqa: E402

SOURCE = "DevelopmentAid"
IST = ZoneInfo("Asia/Kolkata")


def needs_documents(stored: str | None, retry_empty: bool) -> bool:
    if stored is None or not stored.strip():
        return True
    if is_site_chrome_document(stored):
        return True
    return retry_empty and stored.strip() == "[]"


def select_leads(include_closed: bool, retry_empty: bool, limit: int) -> list[dict]:
    with session_scope() as db:
        stmt = select(Opportunity.id, Opportunity.title, Opportunity.opportunity_url,
                      Opportunity.summary, Opportunity.eligibility,
                      Opportunity.documents_json).where(
            Opportunity.source_website == SOURCE,
            Opportunity.opportunity_url.is_not(None),
            Opportunity.opportunity_url != "",
        )
        if not include_closed:
            stmt = stmt.where(actionable_clause())
        stmt = stmt.where(or_(Opportunity.documents_json.is_(None),
                              Opportunity.documents_json.in_(["", "[]"]),
                              Opportunity.documents_json.like("%9001%"),
                              Opportunity.documents_json.like("%QMSCERT%"),
                              Opportunity.documents_json.like("%/cms/file/%")))
        rows = db.execute(stmt.order_by(Opportunity.deadline.is_(None),
                                        Opportunity.deadline)).all()
    out = [dict(id=r[0], title=r[1], url=r[2], summary=r[3] or "",
                eligibility=r[4] or "", docs=r[5])
           for r in rows if needs_documents(r[5], retry_empty)]
    return out[:limit] if limit else out


def save(lead: dict, item) -> str:
    """Write what the page showed. Returns 'documents' | 'none' | 'skipped'."""
    if not item.documents_checked:
        return "skipped"
    docs = [d for d in (item.source_documents or [])
            if isinstance(d, dict) and str(d.get("url", "")).startswith(("http://", "https://"))
            and not is_site_chrome_document(d)]
    pdf = next((d["url"] for d in docs if ".pdf" in d["url"].casefold()
                or d.get("kind", "").casefold() == "pdf"), None)
    now = datetime.now(timezone.utc)
    values = dict(documents_json=json.dumps(docs, ensure_ascii=False),
                  pdf_url=pdf[:1024] if pdf else None,
                  pdf_status="found" if pdf else "none", pdf_checked_at=now)
    if not lead["summary"] and item.summary:
        values["summary"] = item.summary
    if not lead["eligibility"] and item.eligibility:
        values["eligibility"] = item.eligibility
    with session_scope() as db:
        db.execute(update(Opportunity).where(Opportunity.id == lead["id"]).values(**values))
    return "documents" if docs else "none"


def save_debug(page, scraper, leads: list[dict]) -> None:
    """Write the rendered page and every JSON response for a few leads, so the
    page structure can be inspected when summaries or files are not found.
    Contains only opportunity content — no cookies or session data."""
    import re as _re

    out = Path(__file__).resolve().parents[1] / "logs" / "devaid_debug"
    out.mkdir(parents=True, exist_ok=True)
    for lead in leads:
        oid = (_re.search(r"/view/(\d+)", lead["url"]) or [None, "x"])[1]
        captured: list[dict] = []

        def keep(resp, oid=oid, captured=captured):
            try:
                if "json" in (resp.headers or {}).get("content-type", "").lower() \
                        and "/api/" in resp.url:
                    captured.append({"url": resp.url, "status": resp.status,
                                     "body": resp.json()})
            except Exception:
                pass

        page.on("response", keep)
        try:
            response = page.goto(lead["url"], timeout=90_000, wait_until="domcontentloaded")
            if response is not None and response.status in (403, 503):
                scraper._wait_out_challenge(page, "debug", 45_000)
            scraper._wait_for_detail(page, lead["title"])
            page.wait_for_timeout(3_000)
            (out / f"{oid}.html").write_text(page.content(), encoding="utf-8")
            (out / f"{oid}.json").write_text(
                json.dumps(captured, ensure_ascii=False, indent=1)[:5_000_000],
                encoding="utf-8")
            print(f"debug: saved logs/devaid_debug/{oid}.html and {oid}.json "
                  f"({len(captured)} JSON responses; title {page.title()!r})")
        except Exception as exc:
            print(f"debug: {oid} failed: {type(exc).__name__}: {exc}")
        finally:
            page.remove_listener("response", keep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="include closed/expired leads")
    ap.add_argument("--retry-empty", action="store_true",
                    help="also re-read leads already checked with no document found")
    ap.add_argument("--limit", type=int, default=0, help="stop after N leads (0 = all)")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between leads")
    ap.add_argument("--until", default="21:45",
                    help="India time to stop by, HH:MM (blank = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="count only; no browser")
    ap.add_argument("--debug", type=int, default=0,
                    help="save what the browser sees for the first N leads "
                         "(page HTML + the site's JSON) to logs/devaid_debug/")
    a = ap.parse_args()

    leads = select_leads(a.all, a.retry_empty, a.limit)
    print(f"{len(leads):,} DevelopmentAid lead(s) need documents "
          f"({'including' if a.all else 'open leads only, excluding'} closed ones).")
    if a.dry_run or not leads:
        return 0

    stop_at = None
    if a.until:
        h, m = (int(x) for x in a.until.split(":"))
        now_ist = datetime.now(IST)
        stop_at = now_ist.replace(hour=h, minute=m, second=0, microsecond=0)
        if stop_at <= now_ist:
            print(f"It is already past {a.until} India time — run this earlier in "
                  f"the day, or pass --until with a later time.")
            return 1

    from playwright.sync_api import sync_playwright

    from app.schemas.opportunity import RawOpportunity
    from app.scrapers.devaid_auth import membership_state, open_persistent
    from app.scrapers.developmentaid import DevelopmentAidScraper
    from app.services import run_lock

    try:
        lease = run_lock.acquire(label="DevelopmentAid document fetch")
    except run_lock.LeaseNotAcquired as exc:
        print(f"Not started: {exc}")
        return 1

    counts = {"documents": 0, "none": 0, "skipped": 0}
    scraper = DevelopmentAidScraper()
    settings.devaid_detail_enrichment_limit = 10 ** 9   # this run reads them all
    try:
        with sync_playwright() as pw:
            browser = open_persistent(pw, headless=settings.devaid_headless)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                page.goto(leads[0]["url"], timeout=90_000, wait_until="domcontentloaded")
                scraper._wait_for_detail(page, leads[0]["title"])
                state, evidence = membership_state(page)
                if state == "out":
                    print("DevelopmentAid is NOT signed in on this server "
                          f"({evidence}). Sign in again, then re-run.")
                    return 1
                print(f"Signed-in check: {state} ({evidence})")
                if a.debug:
                    save_debug(page, scraper, leads[:a.debug])

                for n, lead in enumerate(leads, 1):
                    if stop_at and datetime.now(IST) >= stop_at:
                        print(f"Reached {a.until} India time — stopping so the "
                              f"nightly scrape can run. Re-run tomorrow to continue.")
                        break
                    item = RawOpportunity(
                        title=lead["title"] or "", source_website=SOURCE,
                        website="https://www.developmentaid.org",
                        opportunity_url=lead["url"], summary=lead["summary"],
                        eligibility=lead["eligibility"])
                    scraper._enrich_from_signed_in_details(page, [item])
                    result = save(lead, item)
                    counts[result] += 1
                    why = ""
                    if result == "skipped":
                        why = "  <- " + getattr(scraper, "_detail_skip_reasons", {}).get(
                            lead["url"], "not opened (already complete or limit reached)")
                    print(f"[{n}/{len(leads)}] {result:9} {(lead['title'] or '')[:60]}{why}",
                          flush=True)
                    if n % 10 == 0 and not run_lock.heartbeat(lease):
                        print("Lost the run lease — stopping.")
                        break
                    time.sleep(max(0.0, a.delay))
            finally:
                browser.close()
    finally:
        run_lock.release(lease)

    print(f"\nDone. With documents: {counts['documents']:,}   "
          f"read, none published: {counts['none']:,}   "
          f"could not read: {counts['skipped']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
