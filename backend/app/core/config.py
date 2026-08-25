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
    # How long an undated ("Ongoing") listing may go unseen by a scrape before it
    # is retired. These rows carry no deadline, so nothing else can ever close
    # them — without this they stay in the live view permanently, which is why
    # calls that closed months ago were still on the dashboard.
    #
    # 21 days, because the run history shows a roughly weekly cadence (3, 10,
    # 16-17, 24 Aug): three consecutive scrapes that looked and did not find it.
    # A retirement only happens when the source itself was demonstrably working
    # in that window (see audit_deadlines), so a broken source cannot age out
    # its own catalogue.
    ongoing_max_age_days: int = 21
    # Run the deadline/link/junk maintenance pass automatically at the end of
    # every scrape. These repairs existed but were never called by anything.
    run_maintenance_after_scrape: bool = True
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
    # Scrape as yourself, everywhere. Point these at your everyday Chrome and
    # EVERY source reuses whatever you are already signed into — UN Partner
    # Portal's /cfei/open, DevelopmentAid's unlocked deadlines, any other site
    # behind a login — with no per-site "Connect account" step and no repeated
    # logging in.
    #
    #   LOP_CHROME_USER_DATA_DIR=C:\Users\<you>\AppData\Local\Google\Chrome\User Data
    #   LOP_CHROME_PROFILE_DIR=Profile 7
    #
    # Chrome must be CLOSED while a scrape runs — it holds an exclusive lock on
    # the whole User Data folder. Local only: a server has no Chrome profile, so
    # EC2 keeps using the exported per-source sessions.
    #
    # The session is COPIED out (see site_auth.mirror_own_chrome), never driven
    # in place: Chrome 136+ refuses remote debugging against a live profile.
    chrome_user_data_dir: str = ""
    # Blank, NOT "Default". A non-empty default is truthy, so `chrome_profile_dir
    # or devaid_chrome_profile_dir` short-circuited on it and silently ignored a
    # profile set under the older name — mirroring the Default profile, which is
    # signed into nothing, while reporting success. Blank means "not set", and
    # site_auth.own_chrome_profile() supplies "Default" only as a last resort.
    chrome_profile_dir: str = ""

    # unlocks real deadlines + the experts search counters.
    devaid_email: str = ""
    devaid_password: str = ""

    # UN Partner Portal ------------------------------------------------------
    # /cfei/open is the signed-in list of open Calls for Expression of Interest.
    # Signed out, the portal shows /landing/opportunities instead, which is a
    # different and much shorter list — so a run without a session does not
    # produce "fewer rows", it produces the wrong ones.
    #
    # Two ways in, tried in this order by scrapers/unpp.py:
    #   1. these credentials, which is the only route that works on EC2;
    #   2. your everyday Chrome session (LOP_CHROME_USER_DATA_DIR /
    #      LOP_CHROME_PROFILE_DIR), which needs Chrome closed and a desktop.
    # Neither available means the scraper yields nothing and says so, rather
    # than quietly scraping the public teaser.
    unpp_email: str = ""
    unpp_password: str = ""
    # Set false to watch the sign-in happen in a visible window — the fastest
    # way to see a CAPTCHA or an SSO redirect that headless cannot get past.
    unpp_headless: bool = True
    # Scrape using YOUR everyday Chrome profile instead of the dedicated one, so
    # there is no separate "Connect account" step on your own machine — you are
    # already signed in there.
    #
    #   LOP_DEVAID_CHROME_USER_DATA_DIR=C:\Users\<you>\AppData\Local\Google\Chrome\User Data
    #   LOP_DEVAID_CHROME_PROFILE_DIR=Default
    #
    # Two hard constraints, both enforced in devaid_auth.open_persistent:
    #   1. Chrome must be FULLY CLOSED while a scrape runs. Chrome holds an
    #      exclusive lock on the profile; a second process opening it either
    #      fails or corrupts it. This is checked and refused, not attempted.
    #   2. It is local-only. A server has no Chrome profile, so EC2 keeps using
    #      the exported session — see scripts/devaid_session.py push.
    # Blank (the default) = the dedicated profile in backend/data/devaid_profile.
    devaid_chrome_user_data_dir: str = ""
    devaid_chrome_profile_dir: str = "Default"
    # Which DevelopmentAid sections to walk, comma separated: "grants,tenders".
    # Both stay one source in the dashboard — this only bounds how much of the
    # archive a run covers. Tenders is ~1.2M listings (~5 hours) versus ~118k
    # for grants (~30 min), so it's useful to run grants alone.
    devaid_sections: str = "grants,tenders"
    # The exact filtered searches the team wants scraped — copied verbatim from
    # DevelopmentAid's own address bar with the advanced filters applied.
    #
    # These are set as DEFAULTS, not left blank, because "blank" meant the
    # scraper fell back to the unfiltered /grants/search and /tenders/search:
    # devaid_filtered_search defaults to False (a long generated query string
    # once tripped Cloudflare), so the filters the team had configured were
    # never actually being sent. An explicit URL bypasses that flag entirely —
    # this is the search that runs, on every machine, with no .env editing.
    #
    # Kept verbatim rather than rebuilt from the sector settings below: the
    # generated grants list carried sector 9, which this search does not, so
    # regenerating it would silently scrape a sector the team did not ask for.
    # Change the search on the site, copy the new URL, paste it here.
    devaid_grants_url: str = (
        "https://www.developmentaid.org/grants/search"
        "?languages=92"
        "&sectors=100,7,3,95,5,6,11,54,8,78,80,30,44,87,85,22,34,48,27"
        "&statuses=3"
    )
    devaid_tenders_url: str = (
        "https://www.developmentaid.org/tenders/search"
        "?sectors=100,5,95,3,6,7,78,8,29,9,11,54,80,16,30,44,20,85,87,60,22,43,34,48,27"
        "&statuses=3"
        "&languages=92"
    )
    # DevelopmentAid search filters, from the team's own saved searches.
    # Grants and tenders carry slightly different sector lists because the two
    # catalogues don't offer identical sectors.
    #   statuses=3  -> currently open only (not forecast, not closed)
    #   languages=92 -> English
    # Blank sectors = every sector.
    devaid_tender_sectors: str = (
        "100,5,95,3,6,7,78,8,29,9,11,54,80,16,30,44,20,85,87,60,22,43,34,48,27"
    )
    devaid_grant_sectors: str = (
        "100,3,95,5,6,7,78,8,54,11,80,30,44,85,87,22,34,48,27,9"
    )
    devaid_statuses: str = "3"
    devaid_language: str = "92"
    # Whether to request the filtered search URL (sectors/statuses/languages in
    # the query string) instead of the plain /grants/search and /tenders/search.
    #
    # OFF by default, because turning it on is what broke this scraper. The
    # plain URLs returned 2,463 result cards on 30 July and filled the database;
    # every run after the filters were added came back 403 with a Cloudflare
    # "Just a moment..." interstitial. A long machine-generated query string is
    # itself a bot signal — ADB's firewall was shown to reject the same shape of
    # URL outright while serving the bare path normally.
    #
    # Filtering by sector is not lost: the pipeline already keeps only English,
    # currently-open rows and classifies each into a vertical, so the narrowing
    # happens after collection instead of in the request.
    devaid_filtered_search: bool = False
    # Run the DevelopmentAid scrape in a *visible* browser window.
    #
    # Headless Chrome reports itself as such: with channel="chrome" the browser's
    # own user agent is used (deliberately — a UA that disagrees with the real
    # browser is itself a bot signal), and in headless mode that string contains
    # "HeadlessChrome". So the scraper has been announcing what it is on every
    # request, which is a plausible reason Cloudflare now serves it a challenge
    # where it did not in July.
    #
    # Running headed is not a disguise — it is a real browser window doing real
    # browsing with the user's own session. It only works on a machine with a
    # screen, so it stays off by default for the server.
    #   true  -> visible window (desktop only, best chance of working)
    #   false -> headless (required on EC2)
    devaid_headless: bool = True
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

    # Dashboard strictness -------------------------------------------------
    # Every row must carry a link that opens the call itself. Without this a
    # linkless row was stored anyway and services/links.py handed the reader a
    # web search, so the dashboard listed entries that opened a search engine.
    require_usable_link: bool = True
    # Every row must show positive evidence that it IS an opportunity — see
    # services/opportunity_gate.py. Most sources are scraped by harvesting every
    # link on a page, so without this a funder's news post or "our grantees"
    # card is stored as a fundable call. Set false only to diagnose whether the
    # gate is what dropped something.
    strict_opportunity_gate: bool = True
    # Delete rows that have been Expired for longer than this. Closed calls are
    # archived rather than deleted so recent history stays queryable; without an
    # upper bound the archive grows forever (the database is already 176 MB).
    # 0 disables the purge and keeps everything.
    expired_purge_days: int = 90

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

    # Shared password protecting the whole dashboard. Empty = no gate, which
    # keeps local development unchanged; set it on any public instance.
    dashboard_password: str = ""

    # Email domains that may sign in with the dashboard password without an
    # admin adding them to the team list first. Comma separated, no "@".
    # Blank = closed: only people already on the team list can sign in.
    #
    # This widens *who* can sign in, not *what* they can do — the dashboard
    # password is still required, and admin still needs the admin password. So
    # it is "anyone at our company who also knows the shared password", not
    # "anyone who claims a company address".
    allowed_email_domains: str = "catalysts.org"

    # Second, higher password for the admin-only panels (scraper controls, team
    # routing, automatic email, expert pool). Everyone with the dashboard
    # password can read opportunities and approve them; only an admin can start
    # a scrape or change who receives email. Empty = anyone signed in is an
    # admin, which is the sensible default for a single-user local setup.
    admin_password: str = ""

    # HMAC key signing those links. Left blank here on purpose — a hard-coded
    # default would be a published key, and anyone could then mint valid
    # approval links for the whole database. Generated once on first run and
    # kept on disk (see below) so links keep working across restarts.
    approval_secret: str = ""

    # env_file is an absolute path on purpose. A bare ".env" is resolved against
    # the process's working directory, so whether the file is read at all depends
    # on where Supervisor happens to start gunicorn from. That makes a fully
    # populated .env look empty — no password, no SMTP — with nothing in the logs
    # to say why. Anchoring it to backend/ removes the question entirely.
    model_config = SettingsConfigDict(
        env_prefix="LOP_", env_file=BASE_DIR / ".env", extra="ignore"
    )


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
