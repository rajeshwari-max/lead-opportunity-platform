"""Best-effort organisation extraction from listing text.

Most sources publish the funder as its own field and the scrapers read it
directly (DevelopmentAid, Bond UK, DevNetJobsIndia and NGOBOX are all at 100%
coverage). Two are not:

  * FundsForNGOs (~46% of the database) exposes no funder field at all — the
    organisation is only ever named inside the summary prose.
  * GrantWatch deliberately anonymises the funder on free listings ("research
    using the funding source's resources"), so it genuinely cannot be
    recovered without a paid account.

This module recovers the name from prose for the first case. It is tuned for
precision over coverage: a wrong organisation is worse than a blank one, so a
candidate is only accepted when it carries a real institutional signal (an
acronym, or a word like Foundation / Council / Ministry / Commission).
Programme names are explicitly rejected — "Micro Grants Program" is a scheme,
not an organisation, and filling the column with those would be misleading.
"""
from __future__ import annotations

import re

# Leading noise on FundsForNGOs summaries: "Deadline: 31-Dec-2026  AI Snippet Summary: ..."
_PREFIX = re.compile(
    r"^(?:\s*deadline\s*:\s*\S+)?\s*(?:ai\s+snippet\s+summary\s*:?\s*)?", re.IGNORECASE
)

# Words that mark a real institution. Deliberately EXCLUDES a bare
# "program/programme": including it let scheme names ("Micro Grants Program",
# "Local Artists Grant Program") through as organisations. UN-style bodies are
# caught by their acronym or by an explicit multi-word pattern instead.
_ORG_MARKER = re.compile(
    r"\b(foundation|trust|council|commission|ministry|minist[eè]re|agency|association|"
    r"institute|institution|university|college|society|bank|department|authority|"
    r"corporation|cent(?:er|re)|network|alliance|federation|consortium|"
    r"organi[sz]ation|office|secretariat|bureau|charity|charitable|endowment|"
    r"philanthrop|embassy|government|municipal|united\s+nations|"
    r"european\s+(?:commission|union)|world\s+(?:bank|food|health)|"
    r"development\s+programme|environment\s+programme|"
    r"fonds|fonden|stiftung|fundaci[oó]n)\b",
    re.IGNORECASE,
)
_MARKER_WORD = re.compile(_ORG_MARKER.pattern, re.IGNORECASE)

# Trailing scheme words — stripped before judging whether a name is institutional.
_TAIL = re.compile(
    r"(?:\s*\((?:round|cycle|phase)\s*[\w\s]*\))?"
    r"(?:\s+(?:grants?|programmes?|programs?|funds?|funding|schemes?|fellowships?|"
    r"scholarships?|awards?|prizes?|competitions?|challenges?|accelerators?|"
    r"residenc(?:y|ies)|subsid(?:y|ies)|bursar(?:y|ies)|calls?|rounds?|cycles?))+$",
    re.IGNORECASE,
)
_ACRONYM = re.compile(r"\(([A-Z]{2,8})\)|^[A-Z]{2,8}$")
_PAREN_TAIL = re.compile(r"^(.*?\([A-Z]{2,8}\))\s+\S.*$")   # drop text after "(ABC)"
# A relative clause means we over-captured: "Minderoo Foundation, that provides ..."
_CLAUSE = re.compile(r",\s+(?:ha[sv]e?|is|are|will|which|who|that|and\s+its)\b.*$", re.IGNORECASE)

_CONNECTORS = {"for", "of", "on", "in", "de", "du", "des", "della", "del", "the", "aux"}
# Too vague to be useful as an organisation on their own.
_GENERIC = {
    "civil society", "society", "government", "the government", "european union",
    "united nations", "embassy", "council", "foundation", "trust", "network",
    "commission", "ministry", "agency", "university", "institute", "bank",
}
_STOP_FIRST = {
    "the", "a", "an", "this", "it", "they", "we", "applications", "apply", "grants",
    "grant", "funding", "fund", "program", "programme", "opportunity", "deadline",
    "eligible", "interested", "organizations", "proposals", "successful", "selected",
    "all", "each", "ai", "snippet", "summary",
}

