"""Country / region normalisation — keeps the two columns cleanly separated.

Scrapers pull location text from wildly different places (a title fragment, a
card label, a hardcoded default), so before this module the `country` column
was accumulating three kinds of non-country values:

  * region names  — "Africa", "Global", "Latin America and Caribbean"
  * title artifacts — "Round 1", "Cycle 2", "9th Edition", "Phase XIII"
  * alias splits  — "UK" and "United Kingdom" counted as two separate filters,
                     likewise "US" / "U.S." / "United States"

while `region` was left empty on ~1,500 rows that had a perfectly resolvable
country. The filter sidebar then showed regions mixed into the country list and
region options that matched zero rows.

`normalize_geo()` is the single place that decides what belongs in each column.
It is deliberately conservative: an unrecognised country is passed through
untouched with a blank region rather than guessed at, so a country missing from
the table below degrades to "no region" instead of a wrong region.
"""
from __future__ import annotations

import re

# Canonical region buckets — mirrors settings.default_regions.
AFRICA = "Africa"
SOUTH_ASIA = "South Asia"
EAST_ASIA = "East Asia"
SOUTHEAST_ASIA = "Southeast Asia"
CENTRAL_ASIA = "Central Asia"
EUROPE = "Europe"
MIDDLE_EAST = "Middle East"
LATIN_AMERICA = "Latin America"
NORTH_AMERICA = "North America"
OCEANIA = "Oceania"
GLOBAL = "Global"

REGIONS: list[str] = [
    AFRICA, SOUTH_ASIA, EAST_ASIA, SOUTHEAST_ASIA, CENTRAL_ASIA,
    EUROPE, MIDDLE_EAST, LATIN_AMERICA, NORTH_AMERICA, OCEANIA, GLOBAL,
]

# Values that name a region/scope rather than a country. When one of these
# turns up in the country column it is moved to region and country is cleared.
_REGION_ALIASES: dict[str, str] = {
    "africa": AFRICA,
    "sub-saharan africa": AFRICA,
    "sub saharan africa": AFRICA,
    "north africa": AFRICA,
    "west africa": AFRICA,
    "east africa": AFRICA,
    "southern africa": AFRICA,
    "south asia": SOUTH_ASIA,
    "east asia": EAST_ASIA,
    "southeast asia": SOUTHEAST_ASIA,
    "south east asia": SOUTHEAST_ASIA,
    "south-east asia": SOUTHEAST_ASIA,
    "central asia": CENTRAL_ASIA,
    "asia": SOUTH_ASIA,        # bare "Asia" is ambiguous; South Asia is this
                               # platform's dominant Asian bucket
    "asia pacific": OCEANIA,
    "europe": EUROPE,
    "eu": EUROPE,
    "european union": EUROPE,
    "middle east": MIDDLE_EAST,
    "mena": MIDDLE_EAST,
    "latin america": LATIN_AMERICA,
    "latin america and caribbean": LATIN_AMERICA,
    "latin america & caribbean": LATIN_AMERICA,
    "caribbean": LATIN_AMERICA,
    "south america": LATIN_AMERICA,
    "central america": LATIN_AMERICA,
    "north america": NORTH_AMERICA,
    "oceania": OCEANIA,
    "pacific": OCEANIA,
    "global": GLOBAL,
    "worldwide": GLOBAL,
    "international": GLOBAL,
    "any country": GLOBAL,
    "all countries": GLOBAL,
}

# Country spellings that should collapse to one canonical name so the filter
# list doesn't show the same place twice.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "usa": "United States",
    "america": "United States",
    "uae": "United Arab Emirates",
    "u.a.e.": "United Arab Emirates",
    "kyrgyz republic": "Kyrgyzstan",
    "slovak republic": "Slovakia",
    "czechia": "Czech Republic",
    "republic of korea": "South Korea",
    "korea": "South Korea",
    "russia": "Russian Federation",
    "ivory coast": "Côte d’Ivoire",
    "cote d'ivoire": "Côte d’Ivoire",
    "drc": "Congo",
    "democratic republic of congo": "Congo",
    "viet nam": "Vietnam",
    "burma": "Myanmar",
    "cabo verde": "Cape Verde",
    "swaziland": "Eswatini",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "palestinian territories": "Palestine",
    "holland": "Netherlands",
}

