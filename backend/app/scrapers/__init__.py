"""Importing this package registers all scraper plugins.

Disabled sources (removed from the dashboard on request — re-enable by
restoring the import):
  - IndevJobs            (app.scrapers.indevjobs)
  - Paul Hamlyn Foundation (app.scrapers.phf)
  - Packard Foundation / Open Society / Blue Action Fund (app.scrapers.funders_misc)
"""
from app.scrapers.bond import BondScraper
from app.scrapers.developmentaid import DevelopmentAidScraper
from app.scrapers.devnet import DevNetScraper
from app.scrapers.fundsforngos import FundsForNGOsScraper
from app.scrapers.grantwatch import GrantWatchScraper
from app.scrapers.ngobox import NGOBoxScraper
from app.scrapers.registry import SCRAPER_REGISTRY, get_scrapers

__all__ = [
    "SCRAPER_REGISTRY", "get_scrapers", "NGOBoxScraper", "DevNetScraper",
    "FundsForNGOsScraper", "BondScraper", "DevelopmentAidScraper", "GrantWatchScraper",
]
