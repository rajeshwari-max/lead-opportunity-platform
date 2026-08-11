"""Reject listings that are advertisements rather than opportunities.

Public tender boards accept submissions, and some of what gets submitted is
spam. DevelopmentAid's board carried 40 pharmaceutical adverts — Arabic and
English listings for abortion pills with a contact number — which the scraper
dutifully stored as Tenders with deadlines in 2028.

The strongest general signal is a **phone number in the title**. A genuine call
for proposals is identified by its subject and reference number; contact details
live in the body or the linked document, never the headline. That rule catches
this spammer and the next one without needing to know anything about either.

The keyword list is a second, narrower net for adverts that omit the number.
It is deliberately specific: broad pharmaceutical terms would reject real
health-sector procurement, which is a category the team actively wants.
"""
from __future__ import annotations

import re

#
# Requires a literal "+" and at least nine digits. An earlier version also
# accepted a bare "00" prefix, which matched inside real reference numbers —
# "PL2002/000-580.06.02" and "53382-002 - 53382-BAN" were both flagged as spam.
# Reference codes never carry a leading +, so demanding one removes that whole
# class of false positive.
_PHONE_IN_TITLE = re.compile(r"\+\d[\d\s\-().]{6,}\d")
_MIN_PHONE_DIGITS = 9


def _has_phone(text: str) -> bool:
    for match in _PHONE_IN_TITLE.finditer(text):
        if sum(ch.isdigit() for ch in match.group()) >= _MIN_PHONE_DIGITS:
            return True
    # A long digit run counts only next to explicit contact wording, so an
    # 11-digit grant reference stays clear of this.
    return bool(
        re.search(r"\b\d{9,}\b", text)
        and re.search(r"whatsapp|واتساب|اتصل|call now|contact us", text, re.IGNORECASE)
    )

# Adverts that skip the phone number. Kept narrow on purpose — "misoprostol"
# alone would also match a legitimate essential-medicines tender, so each term
# here is paired with advertising language.
_SPAM_TERMS = re.compile(
    r"\b(?:cytotec|misoprostol|mifepristone|mifepristol)\b.{0,60}"
    r"\b(?:buy|sale|for sale|pills?|order|price|delivery|whatsapp)\b"
    r"|\b(?:buy|order)\b.{0,40}\b(?:abortion|cytotec|misoprostol)\b"
    r"|حبوب\s+الإجهاض"                       # "abortion pills"
    r"|\b(?:penis|viagra|cialis|casino|escort|forex signals)\b",
    re.IGNORECASE | re.DOTALL,
)


# Scripts that none of the configured sources publish in. Every one of the 86
# sources is an English-language site, so a title written in Arabic, Cyrillic,
# Chinese, Hebrew, Thai or Devanagari did not come from their editorial process
# — it was submitted to an open board. Accented Latin is untouched, so French,
# Spanish and Portuguese titles are unaffected.
_NON_LATIN = re.compile(
    r"[֐-׿"      # Hebrew
    r"؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿"   # Arabic
    r"Ѐ-ӿ"       # Cyrillic
    r"ऀ-ॿ"       # Devanagari
    r"฀-๿"       # Thai
    r"一-鿿"       # CJK
    r"぀-ヿ"       # Kana
    r"가-힯]"      # Hangul
)
# A share, not a single character: a legitimate English title may quote one
# foreign word or a place name in its own script.
_NON_LATIN_SHARE = 0.20


def _mostly_non_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    foreign = sum(1 for c in letters if _NON_LATIN.match(c))
    return foreign / len(letters) >= _NON_LATIN_SHARE


# Page furniture that the generic link scraper mistakes for a listing. These are
# whole-title matches, not substrings: "Skip to main content" is never an
# opportunity, but "Procurement Policy Review Consultant" plainly is.
_FURNITURE = {
    "skip to main content", "skip to content", "procurement policy", "cookie policy",
    "privacy policy", "privacy notice", "terms of use", "terms and conditions",
    "sign in", "sign up", "register", "log in", "login", "contact us", "contact",
    "subscribe", "newsletter", "feedback survey", "news and events", "site map",
    "sitemap", "accessibility", "back to top", "read more", "learn more",
    "apply now", "view all", "see all", "download", "share this page",
    "frequently asked questions", "about us", "our work", "what we do",
}


def is_furniture(title: str) -> bool:
    t = re.sub(r"\s+", " ", (title or "")).strip().strip(".:–-").lower()
    return t in _FURNITURE


def is_spam(title: str, summary: str = "") -> bool:
    """True when a listing looks like an advertisement, not an opportunity."""
    t = (title or "").strip()
    if not t:
        return False
    # Non-Latin titles are rejected at the door now, by explicit instruction:
    # the team works in English and cannot act on a call it cannot read. This
    # does discard some genuine listings — UNDP grant competitions published
    # only in Russian, a Tunisian call for local associations — so it is a
    # deliberate trade of recall for a clean working list, not a spam judgement.
    if _has_phone(t) or is_furniture(t) or _mostly_non_latin(t):
        return True
    return bool(_SPAM_TERMS.search(t) or _SPAM_TERMS.search((summary or "")[:500]))


def find_spam() -> list[tuple[int, str, str]]:
    """Existing rows that would be rejected today. Reported, not deleted —
    removing rows is the operator's call, not a side effect of a scan."""
    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    out: list[tuple[int, str, str]] = []
    with session_scope() as db:
        for opp in db.execute(select(Opportunity)).scalars():
            if is_spam(opp.title or "", opp.summary or ""):
                out.append((opp.id, opp.source_website, (opp.title or "")[:80]))
    return out


def purge_spam() -> int:
    """Delete the rows find_spam() reports. Returns how many went."""
    import logging

    from sqlalchemy import delete, select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    ids = [row[0] for row in find_spam()]
    if not ids:
        return 0
    with session_scope() as db:
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            db.execute(delete(Opportunity).where(Opportunity.id.in_(chunk)))
    log.info("Removed %s spam listings", len(ids))
    return len(ids)
