"""Keep the platform English-only, without machine translation.

Two kinds of non-English row reach the ingest step:

1. A lead the source ALSO publishes in English, scraped in another language
   because of a language selection: a /fr/ path, a ``?lang=es`` parameter, a
   ``fr.`` subdomain, or a page that chose its language from the visitor's
   location. For these the English edition of the SAME page is read and
   stored instead, with its English URL.
2. A lead published only in another language. It is not translated and not
   stored: the team cannot act on a call it cannot read in the original.

Speed rules, because this runs inside the nightly scrape:
  * Language detection is local (stop-word counts, a script check, and
    langdetect only to confirm). No network for English rows, which are
    nearly all rows.
  * Before any network call, the database is asked whether the English
    edition of this page is already stored. If it is, that row is reused.
  * At most two short HTTP requests per non-English row, results cached in
    memory (negative ones too), and a per-run cap.
  * Closed (expired) non-English rows are never looked up.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

log = logging.getLogger("scraper")

# ----------------------------------------------------------- detection

_NON_LATIN = re.compile(
    r"[֐-׿؀-ۿݐ-ݿЀ-ӿऀ-ॿ"
    r"ঀ-৿฀-๿一-鿿぀-ヿ가-힯]")

_EN = frozenset("""
the and of to for with is are be by this that will from or at it which their
has have was were can its these our we you your all not been under through
who what how into within should must support applications apply proposals
deadline call grant grants funding projects project""".split())

_FOREIGN = {
    "fr": frozenset("le la les des du et pour une dans est sur au aux avec par "
                    "appel projets subvention candidature date limite".split()),
    "es": frozenset("el los las del y para con una por que convocatoria "
                    "proyectos subvenciones fecha".split()),
    "pt": frozenset("os do da dos das para com uma em não edital chamada "
                    "projetos inscrições".split()),
    "de": frozenset("der die das und für mit von zu ein eine im auf ist "
                    "ausschreibung förderung".split()),
    "it": frozenset("il gli della delle per con una di che bando progetti".split()),
    "nl": frozenset("het een en van voor met subsidie oproep".split()),
}
_ACCENTED = re.compile(r"[àâçéèêëîïôûùüÿñãõáíóúäöß]", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

try:                                   # optional confirmation step
    from langdetect import DetectorFactory, detect_langs
    DetectorFactory.seed = 0
except Exception:                      # pragma: no cover - dependency missing
    detect_langs = None


def _confirm(text: str) -> tuple[str, float]:
    if detect_langs is None:
        return "", 0.0
    try:
        best = detect_langs(text)[0]
        return best.lang, float(best.prob)
    except Exception:
        return "", 0.0


def detect(title: str, summary: str = "", eligibility: str = "") -> tuple[bool, str]:
    """(is_english, language_code). Unsure means English: a row is never
    dropped on a guess. Short English titles such as "2027 Prince Albert
    Fund" have no stop words at all and stay English."""
    text = " ".join(filter(None, [title or "", (summary or "")[:600],
                                  (eligibility or "")[:200]]))
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 8:
        foreign_script = sum(1 for c in letters if _NON_LATIN.match(c))
        if foreign_script / len(letters) >= 0.20:
            lang, _ = _confirm(text)
            return False, lang or "non-latin"
    words = [w.casefold() for w in _WORD.findall(text)]
    if not words:
        return True, "en"
    en_hits = sum(w in _EN for w in words)
    hits = {lang: sum(w in vocab for w in words) for lang, vocab in _FOREIGN.items()}
    lang, foreign_hits = max(hits.items(), key=lambda kv: kv[1])
    candidate = (
        (foreign_hits >= 2 and foreign_hits > en_hits * 1.5)
        or (foreign_hits >= 1 and en_hits == 0 and _ACCENTED.search(text))
    )
    if not candidate:
        return True, "en"
    detected, prob = _confirm(text)
    if detect_langs is None:
        return False, lang                     # stop words alone
    if detected and detected != "en" and prob >= 0.80:
        return False, detected
    return True, "en"


# --------------------------------------------------- English edition URLs

_LOCALES = r"(?:fr|es|pt|de|it|nl|ar|ru|zh|ja|ko|id|tr|pl|uk|vi|th|hi|bn)"
_PATH_LOCALE = re.compile(rf"^/({_LOCALES}(?:[-_][a-z]{{2}})?)(?=/|$)", re.IGNORECASE)
_LANG_PARAMS = {"lang", "language", "locale", "hl", "lng", "lan", "idioma", "langue"}
_SUBDOMAIN = re.compile(rf"^{_LOCALES}\.", re.IGNORECASE)


