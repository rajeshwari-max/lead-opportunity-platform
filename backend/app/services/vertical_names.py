"""Old vertical names, and why a member's saved routing still works by luck.

The situation
-------------
The verticals were renamed at some point:

    "E4C"                   ->  "E4C(Evidence for Change)"
    "Climate/Sustainability" ->  "Climate/Sustainability(ESG)"

`backfill_verticals()` fixed the ~1,000 OPPORTUNITY rows carrying the old
labels. Nothing fixed the TEAM MEMBERS, and the routing audit found one still
saved with both spellings:

    osama   verticals: Climate/Sustainability, Climate/Sustainability(ESG)

That still routes correctly today, but only because the vertical filter is a
substring test::

    Opportunity.verticals.like("%Climate/Sustainability%")

and the old name happens to be a prefix of the new one. It is working by
accident. The moment anyone makes vertical matching exact — which is the
correct thing to do, since substring matching on a comma-separated list is the
same class of bug as the keyword one just fixed — that member's routing
silently empties, and nobody finds out until they notice they have stopped
receiving mail.

So the rename is finished here rather than left as a trap: the stored value is
normalised, and the resolver keeps accepting the old spelling so that a member
record written before the migration is understood rather than dropped.
"""
from __future__ import annotations

from app.services.verticals import VERTICALS

# Old spelling -> canonical. Only genuine renames belong here; this is not a
# place for aliases someone might find convenient, because every entry is a
# name the system will keep answering to forever.
LEGACY_NAMES: dict[str, str] = {
    "E4C": "E4C(Evidence for Change)",
    "Climate/Sustainability": "Climate/Sustainability(ESG)",
    "Climate / Sustainability": "Climate/Sustainability(ESG)",
    "ESG": "Climate/Sustainability(ESG)",
    "WWB": "Worker Wellbeing",
}


def canonical_vertical(name: str) -> str:
    """The current name for a vertical, or "" if it is not one of ours.

    Returning "" for an unknown value is deliberate. Passing it through would
    let a typo sit in a member's routing forever, matching nothing, looking
    exactly like a correctly configured filter that happens to find nothing.
    """
    value = (name or "").strip()
    if not value:
        return ""
    for known in VERTICALS:
        if value.casefold() == known.casefold():
            return known
    for old, new in LEGACY_NAMES.items():
        if value.casefold() == old.casefold():
            return new
    return ""


def normalize_vertical_csv(value: str) -> tuple[str, list[str]]:
    """Clean a member's saved vertical list.

    Returns (normalised csv, values that were not recognised). Duplicates
    collapse — "Climate/Sustainability, Climate/Sustainability(ESG)" is one
    vertical written twice, and storing it twice makes a filter that looks
    like it covers two things.

    Unrecognised values are RETURNED rather than silently dropped, so a caller
    can report them. Deleting part of someone's routing configuration without
    telling them is how a filter quietly stops matching what they expect.
    """
    seen: list[str] = []
    unknown: list[str] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        canon = canonical_vertical(part)
        if not canon:
            unknown.append(part)
            continue
        if canon not in seen:
            seen.append(canon)
    return ", ".join(seen), unknown
