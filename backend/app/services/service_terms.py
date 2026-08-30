"""Words that say what the WORK is, not what SECTOR it is in.

The finding
-----------
`_merge_team_keywords()` folds the BD spreadsheet's per-vertical term lists
into the sector classifier. The spreadsheet's Health row is:

    Climate & Health, Digital Health, Evaluation, Health care management,
    Health Consulting, Health Systems strengthening, Maternal & Child Health,
    Primary Health, Research, Training & Capacity Building

Most of those name a sector. **Research**, **Evaluation** and **Training &
Capacity Building** do not — they name the kind of engagement. The sheet is a
list of what each vertical's people SEARCH for, which reasonably includes their
own service lines. Folded into a classifier that answers "which sector is this
opportunity in", those words tag everything, because nearly every listing on
this platform is a research, evaluation or training assignment.

Measured on 4,000 recent rows:

    Health    \bResearch\b     sole reason for 114 of 738 Health tags
    Health    \bEvaluation\b   sole reason for  31
    E4C       consult(ing|ancy) sole reason for  59

Those 114 include "IEAC Audience Research — Western Balkans 2026" and "Market
Research and Business Development Consultancy Services", filed under **Health**
on the word "Research" alone.

Why removing them is safe rather than a taste call
--------------------------------------------------
This platform already has axes for the kind of work: `work_type` (Research vs
Implementation) and `study_type` (Baseline / Endline / Data Collection). The
information is not lost by taking it out of the sector classifier — it is moved
to the column that already exists for it, and stops contaminating a different
question.

The E4C exemption
-----------------
E4C(Evidence for Change) is described as "Research and Community Engagement".
For that vertical, research IS the sector — stripping these terms from it would
gut the one vertical they legitimately define. So the rule is applied to every
vertical EXCEPT the one whose identity is that service line.

This also means E4C covering ~34% of the database may be correct rather than
broken: if most of what the platform collects is research and evaluation work,
a research vertical should be large. That is a question about the business, and
it is not answered here.
"""
from __future__ import annotations

import re

# The vertical whose identity IS research/evaluation work. Spelled out rather
# than imported from services/verticals: that module imports THIS one while
# building its pattern table, so importing back would be circular. The constant
# is asserted against the real one in tests/test_service_terms.py, so the two
# cannot drift apart silently.
VERTICAL_E4C = "E4C(Evidence for Change)"

# Terms describing the ENGAGEMENT rather than the sector. Matched against the
# spreadsheet's terms and the hand-written patterns, case-insensitively, as
# whole entries or as a clear substring of one ("Research & Innovation").
#
# Each entry is here because it fires across every sector, not because it is
# vague. "Digital Health" is broad too, and stays — it names a sector.
SERVICE_LINE_TERMS: frozenset[str] = frozenset({
    "research",
    "research & innovation",
    "research and innovation",
    "evaluation",
    "monitoring & evaluation",
    "monitoring and evaluation",
    "training & capacity building",
    "training and capacity building",
    "capacity building",
    "capacity strengthening",
    "consulting",
    "consultancy",
    "project management",
    "organizational development",
    "organisational development",
    "statistics and data analysis",
    "fundraising & grant management",
    "fundraising and grant management",
    "grant management",
})

# The vertical each service line legitimately defines. Its identity IS that
# kind of work, so the terms are kept there.
OWNED_BY: dict[str, str] = {term: VERTICAL_E4C for term in SERVICE_LINE_TERMS}


def is_service_line(term: str, vertical: str) -> bool:
    """Should this term be dropped from this vertical's patterns?

    False for the vertical that owns the term, so E4C keeps "research" while
    Health loses it.
    """
    t = (term or "").strip().lower()
    if not t:
        return False
    if t not in SERVICE_LINE_TERMS:
        return False
    return OWNED_BY.get(t) != vertical


def strip_service_lines(vertical: str, terms) -> tuple[list[str], list[str]]:
    """(kept, dropped) for one vertical's spreadsheet terms."""
    kept, dropped = [], []
    for term in terms:
        (dropped if is_service_line(term, vertical) else kept).append(term)
    return kept, dropped


# --------------------------------------------------- hand-written patterns

