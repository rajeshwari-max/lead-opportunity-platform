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
        (GrantWatch: 09/18/26 = Sep 18) pass dayfirst=False."""
        if not raw or not raw.strip():
            return None
        text = _LABELS.sub("", raw.strip())
        text = _ORDINALS.sub(r"\1", text)          # July 31st -> July 31
        text = text.replace(",", " ").strip()

        parsed = self._try(text, dayfirst)
        if parsed:
            return parsed
        match = _DATE_CANDIDATE.search(text)
        if match:
            return self._try(match.group(0), dayfirst)
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
