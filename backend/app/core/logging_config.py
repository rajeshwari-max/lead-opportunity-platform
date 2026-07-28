"""Structured logging: scraper.log, errors.log, performance.log."""
import logging
from logging.handlers import RotatingFileHandler

from app.core.config import settings

_FMT = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")


def _file_handler(filename: str, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        settings.log_dir / filename, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(_FMT)
    handler.setLevel(level)
    return handler


def setup_logging() -> None:
    """Idempotent logging bootstrap, called once at app startup."""
    root = logging.getLogger()
    if getattr(root, "_lop_configured", False):
        return
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(_FMT)
    root.addHandler(console)

    logging.getLogger("scraper").addHandler(_file_handler("scraper.log", logging.INFO))
    root.addHandler(_file_handler("errors.log", logging.ERROR))
    logging.getLogger("performance").addHandler(_file_handler("performance.log", logging.INFO))
    root._lop_configured = True  # type: ignore[attr-defined]
