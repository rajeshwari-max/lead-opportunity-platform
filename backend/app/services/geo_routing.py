"""Where a member works, as a routing axis. Off until somebody sets it.

Why this is the last gap
------------------------
Measured on 4,000 recent actionable rows:

    (blank)           405   10.1%
    United States     301    7.5%
    United Kingdom    284    7.1%
    Australia         254    6.3%
    Austria           213    5.3%
    Canada            189    4.7%
    ...
    India              71    1.8%

Roughly a third of the database is high-income-country listings, and
`TeamMember` had keywords, categories and verticals but **no geography at
all**. Geography existed only as a dashboard filter, so the digest ignored it —
which is how "Banyule Environment Grants Round – Individuals (Australia)"
reached a member whose filter reads Health / E4C / Livelihood.

Two design decisions, both about not breaking what works
--------------------------------------------------------
**Empty means everywhere.** Every other routing field on `TeamMember` already
works that way, and anything else would change what all four members receive
the moment this deploys. Nobody's mail changes until they choose a geography.

**A row with no country is INCLUDED by default.** 10% of rows have no country
at all — a geographic filter cannot see them either way, and that is a data
gap, not evidence the opportunity is somewhere else. Excluding them by default
would silently drop one row in ten from every filtered digest, and the person
who set a filter for "South Asia" would never learn that "unknown" had been
quietly read as "not South Asia". It is a per-member switch for anyone who
would rather have the tighter list.

Region beats country, and both are ORed
---------------------------------------
A member who selects "South Asia" and "Kenya" wants both — the region and the
one country outside it. Requiring a row to satisfy every selection would make
each addition narrow the list, which is the opposite of what picking more
places means.
"""
from __future__ import annotations

from sqlalchemy import or_

from app.database.models import Opportunity
from app.services.geography import REGIONS, canonical_country, region_for_country


class GeoError(ValueError):
    """A geography that cannot be applied, with a reason meant for a person."""


def parse_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def normalize_countries(value) -> tuple[str, list[str]]:
    """(canonical csv, unrecognised values).

    Unrecognised names are returned rather than dropped. A country nobody
    recognises matches nothing, which looks exactly like a working filter that
    happens to find nothing — the same failure the vertical rename had.
    """
    names = parse_csv(value) if isinstance(value, str) else list(value or [])
    keep: list[str] = []
    unknown: list[str] = []
    for name in names:
        canon = canonical_country(name)
        if not canon:
            unknown.append(name)
        elif canon not in keep:
            keep.append(canon)
    return ", ".join(keep), unknown


def normalize_regions(value) -> tuple[str, list[str]]:
    names = parse_csv(value) if isinstance(value, str) else list(value or [])
    keep: list[str] = []
    unknown: list[str] = []
    for name in names:
        match = next((r for r in REGIONS if r.casefold() == name.strip().casefold()),
                     "")
        if not match:
            unknown.append(name)
        elif match not in keep:
            keep.append(match)
    return ", ".join(keep), unknown


def geo_clause(countries, regions, include_unknown: bool = True):
    """SQL for "is this opportunity somewhere this member works".

    Returns None when nothing is selected, so the caller adds no filter at all
    rather than one that is trivially true — an always-true clause in the query
    plan is slower and reads, wrongly, like a filter that was applied.
    """
    countries = [c for c in (countries or []) if c]
    regions = [r for r in (regions or []) if r]
    if not countries and not regions:
        return None

    terms = []
    if countries:
        terms.append(Opportunity.country.in_(countries))
    if regions:
        terms.append(Opportunity.region.in_(regions))
        # A row whose country is set but whose region was never derived still
        # belongs to its region. Without this, selecting "South Asia" would
        # miss every Indian row that predates the geography backfill.
        implied = [c for c in _countries_in(regions)]
        if implied:
            terms.append(Opportunity.country.in_(implied))
    if include_unknown:
        # Not "matches everything" — specifically the rows where geography is
        # unknown, which a filter cannot judge either way.
        terms.append(
            (Opportunity.country.is_(None) | (Opportunity.country == ""))
            & (Opportunity.region.is_(None) | (Opportunity.region == ""))
        )
    return or_(*terms)


def _countries_in(regions) -> list[str]:
    """Every country this module can map into one of these regions.

    Derived from the geography tables rather than listed by hand, so a country
    added there is routed correctly here without anyone remembering to.
    """
    from app.services.geography import _COUNTRY_REGION  # noqa: PLC0415

    # The map is keyed lowercase; the `country` column stores canonical
    # (title-cased) names, so each key is put back through canonical_country
    # rather than title-cased here — "Cote d'Ivoire" and "Guinea-Bissau" do not
    # survive a naive .title().
    wanted = {r.casefold() for r in regions}
    out = set()
    for key, region in _COUNTRY_REGION.items():
        if (region or "").casefold() in wanted:
            canon = canonical_country(key)
            if canon:
                out.add(canon)
    return sorted(out)


def describe(countries, regions, include_unknown: bool) -> str:
    """One line a person can check their own routing against."""
    parts = []
    if regions:
        parts.append(", ".join(regions))
    if countries:
        parts.append(", ".join(countries))
    if not parts:
        return "everywhere (no geography set)"
    where = " and ".join(parts)
    tail = ("; rows with no country are included"
            if include_unknown else "; rows with no country are excluded")
    return where + tail
