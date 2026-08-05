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
    max_concurrent_sources: int = 6     # websites scraped at the same time. With ~80
                                        # sources, running them all at once saturated
                                        # the event loop and froze the progress API.
    rate_limit_delay: float = 1.0       # polite delay between requests (seconds)
    max_pages_safety_cap: int = 2000    # hard stop against infinite pagination loops
    stale_page_streak: int = 3          # stop a source after N consecutive pages with nothing new
                                        # (listings are newest-first; deeper pages are only older)
    detail_fetch_limit: int = 1500      # max detail-page visits per source per run, for scrapers
                                        # with enrich_details=True. Caps the extra load: only rows
                                        # missing an amount/organisation are fetched at all.
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
    # Baseline filter options always shown, merged with values found in scraped
    # data. Countries only — "Global" is a scope, not a country, and belongs in
    # default_regions (listing it here put a region into the country filter).
    default_countries: list[str] = [
        "India", "Bangladesh", "Nepal", "Kenya",
    ]
    # Must match the canonical buckets in services/geography.py, otherwise the
    # sidebar offers region options that can never match a row.
    default_regions: list[str] = [
        "Africa", "South Asia", "East Asia", "Southeast Asia", "Central Asia",
        "Europe", "Middle East", "Latin America", "North America", "Oceania",
        "Global",
    ]

    # DevelopmentAid membership (optional). When set, the scraper logs in and
    # unlocks real deadlines + the experts search counters.
    devaid_email: str = ""
    devaid_password: str = ""
    # Which DevelopmentAid sections to walk, comma separated: "grants,tenders".
    # Both stay one source in the dashboard — this only bounds how much of the
    # archive a run covers. Tenders is ~1.2M listings (~5 hours) versus ~118k
    # for grants (~30 min), so it's useful to run grants alone.
    devaid_sections: str = "grants,tenders"
    # DevelopmentAid limits how deep a single search can be paged (~100 records),
    # so coverage comes from running many narrowed searches and merging them.
    # This caps how many of those slices one run performs.
    # 118k grants at ~100 readable per search needs >1,200 searches at an absolute
    # minimum, and far more in practice because the partitions are uneven. A run
    # that stops at a few hundred covers a couple of percent, so this is set high
    # enough to finish; the real limit should be the archive running out.
    devaid_max_slices: int = 25000
    # Scrape only open/forecast DevelopmentAid listings. Their unfiltered totals
    # (118k grants, 1.2M tenders) are dominated by calls that closed years ago;
    # restricting to live ones is both what the dashboard needs and small enough
    # to cover fully within the account's per-search read limit.
    # Cover currently-open DevelopmentAid listings before the historical archive.
    # The archive is several times larger, so without this the search budget is
    # spent on closed calls and the live ones — the only actionable leads — are
    # never covered completely.
    devaid_open_first: bool = True
    # Store closed/expired listings too (status=Expired) instead of discarding
    # them. The dashboard filters to Active by default, so this changes what is
    # archived, not what is shown.
    keep_expired: bool = True

    # Email (SMTP) — set these to enable sending. For Gmail use an App Password.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""          # e.g. yourname@gmail.com
    smtp_password: str = ""      # Gmail App Password (16 chars)
    smtp_from_name: str = "Lead Scanning Platform"
    # Daily digest of new matches + deadline reminders. Kept on its own clock:
    # sending after every scrape produced an email per scrape, which is too many.
    digest_enabled: bool = True
    digest_hour: int = 9
    digest_minute: int = 0

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # When true (set via LOP_READ_ONLY=true), scrape/schedule endpoints are
    # disabled. Used for the free cloud mirror: it has no DevelopmentAid login
    # session and no persistent disk, so it only ever displays a data snapshot
    # pushed from the primary machine — it must never attempt to scrape itself.
    read_only: bool = False

    # Absolute base URL used to build one-click approval links in emails.
    # Relative links don't work in mail clients, so this must point at whatever
    # host the recipients can actually reach (your Render URL in production).
    public_base_url: str = "http://localhost:8000"

    # Where the dashboard itself lives. In production Nginx serves the UI and
    # the API from one origin, so this equals public_base_url and can be left
    # blank. In development they are different ports — the API is on :8000 and
    # Vite serves the UI on :5173 — and a "view in the dashboard" link built
    # from public_base_url lands on the API, which answers {"detail":"Not Found"}.
    dashboard_url: str = ""

    # HMAC key signing those links. Left blank here on purpose — a hard-coded
    # default would be a published key, and anyone could then mint valid
    # approval links for the whole database. Generated once on first run and
    # kept on disk (see below) so links keep working across restarts.
    approval_secret: str = ""

    model_config = SettingsConfigDict(env_prefix="LOP_", env_file=".env", extra="ignore")


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)


def _load_or_create_approval_secret() -> str:
    """Stable per-installation signing key.

    Regenerating this on every boot would silently invalidate the approval
    links in every digest already sitting in people's inboxes, so it is written
    once and re-read thereafter. LOP_APPROVAL_SECRET overrides it — which is
    what the cloud mirror needs, since it has no persistent disk and would
    otherwise mint a fresh key on each deploy.
    """
    import secrets

    key_file = BASE_DIR / "data" / ".approval_secret"
    try:
        existing = key_file.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    generated = secrets.token_urlsafe(32)
    try:
        key_file.write_text(generated)
        key_file.chmod(0o600)
    except OSError:
        pass          # read-only filesystem: fall back to a process-lifetime key
    return generated


# Falls back to the API origin, which is correct in production where Nginx
# serves both from one host.
if not settings.dashboard_url:
    settings.dashboard_url = settings.public_base_url

if not settings.approval_secret:
    settings.approval_secret = _load_or_create_approval_secret()
