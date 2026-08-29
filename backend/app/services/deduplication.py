"""Deduplication — deterministic unique_id from Title + Organization + Deadline + URL."""
from __future__ import annotations

import hashlib
import re
from datetime import date


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def make_unique_id(title: str, organization: str, deadline: date | None, url: str) -> str:
    """Stable SHA-256 fingerprint of an opportunity's IDENTITY.

    The deadline used to be part of this key, and that made the key unstable in
    exactly the situation where it most needed to hold. When a source corrects
    or extends a closing date, every field identifying the notice is unchanged
    but the hash moves — so the same call is stored a second time, and the
    dashboard shows it twice with two different deadlines and no way to tell
    which is current.

    A deadline is an ATTRIBUTE of an opportunity, not part of what makes it that
    opportunity. It belongs in the row, where it can be updated, not in the key,
    where changing it forks the record.

    Identity, in order of preference:

      1. The canonical detail URL. A source's own record URL is the closest
         thing to a primary key it exposes, and it survives an edited title.
      2. Title + organization, when there is no usable deep link. Weaker: it
         merges two genuinely different calls a funder gave the same name. That
         is rarer than the same call appearing twice, which is what the old key
         guaranteed every time a deadline was corrected.

    `deadline` stays in the signature and is deliberately unused. Removing the
    parameter would silently change behaviour at call sites nobody reviewed.

    Changing this function changes the key for all 106,854 existing rows, so it
    is paired with scripts/rekey_opportunities.py. Running one without the other
    makes the next scrape treat the whole database as new.
    """
    # A url is only an identity if it identifies ONE opportunity.
    #
    # This caught a real one before it shipped. DevNetJobsIndia stores
    # https://www.devnetjobsindia.org/rfp_assignments.aspx on every row it
    # produces, because that single .aspx page IS its RFP list. Keying on the
    # url alone would have merged 86 completely different RFPs — Soybean Grain
    # Analyser, GIS Agency, NABL Diagnostics, MacBook procurement — into one
    # record and archived 85 real opportunities as duplicates.
    #
    # link_kind() already knows the difference and already names this source in
    # its docstring. Anything that is not a per-opportunity page falls back to
    # title+organization, which for these rows is the only real identity they
    # have.
    from app.services.links import link_kind      # local: avoids a cycle

    link = _norm(url)
    if link and link_kind(url) == "deep":
        key = f"url|{link}"
    else:
        # Includes the source-agnostic case AND the listing-url case. Two calls
        # from one funder with byte-identical titles still merge, which is the
        # accepted cost of having no better identifier for them.
        key = "|".join(["ident", _norm(title), _norm(organization)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def legacy_unique_id(title: str, organization: str, deadline: date | None,
                     url: str) -> str:
    """The pre-2026-08-29 key. Not for new writes.

    The backfill needs to compute what a row's id WAS in order to match it, and
    a second copy of the old logic living in a script would rot.
    """
    key = "|".join([
        _norm(title),
        _norm(organization),
        deadline.isoformat() if deadline else "",
        _norm(url),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