# Canonical country -> region. Only countries listed here get a region.
_COUNTRY_REGION: dict[str, str] = {}


def _add(region: str, *countries: str) -> None:
    for c in countries:
        _COUNTRY_REGION[c.lower()] = region


_add(AFRICA,
     "Algeria", "Angola", "Benin", "Botswana", "Burkina Faso", "Burundi",
     "Cameroon", "Cape Verde", "Central African Republic", "Chad", "Comoros",
     "Congo", "Côte d’Ivoire", "Djibouti", "Egypt", "Equatorial Guinea",
     "Eritrea", "Eswatini", "Ethiopia", "Gabon", "Gambia", "Ghana", "Guinea",
     "Guinea-Bissau", "Kenya", "Lesotho", "Liberia", "Libya", "Madagascar",
     "Malawi", "Mali", "Mauritania", "Mauritius", "Morocco", "Mozambique",
     "Namibia", "Niger", "Nigeria", "Rwanda", "Senegal", "Seychelles",
     "Sierra Leone", "Somalia", "South Africa", "South Sudan", "Sudan",
     "Tanzania", "Togo", "Tunisia", "Uganda", "Zambia", "Zimbabwe")

_add(SOUTH_ASIA,
     "Afghanistan", "Bangladesh", "Bhutan", "India", "Maldives", "Nepal",
     "Pakistan", "Sri Lanka")

_add(EAST_ASIA,
     "China", "Hong Kong", "Japan", "Macau", "Mongolia", "North Korea",
     "South Korea", "Taiwan")

_add(SOUTHEAST_ASIA,
     "Brunei", "Cambodia", "Indonesia", "Laos", "Malaysia", "Myanmar",
     "Philippines", "Singapore", "Thailand", "Timor-Leste", "Vietnam")

_add(CENTRAL_ASIA,
     "Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan", "Uzbekistan")

_add(EUROPE,
     "Albania", "Andorra", "Armenia", "Austria", "Azerbaijan", "Belarus",
     "Belgium", "Bosnia and Herzegovina", "Bulgaria", "Croatia", "Cyprus",
     "Czech Republic", "Denmark", "Estonia", "Finland", "France", "Georgia",
     "Germany", "Greece", "Hungary", "Iceland", "Ireland", "Italy", "Kosovo",
     "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta", "Moldova",
     "Monaco", "Montenegro", "Netherlands", "North Macedonia", "Norway",
     "Poland", "Portugal", "Romania", "Russian Federation", "San Marino",
     "Serbia", "Slovakia", "Slovenia", "Spain", "Sweden", "Switzerland",
     "Ukraine", "United Kingdom")

_add(MIDDLE_EAST,
     "Bahrain", "Iran", "Iraq", "Israel", "Jordan", "Kuwait", "Lebanon",
     "Oman", "Palestine", "Qatar", "Saudi Arabia", "Syria", "Turkey",
     "United Arab Emirates", "Yemen")

_add(NORTH_AMERICA, "Canada", "United States")

_add(LATIN_AMERICA,
     "Antigua and Barbuda", "Argentina", "Bahamas", "Barbados", "Belize",
     "Bolivia", "Brazil", "Chile", "Colombia", "Costa Rica", "Cuba",
     "Dominica", "Dominican Republic", "Ecuador", "El Salvador", "Grenada",
     "Guatemala", "Guyana", "Haiti", "Honduras", "Jamaica", "Mexico",
     "Nicaragua", "Panama", "Paraguay", "Peru", "Saint Lucia", "Suriname",
     "Trinidad and Tobago", "Uruguay", "Venezuela")

