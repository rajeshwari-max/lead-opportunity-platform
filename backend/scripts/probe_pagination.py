"""Find out how a source paginates — by trying it, not by guessing.

    python scripts/probe_pagination.py undp_procurement
    python scripts/probe_pagination.py undp_procurement --write
    python scripts/probe_pagination.py --all --only-unconfigured
    python scripts/probe_pagination.py world_bank --pages 3 --headed

Why this exists
---------------
71 of the 85 sources have no pagination configuration. They depend on
`generic_listing.next_page()` recognising the site's own controls, and that
detector knows four shapes: `rel="next"`, a numbered link inside a container
whose class matches "pag", an arrow/"load more" label, and a page number
already present in the URL. Anything else and the crawl stops at page 1 —
silently, because page 1 returns rows and the run reports success.

Reading the site's markup by hand and writing a `page_url` template is the fix,
but it is 71 sites of manual work and the answer is often wrong on the first
try: a URL that *loads* is not the same as a URL that returns *different
listings*. Several boards answer an out-of-range page by re-serving page 1, and
some accept `?page=2` while ignoring it entirely.

So this probes instead. For each candidate it fetches the page, runs the
source's OWN parser over it, and compares the set of opportunity URLs against
page 1. A candidate only wins if it produces listings that page 1 did not — the
same test a human would apply, applied consistently.

What it reports
---------------
  CONFIGURED    a template that works and is already in sources.json
  FOUND         a template that works and should be added (use --write)
  SINGLE PAGE   page 2 exists but repeats page 1 — the listing really is one
                page, and no template is needed
  NO CANDIDATE  nothing produced a different set of rows; the site probably
                paginates by XHR or infinite scroll and needs its own module
  NO BASELINE   page 1 itself parsed to nothing, so there is nothing to
                compare against — fix the source before probing pagination
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.scrapers                                    # noqa: E402,F401 (registers)
from app.scrapers.generic_listing import CONFIG_PATH   # noqa: E402
from app.scrapers.registry import SCRAPER_REGISTRY     # noqa: E402

log = logging.getLogger("probe")

# Query parameters that mean "page number", and the ones that mean "row offset".
# They are kept apart because the template that comes out the other end differs:
# {page} counts pages, {offset} counts rows, and using the wrong one slides the
# window by a single row per request instead of a whole page — the bug that once
# cost World Bank nine of every ten results.
PAGE_PARAMS = ("page", "paged", "pg", "p", "pageNumber", "page_number", "pageNum")
OFFSET_PARAMS = ("os", "offset", "start", "from", "startIndex", "first", "skip", "b")
PATH_SHAPES = ("/page/{n}/", "/page/{n}", "/p/{n}")


def set_param(url: str, key: str, value) -> str:
    """Replace one query parameter, preserving the rest.

    The braces are un-escaped at the end on purpose. urlencode turns "{page}"
    into "%7Bpage%7D", and generic_listing calls `template.format(page=...)` on
    this string — a percent-encoded brace is not a format placeholder, so the
    template would be emitted verbatim into every request and the crawler would
    ask the site for a page literally named "%7Bpage%7D".
    """
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() != key.lower()]
    q.append((key, str(value)))
    built = urlunparse(parts._replace(query=urlencode(q)))
    return built.replace("%7B", "{").replace("%7D", "}")


def with_path_page(url: str, shape: str, n: int) -> str:
    parts = urlparse(url)
    path = (parts.path or "/").rstrip("/")
    # Don't stack /page/2/page/3 when the URL already carries one.
    path = re.sub(r"/(page|p)/\d+/?$", "", path)
    return urlunparse(parts._replace(path=path + shape.format(n=n)))


def signature(items) -> frozenset:
    """What this page actually listed, as a comparable set."""
    return frozenset((i.opportunity_url or i.title or "").strip() for i in items)


def overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Probe:
    def __init__(self, scraper, pages: int, headless: bool, timeout: int):
        self.s = scraper
        self.pages = pages
        self.headless = headless
        self.timeout = timeout
        self.page = None

    # --------------------------------------------------------------- fetching
    def fetch(self, url: str) -> str:
        try:
            self.page.goto(url, timeout=self.timeout * 1000,
                           wait_until="domcontentloaded")
            try:
                self.page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            self.page.wait_for_timeout(1_500)
            return self.page.content()
        except Exception as exc:                                # noqa: BLE001
            log.debug("fetch failed %s: %s", url, exc)
            return ""

    def listings(self, url: str):
        html = self.fetch(url)
        if not html:
            return None
        try:
            return self.s.parse_listing(html, url)
        except Exception:
            log.debug("parse raised on %s", url, exc_info=True)
            return []

    # ------------------------------------------------------------- candidates
    def dom_candidates(self, base: str) -> list[tuple[str, str]]:
        """(label, url) pairs the page itself offers for page 2."""
        script = """() => {
            const out = [];
            const push = (why, href) => { if (href) out.push([why, href]); };
            const rel = document.querySelector('a[rel=next], link[rel=next]');
            if (rel) push('rel=next', rel.getAttribute('href'));
            document.querySelectorAll('a[href]').forEach(a => {
                const t = (a.textContent || '').trim().toLowerCase();
                const cls = ((a.className||'') + ' ' +
                             ((a.parentElement && a.parentElement.className) || '')).toString();
                if (t === '2' && /pag|page/i.test(cls)) push('numbered link "2"', a.getAttribute('href'));
                else if (t === '2') push('bare link "2"', a.getAttribute('href'));
                if (['next','next page','next »','»','›','>','older','older posts',
                     'load more','show more','view more'].includes(t))
                    push('label "' + t + '"', a.getAttribute('href'));
            });
            return out.slice(0, 20);
        }"""
        try:
            raw = self.page.evaluate(script) or []
        except Exception:
            return []
        seen, out = set(), []
        for why, href in raw:
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            url = urljoin(base, href)
            if url in seen or url.rstrip("/") == base.rstrip("/"):
                continue
            seen.add(url)
            out.append((why, url))
        return out

    def guess_candidates(self, base: str, size: int) -> list[tuple[str, str, str]]:
        """(label, url, template) for the parameter shapes worth trying."""
        out = []
        for key in PAGE_PARAMS:
            out.append((f"?{key}=2", set_param(base, key, 2),
                        set_param(base, key, "{page}")))
        for key in OFFSET_PARAMS:
            out.append((f"?{key}={size}", set_param(base, key, size),
                        set_param(base, key, "{offset}")))
        for shape in PATH_SHAPES:
            out.append((shape.format(n=2), with_path_page(base, shape, 2),
                        with_path_page(base, shape, "{page}")))
        return out

    # ------------------------------------------------------------------- main
    def run(self) -> dict:
        from playwright.sync_api import sync_playwright

        from app.scrapers import site_auth

        name = self.s.name
        base = self.s.start_url
        cfg = getattr(self.s, "config", {}) or {}
        result = {"name": name, "display": self.s.display_name, "start_url": base,
                  "existing": cfg.get("page_url", ""), "verdict": "NO CANDIDATE",
                  "template": "", "page_size": 0, "how": "", "detail": ""}

        with sync_playwright() as pw:
            context = site_auth.open_context(pw, name, headless=self.headless)
            try:
                self.page = context.pages[0] if context.pages else context.new_page()

                first = self.listings(base)
                if first is None:
                    result["verdict"] = "NO BASELINE"
                    result["detail"] = "page 1 never loaded"
                    return result
                if not first:
                    result["verdict"] = "NO BASELINE"
                    result["detail"] = "page 1 loaded but parsed to 0 listings"
                    return result
                sig1 = signature(first)
                size = len(first)
                result["page_size"] = size
                print(f"    page 1: {size} listing(s)")

                # The site's own controls first — they are the ground truth, and
                # a URL the page itself offers cannot be an invented parameter.
                for why, url in self.dom_candidates(base):
                    ok, detail = self.check(url, sig1)
                    print(f"    [{'PASS' if ok else 'no  '}] {why:22} {url[:78]}  {detail}")
                    if ok:
                        tpl = self.templatise(url, size)
                        result.update(verdict="FOUND", template=tpl or url,
                                      how=f"the page's own {why}",
                                      detail=detail)
                        if tpl:
                            return result
                        # A usable URL we cannot turn into a template still tells
                        # the crawler's auto-detection everything it needs.
                        result["verdict"] = "SINGLE STEP"
                        return result

                for why, url, tpl in self.guess_candidates(base, size):
                    ok, detail = self.check(url, sig1)
                    print(f"    [{'PASS' if ok else 'no  '}] {why:22} {url[:78]}  {detail}")
                    if ok:
                        result.update(verdict="FOUND", template=tpl,
                                      how=f"parameter {why}", detail=detail)
                        return result

                # Nothing produced new rows. Distinguish "there is only one page"
                # from "we cannot find page 2" — very different problems, and the
                # first needs no fix at all.
                probe2 = self.listings(set_param(base, "page", 2))
                if probe2 and overlap(sig1, signature(probe2)) > 0.8:
                    result["verdict"] = "SINGLE PAGE"
                    result["detail"] = "page 2 returns the same listings as page 1"
                return result
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    def check(self, url: str, sig1: frozenset) -> tuple[bool, str]:
        """Does this URL return listings page 1 did not?"""
        items = self.listings(url)
        if items is None:
            return False, "did not load"
        if not items:
            return False, "0 listings"
        sig = signature(items)
        ov = overlap(sig1, sig)
        if ov > 0.8:
            return False, f"same rows as page 1 ({ov:.0%} overlap)"
        fresh = len(sig - sig1)
        return True, f"{len(items)} listing(s), {fresh} new ({ov:.0%} overlap)"

    def templatise(self, url: str, size: int) -> str:
        """Turn a working page-2 URL into a {page}/{offset} template."""
        parts = urlparse(url)
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in {k.lower() for k in PAGE_PARAMS} and value == "2":
                return set_param(url, key, "{page}")
            if key.lower() in {k.lower() for k in OFFSET_PARAMS} and value == str(size):
                return set_param(url, key, "{offset}")
        if re.search(r"/(page|p)/2/?$", parts.path or ""):
            return re.sub(r"/(page|p)/2(/?)$", r"/\1/{page}\2", url)
        return ""


def write_config(results: list[dict]) -> int:
    """Add the discovered templates to sources.json. Returns how many changed."""
    entries = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    by_name = {e.get("name"): e for e in entries}
    changed = 0
    for r in results:
        if r["verdict"] != "FOUND" or not r["template"]:
            continue
        entry = by_name.get(r["name"])
        if entry is None:
            print(f"  ! {r['display']} is not a sources.json entry "
                  f"(it has its own module) — add the template there by hand")
            continue
        if entry.get("page_url") == r["template"]:
            continue
        entry["page_url"] = r["template"]
        if "{offset}" in r["template"]:
            entry["page_size"] = r["page_size"]
        entry.setdefault("stale_page_streak", 0)
        changed += 1
        print(f"  + {r['display']}: page_url = {r['template']}")
    if changed:
        CONFIG_PATH.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", help="scraper names (or use --all)")
    ap.add_argument("--all", action="store_true", help="every registered source")
    ap.add_argument("--only-unconfigured", action="store_true",
                    help="skip sources that already have a page_url template")
    ap.add_argument("--write", action="store_true",
                    help="write the discovered templates into sources.json")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per page")
    ap.add_argument("--pages", type=int, default=2,
                    help="how far to confirm (2 = just page 2)")
    ap.add_argument("--json", default="", help="write the full result here")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="  %(levelname)-7s %(message)s")

    if args.all:
        names = list(SCRAPER_REGISTRY)
    else:
        names = []
        for want in args.sources:
            w = want.strip().lower()
            hits = [n for n, c in SCRAPER_REGISTRY.items()
                    if w == n.lower() or w in n.lower()
                    or w in c.display_name.lower().replace(" ", "")]
            if not hits:
                print(f"no scraper matches {want!r}", file=sys.stderr)
                return 2
            names.extend(hits)
    if not names:
        ap.print_help()
        return 2

    scrapers = [SCRAPER_REGISTRY[n]() for n in dict.fromkeys(names)]
    if args.only_unconfigured:
        scrapers = [s for s in scrapers
                    if not (getattr(s, "config", {}) or {}).get("page_url")]

    print(f"\nProbing {len(scrapers)} source(s). Nothing is written to the "
          f"database.\n" + "=" * 96)
    results = []
    for s in scrapers:
        print(f"\n{s.display_name}  ({s.name})\n    {s.start_url}")
        if (getattr(s, "config", {}) or {}).get("page_url"):
            print(f"    already configured: {s.config['page_url']}")
        try:
            r = Probe(s, args.pages, not args.headed, args.timeout).run()
        except Exception as exc:                                # noqa: BLE001
            r = {"name": s.name, "display": s.display_name, "verdict": "ERROR",
                 "detail": f"{type(exc).__name__}: {exc}", "template": "",
                 "page_size": 0, "how": "", "start_url": s.start_url,
                 "existing": ""}
        results.append(r)
        print(f"    => {r['verdict']}"
              + (f": {r['template']}" if r.get("template") else "")
              + (f"  ({r['detail']})" if r.get("detail") else ""))

    print("\n" + "=" * 96 + "\nSUMMARY\n" + "=" * 96)
    for r in results:
        print(f"  {r['verdict']:13} {r['display'][:32]:34} {r.get('template','')[:60]}")

    found = [r for r in results if r["verdict"] == "FOUND"]
    if found and not args.write:
        print(f"\n{len(found)} template(s) found. Re-run with --write to put them "
              f"in sources.json, or paste them in by hand.")
    elif found:
        print()
        n = write_config(found)
        print(f"\nUpdated {n} entr{'y' if n == 1 else 'ies'} in {CONFIG_PATH}")
        print("Re-run scripts/source_report.py after the next scrape to confirm "
              "the pages actually go deeper.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
