"""A source's own notice wording, mapped to a record type the contract knows.

Why this exists
---------------
`record_is_in_scope` judges a record on `record_type` — a value from
`RecordType`, like `tender` or `contract_award`. Sources do not speak that
vocabulary. World Bank says "Contract Award", ADB says "Invitation for Bids",
and both were putting that string into the summary text and passing nothing.

So the whole contract mechanism was inert. World Bank's manifest excludes
`contract_award` and `project`; nothing ever set either, and
`record_is_in_scope` on a blank record type returns keep=True. The exclusion
was real, tested, wired into ingest, and could not fire.

The mapping rule
----------------
Closed kinds are checked FIRST. "Contract Award Notice" contains both "award"
and "notice", and a rule that reached "procurement notice" first would file an
award as an open tender — which is the exact mistake the World Bank manifest
exists to prevent.

An unrecognised string maps to `""`, never to a guess. Empty means "this source
said something we do not have a rule for", and the contract treats that as
unknown rather than as grounds to discard — a vocabulary nobody has configured
must not silently delete a source's output.
"""
from __future__ import annotations

from app.services.source_manifest import RecordType

# Order matters. Each entry is (substring, RecordType), tested in sequence, and
# the finished kinds come first for the reason in the module docstring.
_RULES: tuple[tuple[str, RecordType], ...] = (
    # ---- already decided: nobody can bid on these
    ("contract award", RecordType.CONTRACT_AWARD),
    ("award", RecordType.CONTRACT_AWARD),
    ("cancel", RecordType.CONTRACT_AWARD),
    ("annul", RecordType.CONTRACT_AWARD),
    ("abandon", RecordType.CONTRACT_AWARD),
    # ---- not an opportunity at all
    ("project information", RecordType.PROJECT),
    ("project document", RecordType.PROJECT),
    # ---- open calls, most specific first
    ("expression of interest", RecordType.EOI),
    ("expressions of interest", RecordType.EOI),
    ("request for proposal", RecordType.RFP),
    ("request for quotation", RecordType.RFQ),
    ("request for bid", RecordType.ITB),
    ("invitation for bid", RecordType.ITB),
    ("invitation to bid", RecordType.ITB),
    ("invitation for prequalification", RecordType.ITB),
    ("prequalification", RecordType.ITB),
    ("consultant", RecordType.CONSULTANCY),
    ("consulting", RecordType.CONSULTANCY),
    ("call for proposal", RecordType.CALL_FOR_PROPOSALS),
    ("grant", RecordType.GRANT),
    # ---- the generic ones, last, so a more specific phrase wins
    ("procurement notice", RecordType.TENDER),
    ("tender", RecordType.TENDER),
    ("notice", RecordType.TENDER),
)


def record_type_for(notice_type: str) -> str:
    """The RecordType value for a source's notice wording, or "" if unmapped.

    Returns the enum's *value* rather than the enum, because that is what
    `RawOpportunity.record_type` carries and what `record_is_in_scope`
    compares against.
    """
    text = (notice_type or "").strip().lower()
    if not text:
        return ""
    for needle, kind in _RULES:
        if needle in text:
            return kind.value
    return ""