def english_candidates(url: str) -> list[str]:
    """English editions of ``url`` implied by its own language selection."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return []
    if p.scheme not in ("http", "https") or not p.netloc:
        return []
    out: list[str] = []
    m = _PATH_LOCALE.match(p.path)
    if m:
        out.append(urlunsplit(p._replace(path="/en" + p.path[m.end():])))
    query = parse_qsl(p.query, keep_blank_values=True)
    if any(k.lower() in _LANG_PARAMS for k, _ in query):
        q = urlencode([(k, "en" if k.lower() in _LANG_PARAMS else v) for k, v in query])
        out.append(urlunsplit(p._replace(query=q)))
    if _SUBDOMAIN.match(p.netloc):
        out.append(urlunsplit(p._replace(netloc="www." + p.netloc.split(".", 1)[1])))
        out.append(urlunsplit(p._replace(netloc="en." + p.netloc.split(".", 1)[1])))
    return [u for u in dict.fromkeys(out) if u != url]


_HREFLANG = re.compile(
    r"<link\b[^>]*\bhreflang\s*=\s*[\"']en(?:-[a-z]{2})?[\"'][^>]*>", re.IGNORECASE)
_HREF = re.compile(r"\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_META = r"<meta\b[^>]*\b(?:property|name)\s*=\s*[\"']{}[\"'][^>]*\bcontent\s*=\s*[\"']([^\"']*)[\"']"
_OG_TITLE = re.compile(_META.format("og:title"), re.IGNORECASE)
_OG_DESC = re.compile(_META.format("(?:og:description|description)"), re.IGNORECASE)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    import html as _html
    return " ".join(_html.unescape(_TAGS.sub(" ", fragment or "")).split())


def hreflang_english(page_html: str, base_url: str) -> str:
    for tag in _HREFLANG.findall(page_html or ""):
        href = _HREF.search(tag)
        if href:
            return urljoin(base_url, href.group(1))
    return ""


def read_heading(page_html: str) -> tuple[str, str]:
    """(title, description) of a detail page."""
    title = ""
    for rx in (_OG_TITLE, _H1, _TITLE):
        m = rx.search(page_html or "")
        if m and _clean(m.group(1)):
            title = _clean(m.group(1))
            break
    desc = _OG_DESC.search(page_html or "")
    return title[:500], (_clean(desc.group(1)) if desc else "")[:1000]


# --------------------------------------------------------- the decision

@dataclass
class Verdict:
    keep: bool
    raw: object = None          # RawOpportunity, possibly the English edition
    reason: str = ""            # rejection reason when keep is False
    switched: bool = False      # True when the English edition replaced it
    lang: str = "en"


_cache: dict[str, tuple[float, tuple[str, str, str] | None]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 7 * 24 * 3600
_lookups_this_run = 0


def reset_run_budget() -> None:
    global _lookups_this_run
    _lookups_this_run = 0


def _stored_english(urls: list[str], source_website: str):
    """An English edition already in the database: no network needed."""
    if not urls:
        return None
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity
    with session_scope() as db:
        row = db.execute(
            select(Opportunity.title, Opportunity.summary, Opportunity.opportunity_url)
            .where(Opportunity.opportunity_url.in_(urls))
            .where(Opportunity.source_website == source_website)
            .limit(1)
        ).one_or_none()
    return tuple(row) if row else None


def _fetch_english(url: str, timeout: float) -> tuple[str, str, str] | None:
    """Read the English edition of ``url``: its hreflang alternate first,
    then the URL with its language selection switched to English."""
    import httpx

    from app.core.config import settings
    headers = {"User-Agent": settings.user_agent,
               "Accept-Language": "en-US,en;q=0.9"}
    tried: list[str] = []
    try:
        with httpx.Client(headers=headers, timeout=timeout,
                          follow_redirects=True) as client:
            candidates = english_candidates(url)
            if not candidates:
                # No language marker in the URL: the page may declare its
                # English edition itself.
                r = client.get(url)
                tried.append(url)
                alt = hreflang_english(r.text, str(r.url)) if r.status_code == 200 else ""
                if alt and alt != url:
                    candidates = [alt]
            for cand in candidates[:1]:
                r = client.get(cand)
                tried.append(cand)
                if r.status_code != 200:
                    continue
                title, desc = read_heading(r.text)
                if title and detect(title, desc)[0]:
                    return title, desc, str(r.url)
    except Exception as exc:                 # network trouble is "not found"
        log.debug("English edition lookup failed for %s: %s", url, exc)
    return None


def ensure_english(raw, *, expired: bool = False) -> Verdict:
    """Decide what to store for one scraped row. Never raises."""
    from app.core.config import settings
    if not settings.english_only:
        return Verdict(True, raw)
    try:
        is_en, lang = detect(raw.title, raw.summary, raw.eligibility)
    except Exception:
        return Verdict(True, raw)
    if is_en:
        return Verdict(True, raw)
    if expired:
        return Verdict(False, raw, f"not English ({lang}), closed", lang=lang)

    url = (raw.opportunity_url or "").strip()
    found = None
    try:
        found = _stored_english(english_candidates(url), raw.source_website)
    except Exception as exc:
        log.debug("stored English lookup failed: %s", exc)

    if found is None and url:
        global _lookups_this_run
        now = time.time()
        with _cache_lock:
            hit = _cache.get(url)
        if hit and now - hit[0] < _CACHE_TTL:
            found = hit[1]
        elif _lookups_this_run < settings.english_lookup_max_per_run:
            _lookups_this_run += 1
            found = _fetch_english(url, settings.english_lookup_timeout_s)
            with _cache_lock:
                if len(_cache) > 5000:
                    _cache.clear()
                _cache[url] = (now, found)

    if not found:
        return Verdict(False, raw, f"not English ({lang}), no English version", lang=lang)
    title, desc, en_url = found
    update = {"title": title, "opportunity_url": en_url}
    if not detect(raw.summary or "")[0]:
        update["summary"] = desc            # the English page's own description
    if raw.eligibility and not detect(raw.eligibility)[0]:
        update["eligibility"] = ""
    return Verdict(True, raw.model_copy(update=update), switched=True, lang=lang)
