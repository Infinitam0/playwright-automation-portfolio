"""Runtime settings via pydantic-settings + .env.

A single `Settings(BaseSettings)` with a project env prefix (`LEADS_`).
The credentials are ALSO read without the prefix so a shared `.env` works.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADS_",
        extra="ignore",
    )

    # --- Paths --------------------------------------------------------
    data_dir: Path = Path("data")
    db_path: Path = Path("data/leads.sqlite")
    config_dir: Path = Path("config")

    # --- Credentials (read WITHOUT the prefix too) --------------------
    google_maps_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_MAPS_API_KEY", "LEADS_GOOGLE_MAPS_API_KEY"),
    )
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "LEADS_ANTHROPIC_API_KEY"),
    )

    # --- Politeness / networking --------------------------------------
    # A descriptive UA with a contact URL is good crawler etiquette; set yours.
    user_agent: str = "LeadPipelineBot/1.0 (+https://example.com/bot)"
    request_timeout_seconds: float = 20.0
    per_host_delay_seconds: float = 3.0
    delay_jitter_factor: float = 0.5
    max_concurrency: int = 4
    # robots.txt for the website crawl (src/net). Independent of maps_enabled.
    respect_robots: bool = True
    # Opt-in for the Google Maps browser source, which robots.txt disallows.
    # Enabling it does NOT relax robots.txt for the website crawl.
    maps_enabled: bool = False
    # A host's robots.txt Crawl-delay is honoured up to this, and a host asking
    # for more is skipped rather than crawled faster than it asked. Without a
    # bound, one typo (`Crawl-delay: 86400`) would stall a run for a day.
    robots_max_crawl_delay_seconds: float = 60.0

    # --- Discovery: Google Places -------------------------------------
    places_max_pages: int = 3  # 20 results/page; 3 pages ~= 60 per query
    places_language: str = "nl"
    places_region: str = "nl"

    # --- Retry --------------------------------------------------------
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0

    # --- Enrichment ---------------------------------------------------
    enrich_max_pages_per_site: int = 5  # homepage + contact/about-style pages

    # --- Scoring ------------------------------------------------------
    region_priority_weight: int = 5
    tier_a_threshold: int = 62
    tier_b_threshold: int = 37

    # --- Drafting -----------------------------------------------------
    draft_enabled: bool = True
    draft_model: str = "claude-sonnet-5"
    # Min seconds between calls for a network-bound drafter (AnthropicDrafter),
    # to stay under the API rate limit. Offline template drafts are never
    # throttled. Override: LEADS_DRAFT_MIN_INTERVAL_SECONDS.
    draft_min_interval_seconds: float = 5.0

    # Sender identity stamped into the draft sign-off.
    sender_name: str = ""
    sender_role: str = ""
    sender_company: str = ""
    sender_email: str = ""
    sender_phone: str = ""

    # --- Logging ------------------------------------------------------
    log_level: str = "INFO"

    @property
    def has_anthropic_auth(self) -> bool:
        return bool(self.anthropic_api_key)

    def sender_block(self, company_name: str) -> str:
        """Sign-off + footer for a drafted email.

        Identity (name, role, email), then the reason-for-receipt and opt-out
        lines. An unset sender name stays a {{your name}} placeholder on
        purpose: the lint blocks it, so an unconfigured identity can never export.
        """
        name = self.sender_name or "{{your name}}"
        role = ", ".join(x for x in (self.sender_role, self.sender_company) if x)
        lines = [
            "Kind regards,",
            "",
            name,
            # Optional identity lines; omitted when unset.
            *(x for x in (role, self.sender_email, self.sender_phone) if x),
            "",
            f"You are receiving this email because {company_name} lists this "
            "address on its own website for business contact. Prefer not to hear "
            'from us again? Reply with "unsubscribe" and we will not email you again.',
        ]
        return "\n".join(lines)
