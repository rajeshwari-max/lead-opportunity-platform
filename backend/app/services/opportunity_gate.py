"""One test: is this row actually a funding or procurement opportunity?

Why this exists
---------------
The dashboard was showing rows that are not opportunities at all — navigation
links, news posts, programme landing pages, "our grantees" cards — because most
sources are scraped by the heuristic path in generic_listing.py: harvest every
link on the page, then reject the ones that look like site furniture. A
blocklist can only ever catch junk it has already met, so every new funder site
contributes fresh vocabulary and a few more wrong rows.

This flips it round. A row must show POSITIVE evidence that it is an
opportunity, and the evidence has to be one of a small, explicit set.

What counts as an opportunity here
----------------------------------
The team's own definition, and nothing wider: grants, RFPs/RFQs/RFIs/EOIs,
calls for proposals, funding opportunities, partnership opportunities, and
tenders — plus the adjacent forms the classifier already recognises
(fellowships, scholarships, awards, challenges).

Why "curated" sources skip the vocabulary test
----------------------------------------------
This is the part that a naive strict filter gets badly wrong.

UN Partner Portal's /cfei/open is a list of Calls for Expression of Interest.
EVERY row on it is an opportunity by construction — but its titles read
"Disability Inclusion Assessment" and "First Foods Gujarat — Implementation and
Capacity Support Partner". Neither contains a single funding word. A blanket
vocabulary requirement would delete a whole source of perfectly good leads.

So a scraper can declare `curated = True`, meaning "the page I read contains
opportunities and nothing else". Those rows only have to clear the furniture
test. Sources that are scraped by harvesting links off a general website get no
such benefit and must prove themselves row by row.

Curated is a claim about the SOURCE PAGE, not about quality: /cfei/open, a
tender board, and a procurement notice list qualify. A foundation's "Funding"
section, which mixes calls with news and programme pages, does not.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from app.services.links import is_furniture

# ---------------------------------------------------------------- positive
# Any ONE of these is enough. Kept deliberately broad in wording and strict in
# placement: the words must appear in the title or the row's own summary text,
# not merely somewhere on the page it came from.
_OPPORTUNITY_WORDS = re.compile(
    r"\b("
    # funding
    r"grants?|funding|fund|financial\s+support|seed\s+fund|"
    r"fellowships?|scholarships?|bursar(y|ies)|prizes?|awards?|"
    # solicitations
    r"rfps?|rfqs?|rfas?|rfis?|rfeis?|eois?|itbs?|icbs?|ncbs?|ltas?|"
    r"request\s+for\s+(proposals?|quotations?|applications?|information|"
    r"expressions?\s+of\s+interest|services?|tenders?)|"
    r"expressions?\s+of\s+interest|invitation\s+to\s+(bid|tender)|"
    r"call\s+for\s+(proposals?|applications?|proposals|expressions?|"
    r"proposal|partners?|concepts?|ideas?|submissions?)|"
    r"calls?\s+for\s+\w+|"
    r"terms\s+of\s+reference|solicitation|empanel(ment)?|prequalification|"
    # procurement / partnership
    r"tenders?|bids?|bidding|procurement\s+notice|consultanc(y|ies)|"
    r"partnership\s+opportunit(y|ies)|implementing\s+partner|"
    r"partner\s+selection|selection\s+of\s+(a\s+)?(partner|agency|consultant|"
    r"firm|organi[sz]ation|service\s+provider)|"
    # competitions
    r"challenges?|competitions?|accelerators?|incubators?|"
    # application language
    r"applications?\s+(are\s+)?(open|invited|welcome)|apply\s+(now|by|for)|"
    r"proposals?\s+(are\s+)?(invited|sought|welcome)|open\s+call"
    r")\b",
    re.IGNORECASE,
)

# A URL path that names a funding or procurement record.
_OPPORTUNITY_HREF = re.compile(
    r"/(grants?|funding|fund|opportunit\w*|calls?|call-for-\w+|tenders?|"
    r"rfps?|rfqs?|eois?|proposals?|solicitations?|awards?|fellowships?|"
    r"scholarships?|competitions?|challenges?|procurement|bids?|"
    r"cfei|notices?)(?=[-/?#._]|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- negative
# Page types that are never an opportunity, however much funding vocabulary
# they carry. A funder's news post about a grant round is *about* a call; it is
# not the call, and its link does not open one.
#
# Checked as whole path segments (the lookahead), for the same reason
# generic_listing._NAV_HREF needs it: as bare substrings "/news" matched
# "/newsletter-signup" and "/press" matched "/pressure", which is how real
# listings got discarded while junk survived.
_NOT_AN_OPPORTUNITY_HREF = re.compile(
    r"/(news|blog|blogs|press|press-releases?|media|stories|story|article|"
    r"articles|events?|webinars?|podcasts?|videos?|publications?|reports?|"
    r"annual-reports?|research|insights?|resources?|library|about|about-us|"
    r"team|people|staff|board|careers?|jobs?|vacanc\w*|contact|privacy|terms|"
    r"cookies?|sitemap|search|tag|tags|category|categories|author|feed|"
    r"grantees?|our-grantees?|past-grants?|awarded|portfolio|case-stud\w*|"
    r"impact|annual-review|newsletter|subscribe|donate|login|signin|register)"
    r"(?=[-/?#._]|$)",
    re.IGNORECASE,
)

# Titles that are a heading or a programme name rather than a call. Anchored, so
# a real call that merely contains the word survives ("Grants Programme 2026:
# Call for Proposals" is kept; a bare "Our Grants" is not).
_HEADING_TITLE = re.compile(
    r"^(our\s+|the\s+)?(grants?|funding|funds?|programmes?|programs?|"
    r"opportunit(y|ies)|tenders?|notices?|calls?|awards?|fellowships?|"
    r"scholarships?|partners(hips?)?|grantees?|portfolio|initiatives?|"
    r"projects?|work|approach|strategy|impact|resources?|apply|"
    r"how\s+to\s+apply|eligibility|guidelines?|faqs?)"
    r"(\s+(programme|program|page|overview|home|database|search|list))?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- past awards
# A grant that has ALREADY been given is not an opportunity, and it is the
# hardest kind of junk to reject: "$500,000 grant to the Clean Air Institute for
# monitoring in Lagos" carries every funding word a real call carries, so both
# the vocabulary test and the amount test pass it happily.
#
# Nine sources in the audit point at exactly these pages — "our grants",
# "committed grants", "grantees", "grants database", "past calls" — so this is
# not a rare edge case, it is a whole category of source.
#
# Matched on PAST-TENSE phrasing only. "Award" alone is not enough and never
# will be: an award is also a thing you apply for ("Young Scientist Award 2026:
# call for nominations"), and rejecting the word would delete real calls. What
# gives a past award away is the grammar around it — awarded TO someone,
# someone RECEIVES, we ANNOUNCE the recipients.
_ALREADY_AWARDED = re.compile(
    r"\b("
    r"award(ed|s)\s+(to|\$|£|€|₹|a\s|the\s|over\s|more\s+than)|"
    # Procurement's own name for a decision already taken. Explicit because it
    # is followed by a colon, not by "to" — and because a tender board publishes
    # these alongside its open notices, so a curated source needs it too.
    r"contract\s+award|award\s+notice|notice\s+of\s+award|"
    r"(has|have|were|was)\s+(been\s+)?(awarded|selected|granted|funded)|"
    r"receiv(es|ed)\s+(a\s+|an\s+|\$|£|€|₹|funding|grant|award)|"
    r"win(s|ner|ners)\b|"
    # "Announcing our 2026 grantees" — the noun can sit several words after
    # the verb ("our", a year, a count), so this spans up to 30 characters
    # rather than trying to enumerate the determiners.
    r"announc\w+[^.!?]{0,30}?"
    r"(recipients?|grantees?|winners?|awardees?|fellows|cohort)|"
    r"meet\s+(our|the)\s+(grantees?|winners?|recipients?)|"
    r"grantees?\s+(announced|selected|named)|"
    r"congratulat\w+|"
    r"our\s+(latest\s+)?(grantees?|investments?|portfolio)|"
    r"grant\s+to\s+\w+|"
    r"funded\s+(projects?|organi[sz]ations?|partners?)"
    r")\b",
    re.IGNORECASE,
)


def is_already_awarded(title: str, summary: str = "") -> bool:
    """True when this reads as a grant already given, not one being offered."""
    return bool(_ALREADY_AWARDED.search(f"{title or ''} {(summary or '')[:400]}"))


def _path(url: str) -> str:
    try:
        return (urlparse(url or "").path or "").rstrip("/")
    except ValueError:
        return ""


def is_opportunity(
    title: str,
    summary: str = "",
    url: str = "",
    category: str = "",
    curated: bool = False,
) -> tuple[bool, str]:
    """(keep, reason). `reason` is empty when kept, and names the rule when not.

    The reason is returned rather than logged internally so the cleanup script
    can group rejections by cause — "1,204 rows rejected" is not actionable,
    "1,110 of them are /news/ pages from four sources" is.
    """
    t = " ".join((title or "").split())
    if is_furniture(t, url):
        return False, "furniture"
    if _HEADING_TITLE.match(t):
        return False, "section heading, not a call"

    path = _path(url)
    # The negative URL test applies to EVERY source, curated included: a
    # curated board can still link out to its own news page, and that link is
    # not an opportunity wherever it was found.
    if path and _NOT_AN_OPPORTUNITY_HREF.search(path) and \
            not _OPPORTUNITY_HREF.search(path):
        return False, "page type is never an opportunity"

    # Also every source, curated included. A tender board publishes contract
    # awards alongside its open notices — World Bank's feed is mostly awards —
    # so "this page only contains opportunities" does not mean "and none of them
    # have already been decided".
    if is_already_awarded(t, summary):
        return False, "a grant already awarded, not one being offered"

    if curated:
        # The source page contains opportunities and nothing else, so the row
        # does not have to say "grant" to be one. See the module docstring.
        return True, ""

    cat = str(category or "").strip().lower()
    if cat and cat not in ("", "other"):
        # The classifier matched real solicitation or funding vocabulary. That
        # is the same evidence this function looks for, already computed.
        return True, ""

    haystack = f"{t} {' '.join((summary or '').split())[:1200]}"
    if _OPPORTUNITY_WORDS.search(haystack):
        return True, ""
    if path and _OPPORTUNITY_HREF.search(path):
        return True, ""
    return False, "no opportunity signal"


def rejection_summary(rows) -> dict[str, int]:
    """Tally reasons over an iterable of (keep, reason) results."""
    out: dict[str, int] = {}
    for keep, reason in rows:
        if not keep:
            out[reason] = out.get(reason, 0) + 1
    return out
