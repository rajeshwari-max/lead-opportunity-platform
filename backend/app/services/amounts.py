"""Funding-amount normalisation and extraction.

The Amount column was blank on 99.4% of rows for two separate reasons:

  * sources that publish an amount weren't being read — DevelopmentAid cards
    carry a "Budget" label (EUR 130,000, USD 1,800,000) that the scraper
    ignored entirely;
  * sources that don't publish it as a field still state it in prose
    ("provides up to US$250,000", "grants of €2,000 to €10,000"), and that
    text was discarded.

The handful of values that did get stored were unusable — NGOBOX amounts had
page furniture glued on ("100000 INR Add to Google Calendar Deadline: 31 Jul.
2026 Sha…") and Bond UK stored the literal string "other".

`clean_amount` fixes what a source gives us; `extract_amount` recovers one from
free text. Both prefer returning nothing over returning something wrong.
"""
from __future__ import annotations

import re

# Currency written as a symbol or an ISO-ish code.
_CUR = r"(?:US\$|A\$|C\$|NZ\$|R\$|USD|EUR|GBP|INR|CHF|AUD|CAD|NZD|ZAR|SEK|NOK|DKK|JPY|CNY|KES|NGN|PHP|SGD|THB|IDR|BRL|MXN|Rs\.?|€|£|\$|₹|¥)"
_NUM = r"\d[\d,.\s]*(?:\.\d+)?\s*(?:k\b|m\b|bn\b|million|billion|thousand|lakh|crore)?"
_AMOUNT = rf"{_CUR}\s?{_NUM}|{_NUM}\s?{_CUR}"

# Values that carry no information — treat as absent.
_JUNK_VALUES = {
    "", "n/a", "na", "none", "not applicable", "not specified", "unspecified",
    "other", "others", "tbd", "tba", "varies", "various", "unknown", "-", "--",
    "not disclosed", "confidential", "0",
}
# Page furniture that gets glued onto scraped values.
_FURNITURE = re.compile(
    r"\s*(?:add to (?:google )?calendar|deadline\s*:.*|share\b.*|read more.*|"
    r"apply now.*|view details.*|click here.*)$",
    re.IGNORECASE,
)

# Ordered by how informative the phrasing is — a stated range or ceiling beats
# a bare figure that might be anything (a fee, a past total, a page number).
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"\b(?:ranging\s+)?from\s+({_AMOUNT})\s*(?:to|–|—|-|and)\s*({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"\bbetween\s+({_AMOUNT})\s*(?:to|and|–|—|-)\s*({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"({_AMOUNT})\s*(?:to|–|—)\s*({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"\bup\s+to\s+({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"\b(?:maximum|max\.?|as much as|worth)\s+(?:of\s+)?({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"\b(?:grants?|awards?|funding|budget|amount|support|prize)\s+"
               rf"(?:of|is|:)\s+({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"\bprovides?\s+({_AMOUNT})", re.IGNORECASE),
    re.compile(rf"({_AMOUNT})", re.IGNORECASE),   # last resort: any figure
]


def _tidy_number(value: str) -> str:
    v = re.sub(r"\s+", " ", value or "").strip(" ,.;:-–—")
    # "£ 5000" -> "£5000" for symbols, but letter codes keep the space so it
    # reads as "EUR 130,000" rather than "EUR130,000".
    v = re.sub(r"([€£$₹¥])\s+", r"\1", v)
    v = re.sub(r"\b(US\$|A\$|C\$|NZ\$|R\$)\s+", r"\1", v, flags=re.IGNORECASE)
    v = re.sub(r"\b(USD|EUR|GBP|INR|CHF|AUD|CAD|NZD|ZAR|SEK|NOK|DKK|JPY|CNY|KES|NGN|PHP|SGD|THB|IDR|BRL|MXN)\s*",
               r"\1 ", v, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", v).strip()


def clean_amount(value: str) -> str:
    """Normalise an amount a source provided. Returns '' when it says nothing."""
    v = re.sub(r"\s+", " ", value or "").strip()
    v = _FURNITURE.sub("", v).strip(" ,;:-")
    if v.lower().strip(" .") in _JUNK_VALUES:
        return ""
    if not re.search(r"\d", v):
        return ""            # "other", "varies" — no figure, no value
    # Keep only up to the end of the last currency figure, dropping trailing prose.
    matches = list(re.finditer(_AMOUNT, v, re.IGNORECASE))
    if matches:
        v = v[: matches[-1].end()].strip(" ,;:-")
    return _tidy_number(v)[:256]


def extract_amount(*texts: str) -> str:
    """Recover a funding amount from free text; '' when none is stated."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return ""
    blob = re.sub(r"\s+", " ", blob)
    if not re.search(_CUR, blob, re.IGNORECASE):
        return ""
    for i, rx in enumerate(_PATTERNS):
        m = rx.search(blob)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if not groups:
            continue
        if len(groups) >= 2:                      # a range
            lo, hi = _tidy_number(groups[0]), _tidy_number(groups[1])
            if lo and hi:
                return f"{lo} – {hi}"[:256]
        one = _tidy_number(groups[0])
        if not one:
            continue
        # A bare figure is only trustworthy when the phrasing framed it as an
        # award size; otherwise it could be anything in the sentence.
        if i == len(_PATTERNS) - 1 and not re.search(
            r"\b(grant|award|fund|funding|budget|support|prize|financ|invest)", blob, re.IGNORECASE
        ):
            return ""
        prefix = "up to " if i in (3, 4) else ""
        return f"{prefix}{one}"[:256]
    return ""


def backfill_amounts() -> int:
    """Clean stored amounts and fill blank ones from existing text.

    Idempotent — rows already holding a good value are left alone.
    """
    import logging

    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    updated = 0
    with session_scope() as db:
        for opp in db.execute(select(Opportunity)).scalars().all():
            current = opp.funding_amount or ""
            new = clean_amount(current)
            if not new:
                new = extract_amount(opp.summary or "", opp.title or "")
            if new != current:
                opp.funding_amount = new
                updated += 1
    if updated:
        log.info("Amount backfill: updated %s opportunities", updated)
    return updated
