"""Deadline Engine — normalize messy free-text dates to datetime.date.

Handles: '31 July 2026', '31/07/2026', 'Jul 31 2026', 'July 31st, 2026',
'Apply by: 15 Jul 2026', '2026-07-31', 'Last date - 31.07.2026', etc.
"""
from __future__ import annotations

import re
from datetime import date

from dateutil import parser as du_parser

_ORDINALS = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)
_LABELS = re.compile(
    r"(deadline|apply by|last date|closing date|closes?( on)?|due( date| by)?|"
    r"submission( date)?|apply before|on or before)\s*[:\-–]?\s*",
    re.IGNORECASE,
)
_ONGOING = re.compile(
    r"\b(ongoing|rolling(\s+basis)?|open[\s-]?ended|no\s+(fixed\s+)?deadline|"
    r"always\s+open|until\s+filled|continuous)\b",
    re.IGNORECASE,
)
# Anchored, four-digit year first, and only a time/zone may follow. This is
# what makes "2026-01-09" different from "09-01-2026": the second is genuinely
# ambiguous and must keep going through the source's convention.
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s][\d:.+\-Zz]*)?$")

_DATE_CANDIDATE = re.compile(
    r"(\d{1,2}[\s./\-]\w{3,9}[\s./\-,]+\d{2,4})"     # 31 July 2026 / 31-Jul-26
    r"|(\w{3,9}[\s./\-]\d{1,2}(st|nd|rd|th)?[\s,./\-]+\d{2,4})"  # July 31, 2026
    r"|(\d{4}-\d{2}-\d{2})"                            # ISO
    r"|(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})",           # 31/07/2026
    re.IGNORECASE,
)


class DeadlineParser:
    """Stateless normalizer. dayfirst=True suits Indian/EU sources (31/07/2026)."""

    def parse(self, raw: str, dayfirst: bool = True) -> date | None:
        """dayfirst=True suits Indian/EU sources (31/07/2026); US sources
        (GrantWatch: 09/18/26 = Sep 18) pass dayfirst=False.

        ISO input ignores `dayfirst` entirely — see `_iso` below. That is not a
        convenience; without it, `dayfirst=True` INVERTS ISO dates.
        """
        if not raw or not raw.strip():
            return None

        # YYYY-MM-DD is unambiguous by definition, and dateutil does not treat
        # it that way:
        #
        #     du_parser.parse("2026-01-09", dayfirst=True).date()  ->  2026-09-01
        #
        # `dayfirst` is applied to the last two components whatever the shape,
        # so an ISO date whose day is 12 or under comes back with month and day
        # swapped. The pipeline default is dayfirst=True and DevelopmentAid —
        # the largest source, and one that returns ISO from its API — never
        # sets it. Every one of its deadlines where both parts were <= 12 has
        # been silently eight months out.
        #
        # This is the exact signature the brief flagged: clusters at
        # 2026-01-09, 2026-02-09, 2026-03-09 are ISO dates 2026-09-01,
        # 2026-09-02 and 2026-09-03 read backwards — the day pinned at 09
        # because it is really the month.
        #
        # Detected on shape rather than left to a per-source declaration: a
        # convention someone has to remember to set is a convention that will
        # be missed, and it already was.
        # Labels come off FIRST. "Deadline: 2026-09-01" is an ISO date with a
        # prefix, and checking before stripping sent it down the ambiguous path
        # to be inverted — the bug this guard exists to prevent, reintroduced by
        # the order of two lines.
        text = _LABELS.sub("", raw.strip())
        text = _ORDINALS.sub(r"\1", text)          # July 31st -> July 31
        text = text.replace(",", " ").strip()

        iso = self._iso(text)
        if iso is not None:
            return iso

        parsed = self._try(text, dayfirst)
        if parsed:
            return parsed
        match = _DATE_CANDIDATE.search(text)
        if match:
            return self._try(match.group(0), dayfirst)
        return None

    @staticmethod
    def _iso(raw: str) -> date | None:
        """A leading YYYY-MM-DD, read as itself. None when the text is not that.

        Deliberately strict: anchored at the start, four-digit year first, and
        the rest of the string may only be a time or timezone. "31-07-2026" and
        "07/31/2026" are not ISO and must go through the normal path, where the
        source's convention decides.
        """
        m = _ISO_DATE.match((raw or "").strip())
        if not m:
            return None
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            # 2026-13-45 is ISO-shaped and not a date. Fall through to the
            # normal parser rather than pretending this is unparseable.
            return None

    @staticmethod
    def _try(candidate: str, dayfirst: bool = True) -> date | None:
        try:
            return du_parser.parse(candidate, dayfirst=dayfirst, fuzzy=True).date()
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def is_ongoing(raw: str) -> bool:
        """'Ongoing' / 'rolling basis' / 'no deadline' — active without a date."""
        return bool(raw and _ONGOING.search(raw))

    def is_active(self, deadline: date | None, today: date | None = None) -> bool:
        """Active == deadline >= today. Unparseable deadlines are NOT active
        (requirement: only collect verifiably current opportunities) — except
        explicit ongoing markers, handled by the caller via is_ongoing()."""
        if deadline is None:
            return False
        return deadline >= (today or date.today())
