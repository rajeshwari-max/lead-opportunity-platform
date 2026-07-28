"""Scraper plugin registry — new scrapers self-register via the @register decorator."""
from __future__ import annotations

from app.scrapers.base_scraper import BaseScraper

SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {}


def register(cls: type[BaseScraper]) -> type[BaseScraper]:
    if not cls.name or cls.name == "base":
        raise ValueError(f"{cls.__name__} must define a unique 'name'")
    SCRAPER_REGISTRY[cls.name] = cls
    return cls


def get_scrapers(names: list[str] | None = None) -> list[BaseScraper]:
    """Instantiate requested scrapers (all registered ones when names is empty)."""
    selected = names or list(SCRAPER_REGISTRY)
    unknown = set(selected) - set(SCRAPER_REGISTRY)
    if unknown:
        raise KeyError(f"Unknown scraper(s): {', '.join(sorted(unknown))}")
    return [SCRAPER_REGISTRY[n]() for n in selected]