# Appositive clauses split a name from its verb ("The X Foundation, that
# provides grants, is inviting ...") — drop them before matching.
_APPOSITIVE = re.compile(r",\s+(?:that|which|who)\s+[^,]{0,80},\s*", re.IGNORECASE)

# "<Org> is inviting / invites / announces / provides ...". Lowercase connectors
# are allowed mid-name so "International Centre for Genetic Engineering and
# Biotechnology" is captured whole rather than clipped at the first connector.
_ACTOR = re.compile(
    r"(?:^|(?<=[.•]\s))\s*(?:The\s+)?"
    r"([A-Z][\w&.,'’()\-]*(?:\s+(?:of|for|and|the|de|du|des|on|in)|\s+[A-Z(][\w&.,'’()\-]*){0,12})\s+"
    r"(?:is\s+(?:inviting|accepting|seeking|requesting|calling|offering|pleased|now\s+accepting)"
    r"|invites|announces|announced|has\s+(?:launched|opened|announced)|seeks|offers|provides)\b"
)
# "... funded / administered / offered by <Org>"
_BY = re.compile(
    r"\b(?:offered|funded|administered|launched|managed|supported|implemented|run|provided|"
    r"established|created|sponsored)\s+by\s+(?:the\s+)?"
    r"([A-Z][\w&.,'’()\-]*(?:\s+[A-Z(a-z][\w&.,'’()\-]*){0,8})"
)
# "The <Scheme> by <Org> provides ..."
_PROGRAM_BY = re.compile(
    r"\bby\s+(?:the\s+)?([A-Z][\w&.,'’()\-]*(?:\s+[A-Z(][\w&.,'’()\-]*){0,6})\s+"
    r"(?:provides|offers|supports|aims|seeks|invites)\b"
)
# Fallback: the organisation is named in the title itself.
_TITLE_ORG = re.compile(
    r"([A-Z][\w&.'’\-]*(?:\s+[A-Z][\w&.'’\-]*){0,5}\s+"
    r"(?:Foundation|Trust|Council|Commission|Ministry|Agency|Association|Institute|"
    r"University|Society|Bank|Department|Authority|Network|Embassy))"
)