_add(OCEANIA,
     "Australia", "Fiji", "Kiribati", "Marshall Islands", "Micronesia",
     "Nauru", "New Zealand", "Palau", "Papua New Guinea", "Samoa",
     "Solomon Islands", "Tonga", "Tuvalu", "Vanuatu")

# Title-parsing artifacts that are not places at all — e.g. FundsForNGOs
# derives country from a title fragment, which yields "Round 1", "Cycle 2",
# "9th Edition", "Phase XIII".
_JUNK = re.compile(
    r"^(round|cycle|phase|edition|batch|call|window|tranche|stage|series|"
    r"cohort|wave|intake|version)\b|"
    r"^\d+(st|nd|rd|th)?\s+(round|cycle|phase|edition|batch|call|cohort|window|"
    r"wave|intake|tranche)\b|"
    r"^(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+"
    r"(round|cycle|phase|edition|cohort|window)\b",
    re.IGNORECASE,
)

# A country name never contains money or is mostly digits. Sources that derive
# country from a title fragment pick up amounts and years — "($10,000 – $55,000)"
# became the countries "$10", "000 to $55" and "000" once split on the comma.
_NOT_A_PLACE = re.compile(
    r"[$€£₹¥]|\b(usd|eur|gbp|inr|chf|aud|cad|nzd|zar|sek|nok|dkk|jpy)\b|"
    r"^\d{4}(\s*[-–/]\s*\d{2,4})?$|"          # bare years: 2026, 2025-26
    r"^\d+\s*(k|m|bn|million|billion|lakh|crore)\b|"
    r"^up\s+to\b|^from\b|^under\b|^over\b",
    re.IGNORECASE,
)

# "UK and India", "Hungary, Tunisia", "United States & Israel" — a multi-country
# string can't populate a single-value column, so the first named country wins
# (it is consistently the primary one in the observed data) and the rest are
# dropped rather than leaving an unfilterable compound string.
_SPLIT = re.compile(
    r"\s*(?:,|;|/|\||&|–|—|\band\b|\bwith\b|\bor\b)\s*", re.IGNORECASE
)

# Dependencies, overseas territories and disputed areas. Not UN member states,
# so absent from the country table above, but they are the real geography of
# real calls (Pacific and Caribbean funds especially) and must survive the
# whitelist below rather than being discarded as junk.
_COUNTRY_REGION.update({
    "american samoa": OCEANIA, "cook islands": OCEANIA, "french polynesia": OCEANIA,
    "new caledonia": OCEANIA, "niue": OCEANIA, "tokelau": OCEANIA,
    "wallis and futuna": OCEANIA, "guam": OCEANIA, "northern mariana islands": OCEANIA,
    "norfolk island": OCEANIA, "pitcairn islands": OCEANIA,
    "anguilla": LATIN_AMERICA, "aruba": LATIN_AMERICA, "bermuda": LATIN_AMERICA,
    "british virgin islands": LATIN_AMERICA, "caribbean netherlands": LATIN_AMERICA,
    "cayman islands": LATIN_AMERICA, "curaçao": LATIN_AMERICA,
    "french guiana": LATIN_AMERICA, "guadeloupe": LATIN_AMERICA,
    "martinique": LATIN_AMERICA, "montserrat": LATIN_AMERICA,
    "puerto rico": LATIN_AMERICA, "saint barthélemy": LATIN_AMERICA,
    "saint martin": LATIN_AMERICA, "sint maarten": LATIN_AMERICA,
    "turks and caicos islands": LATIN_AMERICA,
    "u.s. virgin islands": LATIN_AMERICA, "us virgin islands": LATIN_AMERICA,
    "åland islands": EUROPE, "aland islands": EUROPE, "channel islands": EUROPE,
    "faroe islands": EUROPE, "gibraltar": EUROPE, "greenland": EUROPE,
    "guernsey": EUROPE, "isle of man": EUROPE, "jersey": EUROPE,
    "kosovo": EUROPE, "vatican city": EUROPE,
    "réunion": AFRICA, "reunion": AFRICA, "mayotte": AFRICA,
    "saint helena": AFRICA, "somaliland": AFRICA, "western sahara": AFRICA,
    "hong kong": EAST_ASIA, "macau": EAST_ASIA, "taiwan": EAST_ASIA,
    "palestine": MIDDLE_EAST, "west bank and gaza": MIDDLE_EAST,
    # Sovereign states the base table is missing. "congo" alone is already
    # there for Congo-Brazzaville, so the DRC needs its own entry rather than
    # an alias onto it — they are different countries with different calls.
    "dr congo": AFRICA, "sao tome and principe": AFRICA,
    "saint kitts and nevis": LATIN_AMERICA,
    "saint vincent and the grenadines": LATIN_AMERICA,
})

