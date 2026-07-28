"""Deduplication — deterministic unique_id from Title + Organization + Deadline + URL."""
from __future__ import annotations

import hashlib
import re
from datetime import date


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def make_unique_id(title: str, organization: str, deadline: date | None, url: str) -> str:
    """Stable SHA-256 fingerprint. The same opportunity scraped twice (or found on
    two pages) hashes identically, so the DB unique constraint collapses duplicates."""
    key = "|".join([
        _norm(title),
        _norm(organization),
        deadline.isoformat() if deadline else "",
        _norm(url),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
