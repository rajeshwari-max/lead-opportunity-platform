"""Alternative ways to read a funder page when its configured parser finds nothing.

Why this exists
---------------
The November 2026 source-health report listed 35 sources whose page was
fetched but "the parser produced no candidates". Websites change their layout,
move their calls into a feed, put the apply link on another domain, or start
answering automated visitors with a bot check. Any of those turns a working
source into a silent zero until a person notices.

This module does NOT rewrite code or configuration. When the normal parse of a
source's first page yields nothing, the scraper tries these independent
strategies in order and keeps the first that produces funding opportunities:

  1. other_fetch_mode — read the page the other way (plain HTTP if the browser
     got a bot check / blank page, the browser if plain HTTP got a JavaScript
     shell), then run the normal parser on it;
  2. structured_data — schema.org JSON-LD the site embeds for search engines
     (Event, Grant, Article, ItemList …), which survives visual redesigns;
  3. offsite_links   — funding-titled links whose application lives on another
     domain (an application portal, a partner's site), which the normal parser
     drops on purpose;
  4. feed            — the site's RSS/Atom feed (WordPress publishes one by
     default), filtered to funding posts from the last 18 months.

Every row still goes through the same funding-signal and furniture tests as
the normal parser, and then the platform's usual classification and
opportunity gate. The strategy that worked is recorded on the run, the run's
message says "recovered by fallback", and the monthly report lists those
sources so the configuration can be updated deliberately by a person.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# --------------------------------------------------------------- page walls

_CHALLENGE = (
    "just a moment", "attention required", "checking your browser",
    "cf-chl", "challenge-platform", "turnstile", "cf_chl_opt",
    "sorry, you have been blocked", "enable javascript and cookies",
    "verify you are human", "confirm you are human",
    "performing security verification", "access denied", "request blocked",
    "incapsula", "ddos protection by", "are you a robot",
)
_JS_SHELL = re.compile(
    r"(you need to enable javascript|please enable javascript|"
    r"this (site|app|page) requires javascript|javascript is (disabled|required))",
    re.IGNORECASE)


def page_wall(html: str) -> str:
    """'challenge' | 'js_shell' | '' — what kind of non-content page this is."""
    if not html:
        return "js_shell"
    head = html[:60_000].lower()
    if any(marker in head for marker in _CHALLENGE):
        return "challenge"
    soup = BeautifulSoup(html, "lxml")
    scripts = len(soup.find_all("script"))
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if _JS_SHELL.search(html[:60_000]) and len(text) < 2_000:
        return "js_shell"
    if len(text) < 400 and scripts >= 3:
        return "js_shell"
    return ""


# ------------------------------------------------------------ JSON-LD (2)

_LD_TYPES = {
    "event", "grant", "monetarygrant", "creativework", "article", "newsarticle",
    "blogposting", "webpage", "offer", "jobposting", "educationalopportunity",
}


def structured_data(html: str, page_url: str) -> list[dict]:
    """[{title, url, text, deadline}] from schema.org JSON-LD blocks."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []

    def visit(node) -> None:
        if isinstance(node, list):
            for value in node:
                visit(value)
            return
        if not isinstance(node, dict):
            return
        if "@graph" in node:
            visit(node["@graph"])
        if "itemListElement" in node:
            visit(node["itemListElement"])
        if "item" in node and isinstance(node["item"], dict):
            visit(node["item"])
        kind = node.get("@type")
        kinds = {str(k).casefold() for k in (kind if isinstance(kind, list) else [kind])}
        title = node.get("name") or node.get("headline") or ""
        url = node.get("url") or node.get("@id") or ""
        if kinds & _LD_TYPES and isinstance(title, str) and isinstance(url, str) \
                and title.strip() and url.strip():
            out.append({
                "title": title.strip(),
                "url": urljoin(page_url, url.strip()),
                "text": str(node.get("description") or "")[:1000],
                "deadline": str(node.get("endDate") or node.get("validThrough")
                                or node.get("expires") or "")[:64],
            })

    for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            visit(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------- off-site (3)

_SOCIAL = re.compile(
    r"(facebook|twitter|x\.com|linkedin|instagram|youtube|youtu\.be|whatsapp|"
    r"t\.me|pinterest|addtoany|sharethis|google\.com/maps|flickr|vimeo|tiktok|"
    r"mailchimp|eepurl|apple\.com|play\.google)", re.IGNORECASE)


def offsite_links(html: str, page_url: str, website: str) -> list[dict]:
    """Funding-titled links that point to another domain."""
    from app.scrapers.generic_listing import (
        _FUNDING_WORDS, _looks_like_opportunity, clean_title, strip_field_label,
    )

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    home = urlparse(website).netloc.replace("www.", "")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")) or _SOCIAL.search(href):
            continue
        if home and home in urlparse(href).netloc:
            continue                    # on-site: the normal parser's job
        title = strip_field_label(clean_title(a.get_text(" ", strip=True)))
        if not _looks_like_opportunity(title, href) or not _FUNDING_WORDS.search(title):
            continue
        if href in seen:
            continue
        seen.add(href)
        block = a.find_parent(["article", "li", "div", "tr"]) or a
        out.append({"title": title, "url": href,
                    "text": block.get_text(" ", strip=True)[:1000], "deadline": ""})
    return out


# ------------------------------------------------------------- feeds (4)

def feed_urls(html: str, page_url: str, website: str) -> list[str]:
    """Advertised RSS/Atom feeds, plus WordPress's default feed."""
    soup = BeautifulSoup(html, "lxml")
    urls = [urljoin(page_url, link.get("href"))
            for link in soup.find_all("link", href=True)
            if re.search(r"(rss|atom)\+xml", link.get("type") or "", re.I)]
    if "wp-content" in html or "wp-json" in html:
        base = website.rstrip("/") or f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
        urls.append(base + "/feed/")
    # Comment feeds are never opportunities.
    return list(dict.fromkeys(u for u in urls if "comments" not in u.lower()))[:3]


def feed_items(xml: str, max_age_days: int = 548,
               now: datetime | None = None) -> list[dict]:
    """[{title, url, text, deadline}] from an RSS or Atom document."""
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(xml, "xml")
    out = []
    for entry in soup.find_all(["item", "entry"]):
        title = (entry.find("title").get_text(" ", strip=True)
                 if entry.find("title") else "")
        link_tag = entry.find("link")
        url = ""
        if link_tag is not None:
            url = (link_tag.get("href") or link_tag.get_text(strip=True) or "").strip()
        date_tag = entry.find(["pubDate", "published", "updated"])
        if date_tag is not None:
            raw = date_tag.get_text(strip=True)
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                try:
                    when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < now - timedelta(days=max_age_days):
                    continue            # an old post, not a current call
        desc_tag = entry.find(["description", "summary", "content"])
        text = BeautifulSoup(desc_tag.get_text(" ", strip=True), "lxml").get_text(
            " ", strip=True) if desc_tag is not None else ""
        if title and url:
            out.append({"title": title, "url": url, "text": text[:1000], "deadline": ""})
    return out
