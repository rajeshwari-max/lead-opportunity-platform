"""Application configuration (12-factor: overridable via LOP_* environment variables)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/


class Settings(BaseSettings):
    """Central settings object, injected wherever configuration is needed."""

    app_name: str = "Lead Opportunity Automation Platform"
    api_prefix: str = "/api"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'opportunities.db'}"
    log_dir: Path = BASE_DIR / "logs"

    # Scraping behaviour
    request_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 2.0          # exponential backoff base (seconds)
    concurrency_per_source: int = 4     # concurrent page fetches per website
    rate_limit_delay: float = 1.0       # polite delay between requests (seconds)
    max_pages_safety_cap: int = 2000    # hard stop against infinite pagination loops
    stale_page_streak: int = 3          # stop a source after N consecutive pages with nothing new
                                        # (listings are newest-first; deeper pages are only older)
    # Full browser identity — some sites (e.g. NGOBOX) serve stale cached pages
    # to clients that don't look like a real browser.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # Categories offered in filters/team routing. Removed here -> classified as "Other".
    enabled_categories: list[str] = [
        "Grant", "RFP", "Tender", "Proposal",
    ]
    # Baseline filter options always shown, merged with values found in scraped data.
    default_countries: list[str] = [
        "India", "Bangladesh", "Nepal", "Kenya", "Global",
    ]
    default_regions: list[str] = [
        "Asia", "South Asia", "East Asia", "Africa", "Europe",
        "Latin America", "Middle East", "Global",
    ]

    # DevelopmentAid membership (optional). When set, the scraper logs in and
    # unlocks real deadlines + the experts search counters.
    devaid_email: str = ""
    devaid_password: str = ""

    # Email (SMTP) — set these to enable sending. For Gmail use an App Password.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""          # e.g. yourname@gmail.com
    smtp_password: str = ""      # Gmail App Password (16 chars)
    smtp_from_name: str = "Lead Opportunity Platform"

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # When true (set via LOP_READ_ONLY=true), scrape/schedule endpoints are
    # disabled. Used for the free cloud mirror: it has no DevelopmentAid login
    # session and no persistent disk, so it only ever displays a data snapshot
    # pushed from the primary machine — it must never attempt to scrape itself.
    read_only: bool = False

    model_config = SettingsConfigDict(env_prefix="LOP_", env_file=".env", extra="ignore")


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
