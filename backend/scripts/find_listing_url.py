"""Find the page on a funder's site that actually lists OPEN calls.

    python scripts/find_listing_url.py clean_air_fund
    python scripts/find_listing_url.py cleanair rockefeller gates laudes cjrf
    python scripts/find_listing_url.py --all --write

Why this exists
---------------
Nine sources in the coverage audit point at a page that lists money already
given: "our grants", "committed grants", "grantees", "grants database", "past
calls". Whatever those pages yield, nobody can apply to it. No parser change
reaches that — the URL is simply aimed at the wrong page.

The obvious fix is to repoint each source at its real funding page. The
tempting way to do that is to guess a path like /funding-opportunities and move
on. That is how World Bank ended up with a pagination template that had been
doing nothing for months: a URL that LOADS tells you nothing about whether it
lists what you want.

So this measures instead. For every candidate URL it fetches the page, runs the
source's own parser, puts every row through `services/opportunity_gate.py`, and
scores the page on what survives. The winner is the URL that yields the most
rows that are real opportunities — not the one that returns the most rows, and
not the one that happens to respond.

Candidates come from two places
-------------------------------
1. The site's own navigation. Links whose text or href talks about funding,
   opportunities, applying, calls, RFPs or tenders. A path the site offers is
   worth more than one invented here, and it costs nothing to read.
2. A list of conventional paths, tried only on the same domain.

What the score means
--------------------
    rows      parsed off the page at all
    open      survived the opportunity gate
    dated     of those, how many carry a deadline
    awarded   rejected as grants already given — the signal that this page is
              a grantee list rather than a call list

A page scoring `rows 40 / open 0 / awarded 38` is a grantee page, and saying so
is more useful than any repointing: that funder may publish no open calls at
all, in which case the honest action is to disable the source rather than keep
scraping a page that cannot contain what we want.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.scrapers                                      # noqa: E402,F401
from app.scrapers.generic_listing import CONFIG_PATH     # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY       # noqa: E402
from app.services.opportunity_gate import is_opportunity  # noqa: E402

log = logging.getLogger("find")

# Conventional places a funder puts its open calls. Ordered roughly by how
# often they are the real answer.
CANDIDATE_PATHS = (
    "/funding-opportunities", "/funding-opportunities/", "/open-calls",
    "/open-calls/", "/calls", "/calls/", "/call-for-proposals",
    "/funding", "/funding/", "/apply", "/apply/", "/grants/apply",
    "/opportunities", "/opportunities/", "/grant-opportunities",
    "/current-opportunities", "/rfp", "/rfps", "/tenders",
    "/how-to-apply", "/grants/open-calls", "/what-we-fund",
)

# Link text or href that suggests a page of open calls. Deliberately narrower
# than the opportunity gate's vocabulary: this is choosing where to LOOK, and a
# link saying "our grantees" must not be followed.
_PROMISING = re.compile(
    r"(funding\s+opportunit|open\s+call|call\s+for\s+(proposal|application|"
    r"expression)|current\s+(call|opportunit|funding)|apply\s+for|"
    r"how\s+to\s+apply|request\s+for\s+proposal|grant\s+opportunit|"
    r"available\s+(grant|funding)|opportunities|tenders?|rfps?)",
    re.IGNORECASE,
)
_UNPROMISING = re.compile(
    r"(grantee|awarded|past\s+(grant|call)|our\s+grants|committed|"
    r"portfolio|recipients?|winners?|annual\s+report|news|blog)",
    re.IGNORECASE,
)


def score_page(scraper, html: str, url: str) -> dict:
    """Parse a page and measure what survives the opportunity gate."""
    try:
        items = scraper.parse_listing(html, url)
    except Exception:                                       # noqa: BLE001
        return {"rows": 0, "open": 0, "dated": 0, "awarded": 0, "error": "parse raised"}
    kept, dated, awarded = [], 0, 0
    for i in items:
        ok, why = is_opportunity(
            i.title, i.summary, i.opportunity_url,
            str(getattr(i.category_hint, "value", i.category_hint) or ""),
            bool(getattr(scraper, "curated", False)))
        if ok:
            kept.append(i)
            if (i.deadline_raw or "").strip():
                dated += 1
        elif "awarded" in why:
            awarded += 1
    return {"rows": len(items), "open": len(kept), "dated": dated,
            "awarded": awarded, "error": "",
            "samples": [i.title[:70] for i in kept[:3]]}


class Finder:
    def __init__(self, scraper, headless: bool, timeout: int):
        self.s = scraper
        self.headless = headless
        self.timeout = timeout
        self.page = None

    def fetch(self, url: str) -> str:
        try:
            self.page.goto(url, timeout=self.timeout * 1000,
                           wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            self.page.wait_for_timeout(1_200)
            return self.page.content()
        except Exception as exc:                            # noqa: BLE001
            log.debug("fetch failed %s: %s", url, exc)
            return ""

    def nav_candidates(self, base: str) -> list[str]:
        """Links the site itself offers that look like a page of open calls."""
        try:
            links = self.page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => [(e.textContent||'').trim().slice(0,80),"
                " e.getAttribute('href')||''])") or []
        except Exception:
            return []
        host = urlparse(base).netloc.replace("www.", "")
        out, seen = [], set()
        for text, href in links:
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            blob = f"{text} {href}"
            if _UNPROMISING.search(blob) or not _PROMISING.search(blob):
                continue
            url = urljoin(base, href)
            if urlparse(url).netloc.replace("www.", "") != host:
                continue
            if url.rstrip("/") in seen or url.rstrip("/") == base.rstrip("/"):
                continue
            seen.add(url.rstrip("/"))
            out.append(url)
        return out[:12]

    def run(self) -> dict:
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        base = self.s.start_url
        result = {"name": self.s.name, "display": self.s.display_name,
                  "current_url": base, "best_url": "", "verdict": "", "tried": []}

        with sync_playwright() as pw:
            context = site_auth.open_context(pw, self.s.name, headless=self.headless)
            try:
                self.page = context.pages[0] if context.pages else context.new_page()

                html = self.fetch(base)
                current = score_page(self.s, html, base) if html else {
                    "rows": 0, "open": 0, "dated": 0, "awarded": 0,
                    "error": "did not load"}
                current["url"] = base
                current["how"] = "configured"
                self._print(current)
                result["tried"].append(current)

                seen = {base.rstrip("/")}
                candidates = [(u, "site navigation") for u in self.nav_candidates(base)]
                root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
                candidates += [(root + p, "conventional path") for p in CANDIDATE_PATHS]

                for url, how in candidates:
                    if url.rstrip("/") in seen:
                        continue
                    seen.add(url.rstrip("/"))
                    html = self.fetch(url)
                    if not html:
                        continue
                    row = score_page(self.s, html, url)
                    if not row["rows"]:
                        continue          # nothing parsed: not worth reporting
                    row["url"] = url
                    row["how"] = how
                    self._print(row)
                    result["tried"].append(row)

                # Best = most gate-surviving rows, then most with a deadline.
                ranked = sorted(result["tried"],
                                key=lambda r: (r["open"], r["dated"]), reverse=True)
                best = ranked[0] if ranked else None
                if best and best["open"] and best["url"] != base:
                    result["best_url"] = best["url"]
                    result["verdict"] = "REPOINT"
                elif best and best["open"]:
                    result["verdict"] = "ALREADY CORRECT"
                elif any(r["awarded"] for r in result["tried"]):
                    result["verdict"] = "AWARDS ONLY"
                else:
                    result["verdict"] = "NO OPEN CALLS FOUND"
                return result
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    @staticmethod
    def _print(r: dict) -> None:
        flag = "  <-- best so far" if r["open"] else ""
        print(f"    rows {r['rows']:>3}  open {r['open']:>3}  dated {r['dated']:>3}  "
              f"awarded {r['awarded']:>3}   [{r['how']}] {r['url'][:74]}{flag}")
        for t in r.get("samples", [])[:2]:
            print(f"        + {t}")


def write_config(results: list[dict]) -> int:
    entries = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    by_name = {e.get("name"): e for e in entries}
    changed = 0
    for r in results:
        if r["verdict"] != "REPOINT" or not r["best_url"]:
            continue
        entry = by_name.get(r["name"])
        if entry is None:
            print(f"  ! {r['display']} has its own module — change its start_url there")
            continue
        print(f"  + {r['display']}\n      {entry.get('url')}\n   -> {r['best_url']}")
        entry["url"] = r["best_url"]
        changed += 1
    if changed:
        CONFIG_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="apply the recommended url to sources.json")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")

    if args.all:
        names = list(SCRAPER_REGISTRY)
    else:
        names = []
        for want in args.sources:
            w = want.strip().lower()
            hits = [n for n, c in SCRAPER_REGISTRY.items()
                    if w in n.lower() or w in c.display_name.lower().replace(" ", "")]
            if not hits:
                print(f"no scraper matches {want!r}", file=sys.stderr)
                return 2
            names.extend(hits)
    if not names:
        ap.print_help()
        return 2

    scrapers = [SCRAPER_REGISTRY[n]() for n in dict.fromkeys(names)]
    print(f"\nChecking {len(scrapers)} source(s). Nothing is written to the "
          f"database.\n" + "=" * 100)
    results = []
    for s in scrapers:
        print(f"\n{s.display_name}  ({s.name})")
        try:
            r = Finder(s, not args.headed, args.timeout).run()
        except Exception as exc:                            # noqa: BLE001
            r = {"name": s.name, "display": s.display_name, "current_url": s.start_url,
                 "best_url": "", "verdict": "ERROR", "tried": [],
                 "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
        print(f"    => {r['verdict']}" + (f": {r['best_url']}" if r["best_url"] else ""))

    print("\n" + "=" * 100 + "\nSUMMARY\n" + "=" * 100)
    for r in results:
        print(f"  {r['verdict']:20} {r['display'][:30]:32} {r['best_url'][:56]}")

    dead = [r for r in results if r["verdict"] in ("AWARDS ONLY", "NO OPEN CALLS FOUND")]
    if dead:
        print(f"\n  {len(dead)} source(s) show no open calls anywhere on their site.")
        print("  Scraping these cannot produce a lead. Consider removing them from")
        print("  sources.json rather than leaving them to add noise every run.")

    repoint = [r for r in results if r["verdict"] == "REPOINT"]
    if repoint and not args.write:
        print(f"\n  {len(repoint)} source(s) have a better URL. Re-run with --write "
              f"to apply.")
    elif repoint:
        print()
        n = write_config(repoint)
        print(f"\nUpdated {n} entr{'y' if n == 1 else 'ies'} in {CONFIG_PATH}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