# The hand-written regexes are not plain terms, so they are matched by the
# pattern text itself. Listed explicitly rather than inferred: guessing which
# regex "means research" from its source would eventually drop a pattern
# somebody relied on, silently.
SERVICE_LINE_PATTERNS: dict[str, frozenset[str]] = {
    "Health": frozenset({
        r"\bEvaluation\b",
        r"\bResearch\b",
        r"\bTraining\s+\&\s+Capacity\s+Building\b",
    }),
    "Livelihood": frozenset({
        r"\bResearch\s+\&\s+Innovation\b",
        r"\bTraining\s+\&\s+Capacity\s+Building\b",
        r"\bMonitoring\s+\&\s+Evaluation\b",
        r"\bProject\s+Management\b",
        r"\bOrganizational\s+Development\b",
        r"\bStatistics\s+and\s+Data\s+Analysis\b",
        r"\bFundraising\s+\&\s+Grant\s+Management\b",
        r"\bResearch\b",
        r"\bEvaluation\b",
    }),
    "Climate/Sustainability(ESG)": frozenset({
        r"\bResearch\b",
        r"\bEvaluation\b",
        r"\bTraining\s+\&\s+Capacity\s+Building\b",
    }),
    "Worker Wellbeing": frozenset({
        r"\bResearch\b",
        r"\bEvaluation\b",
    }),
    "Innovative Finance": frozenset({
        r"\bResearch\b",
        r"\bEvaluation\b",
        r"\bGrant\s+Management\b",
    }),
}


def owned_elsewhere(term: str, vertical: str, handwritten: dict) -> str:
    """Which OTHER vertical already claims this concept, if any.

    The comment on the Livelihood keyword block says its spreadsheet row was
    cleaned up — "Terms in that row that belong to another vertical are routed
    there instead (M&E/Research -> E4C, Environment & Climate -> Climate, WASH
    -> Health, HR & Employment -> Worker Wellbeing)". That was done to the
    hand-written list. `_merge_team_keywords()` then folds the untouched sheet
    back in and re-adds every one of them, so the intent is undone three
    functions below where it is described.

    Measured on the sheet's Livelihood row, twelve of its eighteen terms are
    already matched by another vertical's own patterns:

        Education, Monitoring & Evaluation, Organizational Development,
        Research & Innovation, Statistics and Data Analysis,
        Training & Capacity Building        -> E4C
        Energy, Environment & Climate       -> Climate
        Fundraising & Grant Management,
        Macro-Economy & Public Finance      -> Innovative Finance
        HR & Employment                     -> Worker Wellbeing
        Sanitation & Hygiene                -> Health

    Dropping those from Livelihood loses no recall at all: the row still gets
    tagged, by the vertical that actually owns the concept. What it stops is
    Livelihood being credited for every energy, education and evaluation
    listing on the platform — `\\bEnergy\\b` alone was the sole reason for 45
    Livelihood tags, including "Supply of Energy-Dispersive X-ray Fluorescence
    Spectrometer".

    Compared against the HAND-WRITTEN patterns only, never the merged ones.
    Merged sets would make the answer depend on which vertical was built first,
    and a rule whose result changes with dictionary order is not a rule.
    """
    import re as _re

    for other, patterns in handwritten.items():
        if other == vertical:
            continue
        for pat in patterns:
            try:
                if _re.search(pat, term, _re.IGNORECASE):
                    return other
            except _re.error:
                continue
    return ""


def pattern_is_service_line(vertical: str, pattern: str) -> bool:
    """Is this compiled pattern a service line for this vertical?

    Compared case-insensitively on the pattern SOURCE, because the merge step
    generates `\\bResearch\\b` from the sheet while a hand-written rule may
    spell the same thing `\\bresearch\\b`.
    """
    if vertical == VERTICAL_E4C:
        return False
    wanted = SERVICE_LINE_PATTERNS.get(vertical, frozenset())
    return any(pattern.casefold() == p.casefold() for p in wanted)


def dedupe_patterns(patterns) -> list[str]:
    """Collapse patterns that differ only by case.

    The merge produces `\\bEnergy\\b` from the sheet while a hand-written rule
    already had `\\benergy\\b`; both compile with IGNORECASE, so the pair is one
    rule evaluated twice. Harmless for correctness and misleading in the audit,
    where the same rule appears as two separate rows with identical counts.
    """
    seen: dict[str, str] = {}
    for p in patterns:
        key = p.casefold()
        if key not in seen:
            seen[key] = p
    return list(seen.values())