# Short forms, official long forms, and the misspellings that actually occur in
# scraped listings. Mapping them keeps two spellings of one country from sitting
# side by side in the filter.
_COUNTRY_ALIASES.update({
    "dem. rep. congo": "DR Congo", "dem rep congo": "DR Congo",
    "congo dr": "DR Congo", "congo, dem. rep.": "DR Congo",
    "democratic republic of the congo": "DR Congo",
    "bosnia": "Bosnia and Herzegovina",
    "eswatini (swaziland)": "Eswatini", "swaziland": "Eswatini",
    "trinidad": "Trinidad and Tobago", "antigua": "Antigua and Barbuda",
    "saint vincent": "Saint Vincent and the Grenadines",
    "st vincent": "Saint Vincent and the Grenadines",
    "saint kitts": "Saint Kitts and Nevis", "st kitts": "Saint Kitts and Nevis",
    "sao tome": "Sao Tome and Principe",
    "são tomé": "Sao Tome and Principe",
    "macedonia": "North Macedonia", "fyrom": "North Macedonia",
    "netherland": "Netherlands", "the netherlands": "Netherlands",
    "holland": "Netherlands",
    "republic of ireland": "Ireland",
    "cote d ivoire": "Côte d’Ivoire", "cote d'ivoire": "Côte d’Ivoire",
    "cote d’ivoire": "Côte d’Ivoire", "ivory coast": "Côte d’Ivoire",
    "hongkong": "Hong Kong",
    "lao pdr": "Laos", "lao people's democratic republic": "Laos",
    "syrian arab republic": "Syria",
    "srilanka": "Sri Lanka", "timor leste": "Timor-Leste",
    "new zeland": "New Zealand", "ukarine": "Ukraine",
    "united kindom": "United Kingdom", "u.k.": "United Kingdom",
    "u.s": "United States", "u.s.": "United States",
    "united states of america": "United States",
    # NB: the base table's canonical name is "Russian Federation" — aliasing the
    # other way round created a cycle (russia -> Russian Federation -> russia)
    # that made the backfill flip the same rows on every run.
    "viet nam": "Vietnam",
    "republic of korea": "South Korea", "korea, rep.": "South Korea",
    "wallis": "Wallis and Futuna",
    "cabo verde": "Cape Verde", "türkiye": "Turkey", "turkiye": "Turkey",
    "czechia": "Czech Republic", "burma": "Myanmar",
    # unaccented spellings of territories, so one place is one filter entry
    "aland islands": "Åland Islands", "reunion": "Réunion",
    "curacao": "Curaçao", "saint barthelemy": "Saint Barthélemy",
    "us virgin islands": "U.S. Virgin Islands",
    "virgin islands, u.s.": "U.S. Virgin Islands",
})

