"""Email automation settings, changeable from the dashboard.

The digest hour, the reminder offsets and the send-on-scrape switch all live in
`.env` as defaults, but nobody should have to edit a file and restart a server
to move a send time. This keeps the live values in a small JSON file next to the
scrape schedule state, seeded from the environment on first run.

Precedence: the JSON file wins once it exists, because it represents a decision
someone made in the UI. The environment only supplies the initial values.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import BASE_DIR, settings

log = logging.getLogger("scraper")

_STATE_FILE = Path(BASE_DIR) / "data" / "email_settings.json"


class EmailSettings(BaseModel):
    """What the dashboard can change about automatic sending."""

    # Master switch for the daily run.
    digest_enabled: bool = True
    digest_hour: int = Field(default=9, ge=0, le=23)
    digest_minute: int = Field(default=0, ge=0, le=59)

    # Days-before-deadline at which a reminder goes out. Descending order is
    # enforced on save so the earliest warning is always sent first.
    reminder_days: list[int] = Field(default_factory=lambda: [10, 7, 2])

    # Email newly-scraped matches as soon as a scrape finishes, rather than
    # waiting for the next daily run. Only members with auto_send are included.
    send_on_scrape: bool = True

    def normalised(self) -> "EmailSettings":
        days = sorted({int(d) for d in self.reminder_days if 0 < int(d) <= 90}, reverse=True)
        return self.model_copy(update={"reminder_days": days or [10, 7, 2]})


def _defaults() -> EmailSettings:
    return EmailSettings(
        digest_enabled=settings.digest_enabled,
        digest_hour=settings.digest_hour,
        digest_minute=settings.digest_minute,
    )


def load() -> EmailSettings:
    """Current settings — file if present, otherwise environment defaults."""
    if not _STATE_FILE.exists():
        return _defaults()
    try:
        return EmailSettings(**json.loads(_STATE_FILE.read_text(encoding="utf-8"))).normalised()
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        log.exception("Could not read email settings — falling back to defaults")
        return _defaults()


def save(new: EmailSettings) -> EmailSettings:
    """Persist and return the normalised settings."""
    clean = new.normalised()
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(clean.model_dump_json(indent=2), encoding="utf-8")
    except OSError:
        log.exception("Could not persist email settings")
    return clean
