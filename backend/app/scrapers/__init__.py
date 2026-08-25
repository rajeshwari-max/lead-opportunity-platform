"""Importing this package registers all scraper plugins.

Every bespoke scraper is active. IndevJobs, Paul Hamlyn, Packard, Open Society
and Blue Action Fund were previously commented out and so never ran — they are
re-enabled here, which is why those names showed zero results despite having
working scrapers.
"""
from app.scrapers.bond import BondScraper
from app.scrapers.adb import AdbTendersScraper
from app.scrapers.developmentaid import DevelopmentAidScraper
from app.scrapers.devnet import DevNetScraper
from app.scrapers.funders_misc import (
    BlueActionFundScraper,
    OpenSocietyScraper,
    PackardScraper,
)
from app.scrapers.fundsforngos import FundsForNGOsScraper
from app.scrapers.grantwatch import GrantWatchScraper
from app.scrapers.indevjobs import IndevJobsScraper
from app.scrapers.ngobox import NGOBoxScraper
from app.scrapers.phf import PHFScraper
from app.scrapers.unpp import UNPartnerPortalScraper
from app.scrapers.worldbank import WorldBankScraper
from app.scrapers.registry import SCRAPER_REGISTRY, get_scrapers

# Config-driven funder sites (backend/app/scrapers/sources.json). Imported last
# so a bespoke scraper always wins if both define the same name.
#
# That comment used to be wrong in a way that mattered. register() is a plain
# `SCRAPER_REGISTRY[cls.name] = cls`, so importing the config sources LAST meant
# the generated class OVERWROTE the bespoke one of the same name — the exact
# opposite of what is written above. generic_listing._build() now skips a name
# that is already registered and logs it, so the order here means what it says.
from app.scrapers import generic_listing  # noqa: E402,F401

__all__ = [
    "SCRAPER_REGISTRY", "get_scrapers", "NGOBoxScraper", "DevNetScraper",
    "FundsForNGOsScraper", "BondScraper", "DevelopmentAidScraper", "GrantWatchScraper",
    "IndevJobsScraper", "PHFScraper", "PackardScraper", "OpenSocietyScraper",
    "BlueActionFundScraper", "UNPartnerPortalScraper", "WorldBankScraper",
    "generic_listing",
]