# Continent- and scope-level values that turn up in the country column. They are
# real geography, just not countries — so they move to the region column instead
# of being thrown away.
_REGION_ALIASES.update({
    "the caribbean": LATIN_AMERICA, "caribbean": LATIN_AMERICA,
    "latin american": LATIN_AMERICA, "central america": LATIN_AMERICA,
    "south america": LATIN_AMERICA,
    "asia-pacific": OCEANIA, "asia pacific": OCEANIA, "pacific": OCEANIA,
    "eastern europe": EUROPE, "western europe": EUROPE,
    "europe non eu 27": EUROPE, "europe non eu...": EUROPE,
    # DevelopmentAid's most common non-country scope — 1,660 rows that had
    # neither a country nor a region until it was mapped.
    "eu 27": EUROPE, "eu-27": EUROPE, "eu27": EUROPE,
    "european union": EUROPE, "eu": EUROPE, "balkans": EUROPE,
    "northern america": NORTH_AMERICA,
    "the middle east": MIDDLE_EAST, "mena": MIDDLE_EAST,
    "sub-saharan africa": AFRICA, "west africa": AFRICA, "east africa": AFRICA,
    "southern africa": AFRICA, "north africa": AFRICA,
    "global south": GLOBAL, "developing countries": GLOBAL,
    "selected countries": GLOBAL, "various": GLOBAL, "various - see site": GLOBAL,
    "multiple countries": GLOBAL, "all countries": GLOBAL,
})


_SMALL_WORDS = {"and", "of", "the", "da", "del", "de", "des", "du"}


def _titlecase(name: str) -> str:
    """Display casing for a country name held in lowercase in the tables."""
    words = []
    for i, word in enumerate(name.split()):
        if "." in word:                       # u.s. -> U.S.
            words.append(word.upper())
        elif word in _SMALL_WORDS and i:      # bosnia and herzegovina
            words.append(word)
        elif "’" in word or "'" in word:      # d’ivoire -> d’Ivoire
            head, sep, tail = word.replace("'", "’").partition("’")
            words.append(head + sep + tail.capitalize())
        elif "-" in word:                     # guinea-bissau -> Guinea-Bissau
            words.append("-".join(p.capitalize() for p in word.split("-")))
        else:
            words.append(word.capitalize())
    return " ".join(words)


# One canonical spelling per country, so scraped casing can never split a
# country into two filter entries ("Hong kong" alongside "Hong Kong").
# Alias targets are already written the way they should display, so they win;
# everything else is title-cased from the table key.
_DISPLAY: dict[str, str] = {k.lower(): _titlecase(k) for k in _COUNTRY_REGION}
_DISPLAY.update({v.lower(): v for v in _COUNTRY_ALIASES.values()})
# An alias always wins over the table's own title-casing, so drop the shadowed
# entry — otherwise "aland islands" would still hold "Aland Islands" alongside
# the accented "Åland Islands" the alias resolves to.
for _key in _COUNTRY_ALIASES:
    if _COUNTRY_ALIASES[_key].lower() != _key:
        _DISPLAY.pop(_key, None)


def canonical_country(value: str) -> str:
    """Canonical country name, or '' if the value isn't a recognised country.

    This is a whitelist, deliberately. Pattern-based rejection was tried first
    and could not hold the line: the country column had accumulated funder
    acronyms (GIZ, UNDP, DAAD), lot numbers ("Lot 4", "Track 2"), call names
    ("Second Call", "VI Edition") and stray prose ("Apply Now") — 138 distinct
    non-countries, each needing its own rule. Anything not in the country /
    territory table is now dropped, so no future scraper can add a new kind of
    junk to the filter list.
    """
    v = (value or "").strip().strip(".,;:-–—()[]").strip()
    if not v or _JUNK.search(v) or _NOT_A_PLACE.search(v):
        return ""
    alias = _COUNTRY_ALIASES.get(v.lower())
    if alias:
        return alias
    return _DISPLAY.get(v.lower(), "")


def region_for_country(country: str) -> str:
    """Region for a canonical country name, or '' when unknown (never guessed)."""
    return _COUNTRY_REGION.get((country or "").strip().lower(), "")


