"""Which opportunities a person should read FIRST.

The problem
-----------
Routing already decides what reaches a member: the strict Active rule, their
categories, their verticals, their geography and their keywords all filter
before anything is sent. What none of that decides is ORDER.

So a digest arrived sorted by relevance score, or — for a member with no
keywords — by closing date alone. For a team whose work is in India that means
a Latin American call with a nearer deadline sits above the Indian one they
actually bid for, and the reader has to scan for their own country in a list
that was sorted by something else.

Why ordering, and not another filter
------------------------------------
This deliberately does NOT drop anything. Everything in the list already passed
the member's own geography filter — they asked for it. Re-filtering here would
silently override a choice they made, and the first time it went wrong nobody
would be able to tell whether a missing opportunity was never scraped or was
sorted out of existence.

Ordering is the safe half of the same idea: the Indian calls come first, the
South Asian ones next, and nothing is lost from the bottom.

Tiers, not a score
------------------
A single blended number would let a strong keyword match in Peru outrank a
weaker one in Delhi, which is exactly what this exists to stop. Tiers are
absolute: every tier-0 row sorts above every tier-1 row, and relevance decides
the order WITHIN a tier — where it is the right tool, because by then every
candidate is equally close to home.

Configurable, because it is a business fact
-------------------------------------------
"India first" is true for this team and would be wrong for another. Both lists
are settings, so a team working out of Nairobi changes one line of .env rather
than editing code.
"""
from __future__ import annotations

from dataclasses import dataclass

# The tier given to anything that matches nothing below. Not a magic number in
# the sort: it is len(countries) + len(regions), computed once.
UNRANKED = 9_999


def _csv(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


@dataclass(frozen=True)
class Priority:
    """The ordered home geography, read from settings."""

    countries: tuple[str, ...]
    regions: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.countries) + len(self.regions)

    def tier(self, country: str = "", region: str = "", location: str = "") -> int:
        """Lower is nearer home. Countries outrank regions.

        A country match beats a region match on purpose: "India" is more
        specific than "South Asia", and a row that names the country is the one
        a reader in Delhi wants at the top.

        `location` is consulted last and only as free text, because several
        sources fill it and leave `country` empty — DevelopmentAid stores
        "Malawi, Zambia" there. Substring matching on it is loose, so it is the
        final resort rather than the first check.
        """
        c = (country or "").strip().casefold()
        r = (region or "").strip().casefold()
        loc = (location or "").strip().casefold()

        for i, name in enumerate(self.countries):
            if c == name.casefold():
                return i
        for j, name in enumerate(self.regions):
            if r == name.casefold():
                return len(self.countries) + j

        # Nothing structured matched. Try the free-text location, and only for
        # a whole-word-ish containment — "India" must not match "Indiana", and
        # it would with a bare `in`.
        if loc:
            for i, name in enumerate(self.countries):
                if _mentions(loc, name):
                    return i
            for j, name in enumerate(self.regions):
                if _mentions(loc, name):
                    return len(self.countries) + j
        return UNRANKED

    def label(self, tier: int) -> str:
        """What to call a tier in the email, so the grouping is visible."""
        if tier < len(self.countries):
            return self.countries[tier]
        if tier < self.size:
            return self.regions[tier - len(self.countries)]
        return "Other regions"


def _mentions(haystack: str, needle: str) -> bool:
    """`needle` as a whole word inside `haystack`, both already casefolded.

    "India" in "Indiana" is True for a bare substring test and wrong for every
    purpose this has. Bounded by non-letters instead.
    """
    import re

    return re.search(rf"(?<![a-z]){re.escape(needle.casefold())}(?![a-z])",
                     haystack) is not None


def load() -> Priority:
    """The configured home geography.

    Defaults are this team's: India first, then South Asia, then Global —
    Global before the rest because a worldwide call is open to India by
    definition, where a call scoped to Latin America is not.
    """
    from app.core.config import settings

    return Priority(
        countries=tuple(_csv(getattr(settings, "digest_priority_countries",
                                     "India"))),
        regions=tuple(_csv(getattr(settings, "digest_priority_regions",
                                   "South Asia,Global"))),
    )


def sort_key(opportunity, priority: Priority | None = None):
    """Primary sort key for a digest: nearer home first.

    Returns just the tier. The caller appends whatever it was already sorting
    by — relevance score, then deadline — so the existing order survives
    intact inside each tier.
    """
    p = priority or load()
    return p.tier(
        country=getattr(opportunity, "country", "") or "",
        region=getattr(opportunity, "region", "") or "",
        location=getattr(opportunity, "location", "") or "",
    )


def group(opportunities, priority: Priority | None = None):
    """[(label, [rows])] in tier order, skipping tiers with nothing in them.

    For an email that shows its own grouping — "India (4)", "South Asia (2)" —
    rather than one undifferentiated list the reader has to sort by eye.
    """
    p = priority or load()
    buckets: dict[int, list] = {}
    for opp in opportunities:
        buckets.setdefault(sort_key(opp, p), []).append(opp)
    return [(p.label(t), buckets[t]) for t in sorted(buckets)]
