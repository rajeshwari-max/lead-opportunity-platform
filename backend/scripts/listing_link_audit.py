"""Rows whose "opportunity link" opens the list they were scraped from.

The defect
----------
DevNetJobsIndia's RFPs live on one .aspx page, and rows whose job_id could not
be recovered were stored with that page as their `opportunity_url`. 86 rows in
the 2026-08-29 database share
`https://www.devnetjobsindia.org/rfp_assignments.aspx`, so their dashboard link
opens the index rather than the RFP it names — and, because dedup keys on the
URL, they also collided with each other.

The scraper no longer does this: it recovers the id three ways and returns an
empty link when all three fail, so the row is dropped instead of shipped
pointing at the index. This is about the rows already stored.

What it does
------------
Blanks `opportunity_url` where it is the source's own listing page rather than
a link to the call. Blanking is the repair, not deletion: `resolve_link` then
labels the row honestly — `link_kind` becomes "listing" or "search" and the UI
says so — instead of presenting an index page as though it were the
opportunity. Presenting a wrong link costs more trust than admitting there
isn't one.

    python scripts/listing_link_audit.py
    python scripts/listing_link_audit.py --apply

Read-only unless --apply is given. Nothing is ever deleted.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import sqlalchemy                                          # noqa: F401
except ModuleNotFoundError:
    _root = Path(__file__).resolve().parents[1]
    _act = (".venv\\Scripts\\activate" if sys.platform == "win32"
            else "source .venv/bin/activate")
    print(f"Needs the project venv.\n\n    cd {_root}\n    {_act}\n"
          f"    python scripts/{Path(__file__).name}\n", file=sys.stderr)
    raise SystemExit(2)

from sqlalchemy import select, update                          # noqa: E402

from app.database.db import session_scope                      # noqa: E402
from app.database.models import Opportunity                    # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY             # noqa: E402


def _norm(url: str) -> str:
    """Compare on scheme+host+path. A query string or fragment is what usually
    distinguishes a real detail link, so ignoring them here would match rows
    that are perfectly fine."""
    try:
        p = urlparse((url or "").strip().lower())
    except ValueError:
        return ""
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    path = (p.path or "").rstrip("/")
    if not host:
        return ""
    return urlunparse((p.scheme or "https", host, path, "", "", ""))


def listing_urls() -> dict[str, set[str]]:
    """{display_name: {its own listing/start URLs}} from the registry itself.

    Read from the scrapers rather than listed by hand, so a source whose
    listing URL changes is still checked against the right page.
    """
    out: dict[str, set[str]] = {}
    for key, cls in SCRAPER_REGISTRY.items():
        display = getattr(cls, "display_name", key)
        urls = set()
        for attr in ("start_url", "listing_url", "base_url"):
            v = getattr(cls, attr, "")
            if isinstance(v, str) and v.startswith("http"):
                n = _norm(v)
                if n:
                    urls.add(n)
        if urls:
            out.setdefault(display, set()).update(urls)
    return out


def audit(apply: bool, examples: int) -> int:
    pages = listing_urls()
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity).where(
                Opportunity.opportunity_url.is_not(None),
                Opportunity.opportunity_url != "",
            )
        ).scalars().all()

        bad = []
        by_source: Counter = Counter()
        for r in rows:
            own = pages.get(r.source_website or "")
            if not own:
                continue
            if _norm(r.opportunity_url) in own:
                bad.append(r)
                by_source[r.source_website] += 1

        print("=" * 78)
        print("LISTING-LINK AUDIT — rows linking to their own index page")
        print("=" * 78)
        print(f"{len(rows):,} rows have a stored opportunity_url.")
        print(f"{len(bad):,} of them point at the listing page they were scraped from.")
        print()
        for source, n in by_source.most_common(20):
            print(f"    {source[:44]:<46} {n:>7,}")
        if bad:
            print()
            print("Examples:")
            for r in bad[:examples]:
                print(f"    {(r.title or '')[:56]:<58}")
                print(f"        -> {r.opportunity_url[:66]}")
        print()

        if not bad:
            print("Nothing to repair.")
            return 0

        print("Blanking the link is the repair, not deletion. resolve_link then")
        print("labels the row honestly — link_kind becomes 'listing' or 'search'")
        print("and the UI says so — instead of presenting an index page as though")
        print("it were the opportunity.")
        print()

        if not apply:
            print("DRY RUN — nothing was written.")
            print("Re-run with --apply once the sources above look right.")
            return 0

        db.execute(
            update(Opportunity)
            .where(Opportunity.id.in_([r.id for r in bad]))
            .values(opportunity_url="")
        )
        print(f"APPLIED — {len(bad):,} misleading links cleared. No row was deleted;")
        print("each keeps its title, deadline and source, and the dashboard now")
        print("labels the destination for what it is.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="clear the misleading links (default: dry run)")
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()
    return audit(a.apply, max(0, a.examples))


if __name__ == "__main__":
    raise SystemExit(main())