def normalize_geo(country: str, region: str = "") -> tuple[str, str]:
    """Return a cleanly separated (country, region) pair.

    Rules, in order:
      1. A region/scope name in the country column ("Africa", "Global") moves to
         region and clears country — those are not countries.
      2. A compound value ("UK and India") keeps its first resolvable country.
      3. Aliases collapse ("UK" -> "United Kingdom").
      4. Junk fragments ("Round 1") are dropped entirely.
      5. Region is filled in from the country when the country is known; an
         explicitly scraped region is trusted and never overwritten.
    """
    raw_country = (country or "").strip()
    out_region = (region or "").strip()

    # An explicitly scraped region still gets alias-normalised so "Worldwide"
    # and "Global" don't sit side by side in the filter list.
    if out_region:
        out_region = _REGION_ALIASES.get(out_region.lower(), out_region)

    if not raw_country:
        return "", out_region

    # 1. whole value is a region/scope
    as_region = _REGION_ALIASES.get(raw_country.lower())
    if as_region:
        return "", out_region or as_region

    # 2/3/4. resolve the (possibly compound) country value
    # Don't split a name that is itself a country. "Trinidad and Tobago",
    # "Bosnia and Herzegovina", "Antigua and Barbuda" contain the same "and"
    # that separates a compound list, and splitting them produced "Trinidad".
    whole = canonical_country(raw_country)
    if whole:
        return whole, out_region or region_for_country(whole)

    candidates = [p for p in _SPLIT.split(raw_country) if p.strip()] or [raw_country]
    resolved = ""
    scope_region = ""
    for part in candidates:
        # A region name inside a compound ("EU 27, India"). Held aside rather
        # than adopted straight away: it only describes the rows where no
        # actual country resolves. Letting it win outright filed India under
        # Europe, because the scope was simply listed first.
        part_region = _REGION_ALIASES.get(part.strip().lower())
        if part_region:
            scope_region = scope_region or part_region
            continue
        cand = canonical_country(part)
        if cand and region_for_country(cand):   # prefer a country we can place
            resolved = cand
            break
        if cand and not resolved:               # fall back to first plausible
            resolved = cand

    if not resolved:
        return "", out_region or scope_region

    # 5. the country's own region always beats a scope mentioned alongside it;
    #    an explicitly scraped region still outranks both.
    return resolved, out_region or region_for_country(resolved) or scope_region


def backfill_geography() -> int:
    """Re-normalise country/region on existing rows. Safe to run repeatedly."""
    import logging

    from sqlalchemy import select

    from app.database.db import session_scope
    from app.database.models import Opportunity

    log = logging.getLogger("scraper")
    updated = 0
    with session_scope() as db:
        rows = db.execute(select(Opportunity)).scalars().all()
        for opp in rows:
            # Some sources only ever fill `location` (DevelopmentAid's API stores
            # "Malawi, Zambia" there and nothing in country), which left the
            # Country filter and the By Region chart empty for tens of thousands
            # of rows. Fall back to it so those resolve.
            #
            # `location` wins whenever it names a country, because it is the
            # string the listing actually published — `country` is derived, and
            # a derived value can be stale. Rows normalised before the territory
            # table existed had skipped past names we could not resolve then and
            # settled on a later one: "Antigua and Barbuda, Bahamas, Barbados…"
            # was stored as Bahamas. Re-deriving corrects those.
            #
            # When location holds sub-national detail ("Haryana, Rajasthan") it
            # resolves to nothing and the stored country is kept.
            # Region is deliberately re-derived from scratch (passing "" rather
            # than opp.region). normalize_geo trusts an incoming region because
            # at scrape time it came from the source — but the stored region is
            # itself a product of an earlier backfill, so trusting it here just
            # preserves old mistakes: rows re-resolved to Albania kept the
            # "South Asia" left behind when they had resolved to Afghanistan.
            new_country, new_region = normalize_geo(opp.location or "", "")
            if not new_country:
                # Keep any region the location did yield — "EU 27" names no
                # country but does place the row in Europe.
                new_country, fallback_region = normalize_geo(opp.country or "", "")
                new_region = fallback_region or new_region
            # Nothing could be derived — leave whatever was already there.
            new_region = new_region or (opp.region or "").strip()
            if new_country != (opp.country or "") or new_region != (opp.region or ""):
                opp.country = new_country
                opp.region = new_region
                updated += 1
    if updated:
        log.info("Geography backfill: cleaned %s rows", updated)
    return updated