def _clean(cand: str) -> str:
    c = re.sub(r"\s+", " ", cand or "").strip(" ,.;:-–—")
    c = re.sub(r"^(The|A|An)\s+", "", c, flags=re.IGNORECASE)
    # Detail pages run headings into body text ("Grant The Rainbow Foundation
    # is inviting..."), so drop leading noise words instead of rejecting the
    # whole candidate because of them.
    words = c.split()
    while words and words[0].lower().strip(",.") in _STOP_FIRST:
        words.pop(0)
    c = re.sub(r"^(The|A|An)\s+", "", " ".join(words), flags=re.IGNORECASE)
    if not (3 <= len(c) <= 120):
        return ""
    words = c.split()
    if not words:
        return ""
    # mostly-lowercase text is prose, not a name
    if sum(1 for w in words if w[:1].isupper()) < max(1, len(words) // 2):
        return ""
    return c


def _cut_after_marker(cand: str) -> str:
    """Trim descriptive text that follows the institution word.

    "High Fives Foundation Empowerment" -> "High Fives Foundation".
    The name is kept whole when the marker is followed by a connector or a
    parenthetical acronym, so "International Centre for Genetic Engineering"
    and "Earth Journalism Network (EJN)" survive intact.
    """
    toks = cand.split()
    for i, tok in enumerate(toks):
        if not _MARKER_WORD.fullmatch(tok.strip("’'s,.()")):
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        low = nxt.lower().strip(",.")
        after = toks[i + 2] if i + 2 < len(toks) else ""
        if not nxt:
            return " ".join(toks).strip(" ,.;:-")
        if nxt.startswith("("):
            continue                                   # "Network (EJN)"
        if _MARKER_WORD.fullmatch(low):
            continue                                   # "Charitable Trust" — keep going
        # A connector continues the name only when a capitalised word follows
        # ("Centre for Genetic"), not prose ("Trust for community work").
        if low in _CONNECTORS or low == "and":
            if after[:1].isupper():
                continue
        return " ".join(toks[: i + 1]).strip(" ,.;:-")
    return cand


def _finalize(cand: str, trusted: bool = False) -> str:
    """`trusted` = the phrasing itself named the funder ("... funded by <X>"),
    so the institutional-marker requirement is waived; those constructions are
    explicit enough that names like "Pro Helvetia" shouldn't be discarded."""
    c = _CLAUSE.sub("", cand).strip(" ,.;:-–—")
    if _ACRONYM.search(c):
        c = _PAREN_TAIL.sub(r"\1", c)      # "X (ABC) in collaboration with..." -> "X (ABC)"
        c = _cut_after_marker(c) if _ORG_MARKER.search(c) else c
        return c if len(c) >= 4 and not _is_generic(c) else ""
    trimmed = _TAIL.sub("", c).strip(" ,.;:-–—") or c
    if not _ORG_MARKER.search(trimmed):
        if not trusted:
            return ""                      # no institutional signal — don't guess
        out = trimmed
    else:
        out = _cut_after_marker(trimmed)
    if len(out) < 4 or _is_generic(out):
        return ""
    return out


def _is_generic(value: str) -> bool:
    low = value.lower().strip()
    return low in _GENERIC or any(low.endswith(g) for g in ("civil society", "the government"))


def extract_organization(summary: str, title: str = "") -> str:
    """Return an organisation name, or '' when none can be identified confidently."""
    text = _PREFIX.sub("", re.sub(r"\s+", " ", summary or "").strip())
    text = _APPOSITIVE.sub(" ", text)
    if text:
        # "by <X>" phrasings name the funder outright, so they're trusted;
        # the "<X> is inviting" phrasing often catches a scheme name instead
        # and still has to prove it's an institution.
        for rx, trusted in ((_PROGRAM_BY, True), (_BY, True), (_ACTOR, False)):
            m = rx.search(text[:600])
            if m:
                cleaned = _clean(m.group(1))
                if cleaned:
                    final = _finalize(cleaned, trusted=trusted)
                    if final:
                        return final
    m = _TITLE_ORG.search(title or "")
    if m:
        out = _cut_after_marker(m.group(1).strip())
        if 4 <= len(out) <= 120 and not _is_generic(out):
            return out
    return ""


def tidy_organization(value: str, max_len: int = 200) -> str:
    """Normalise an organisation the source *did* provide.

    DevelopmentAid joins every co-funder into one string, which previously got
    chopped mid-word at the column limit ("...Ministry for Foreign Affai"). Cut
    on a separator instead and say how many were omitted, so the value stays
    readable and honest.
    """
    v = re.sub(r"\s+", " ", value or "").strip(" ,;")
    if len(v) <= max_len:
        return v
    parts = [p.strip() for p in v.split(",") if p.strip()]
    if len(parts) > 1:
        kept: list[str] = []
        for p in parts:
            if len(", ".join(kept + [p])) > max_len - 15:
                break
            kept.append(p)
        if kept:
            hidden = len(parts) - len(kept)
            return ", ".join(kept) + (f" +{hidden} more" if hidden > 0 else "")
    return v[: max_len - 1].rsplit(" ", 1)[0] + "…"


def backfill_organizations() -> int:
    """Fill blank organisations on existing rows. Safe to run repeatedly."""
    import logging

    from sqlalchemy import or_, select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    updated = 0
    with session_scope() as db:
        rows = db.execute(
            select(Opportunity).where(
                or_(Opportunity.organization == "", Opportunity.organization.is_(None))
            )
        ).scalars().all()
        for opp in rows:
            found = extract_organization(opp.summary or "", opp.title or "")
            if found:
                opp.organization = found
                updated += 1

        # An all-digits organisation is a database identifier that leaked out of
        # a source (DevelopmentAid's `donorIds` did exactly this, filling the
        # column with values like "118391"). Clear them so the next scrape can
        # store the real name instead of leaving a number on screen.
        for opp in db.execute(select(Opportunity)).scalars().all():
            value = (opp.organization or "").strip()
            if value and value.replace(",", "").replace(" ", "").isdigit():
                opp.organization = ""
                updated += 1
    if updated:
        log.info("Organization backfill: filled %s opportunities", updated)
    return updated
