from pathlib import Path
from urllib.parse import urlencode

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.scraper import site_profile as P

# Default areas - override via LISTING_AREAS env var (JSON list). Each value is
# sent as a `location` query parameter: a city, or "city/neighbourhood".
# Examples:
#   LISTING_AREAS=["example-city/north","example-city/centre","other-town"]
#   LISTING_AREAS=["example-city","other-town"]
DEFAULT_AREAS = [
    "example-city",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LISTING_",
        extra="ignore",
    )

    # Root of the listing portal (LISTING_BASE_URL).
    base_url: str = "https://listings.example.com"

    spreadsheet_id: str
    google_credentials_path: Path = Path("credentials/service_account.json")

    headless: bool = True
    scrape_details: bool = True
    max_pages: int = 10
    page_delay_seconds: float = 2.0
    detail_delay_seconds: float = 3.0

    # Search configuration
    areas: list[str] = DEFAULT_AREAS
    sort: str = P.DEFAULT_SORT
    price_min: int | None = None
    price_max: int | None = None

    # Incremental scraping: stop after this many consecutive known IDs
    consecutive_known_threshold: int = 15

    # Proxy settings
    proxy_enabled: bool = False
    proxy_source: str = "file"  # file, url, or inline
    proxy_file_path: Path = Path("proxies.txt")
    proxy_url: str = ""
    proxy_list: list[str] = []
    proxy_rotation_strategy: str = "round_robin"
    proxy_max_consecutive_failures: int = 3
    proxy_max_total_failures: int = 10
    proxy_cooldown_seconds: float = 60.0
    proxy_fallback_direct: bool = True

    # Fingerprint rotation per context. On by default: it also selects the
    # session path (see main.py `use_session`), which is where per-context proxy
    # rotation and retry live.
    fingerprint_enabled: bool = True

    # Retry / backoff
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0

    # Circuit breaker
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 120.0

    # Timing jitter
    delay_jitter_factor: float = 0.5

    # Telegram notifications
    telegram_enabled: bool = False
    telegram_dry_run: bool = False
    telegram_bot_token: str = ""
    telegram_chat_ids: list[str] = []  # JSON list of user/group chat IDs
    telegram_include_sheet_url: bool = True

    # Block alerts: a block persists across every run, so repeat alerts are
    # throttled to one per this window (state file: logs/.block_alert).
    block_alert_cooldown_hours: float = 24.0

    # Listing lifecycle tracking (speed-to-sell). Ships disabled: enable only
    # after confirming the full inventory fits within max_pages (otherwise a
    # truncated pass would misread listings as disappeared). Runs once/day, on
    # the run whose UTC hour matches lifecycle_sweep_hour_utc.
    lifecycle_sweep_enabled: bool = False
    lifecycle_sweep_hour_utc: int = 6

    # Precise sold detection. The lifecycle sweep can query the portal's sold
    # view directly (status filter) to confirm which tracked listings actually
    # sold, rather than only inferring from disappearance. Gated by the master
    # lifecycle_sweep_enabled. Values: site_profile.DEFAULT_SOLD_STATUS_FILTER.
    sold_sweep_enabled: bool = True
    sold_status_filter: list[str] = P.DEFAULT_SOLD_STATUS_FILTER
    sold_sweep_max_pages: int = 5

    def _search(self, statuses: list[str] | None = None) -> str:
        """Search URL: /search?location=..&min_price=..&max_price=..&status=..&sort=.."""
        params = [(P.LOCATION_PARAM, a) for a in self.areas]
        if self.price_min is not None:
            params.append((P.MIN_PRICE_PARAM, str(self.price_min)))
        if self.price_max is not None:
            params.append((P.MAX_PRICE_PARAM, str(self.price_max)))
        params += [(P.STATUS_PARAM, s) for s in statuses or []]
        params.append((P.SORT_PARAM, self.sort))
        return f"{self.base_url.rstrip('/')}{P.SEARCH_PATH}?{urlencode(params)}"

    @property
    def search_url(self) -> str:
        return self._search()

    def search_url_page(self, page: int) -> str:
        return f"{self.search_url}&{P.PAGE_PARAM}={page}"

    @property
    def search_url_sold(self) -> str:
        """Search URL filtered to sold / under-offer listings (the sold view)."""
        return self._search(self.sold_status_filter)

    def search_url_sold_page(self, page: int) -> str:
        return f"{self.search_url_sold}&{P.PAGE_PARAM}={page}"
