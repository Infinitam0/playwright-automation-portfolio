"""Browser fingerprint rotation per context, to reduce bot-detection false positives.

Varies viewport, colour scheme and device scale factor; locale and timezone
come from site_profile (one locale by default).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from src.scraper import site_profile as P

# The User-Agent is NOT randomized here: browser.py derives it from the running
# Chromium so it always matches the browser's real version and platform.

# Common desktop viewport resolutions
_VIEWPORTS = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1680, 1050),
    (1920, 1080),
    (1920, 1200),
    (2048, 1152),
    (2560, 1080),
    (2560, 1440),
    (1280, 800),
]

# Locales matching the target portal's market
_LOCALES = list(P.BROWSER_LOCALES)


@dataclass(frozen=True)
class FingerprintProfile:
    """An immutable browser fingerprint configuration."""

    viewport_width: int
    viewport_height: int
    locale: str
    timezone_id: str
    color_scheme: str
    device_scale_factor: float


DEFAULT_FINGERPRINT = FingerprintProfile(
    viewport_width=1280,
    viewport_height=900,
    locale=P.BROWSER_LOCALES[0],
    timezone_id=P.TIMEZONE_ID,
    color_scheme="light",
    device_scale_factor=1.0,
)


class FingerprintManager:
    """Generates randomized browser fingerprint profiles."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def generate(self) -> FingerprintProfile:
        """Generate a fingerprint profile.

        When disabled, returns the exact current hardcoded values
        for zero behavior change.
        """
        if not self.enabled:
            return DEFAULT_FINGERPRINT

        vw, vh = random.choice(_VIEWPORTS)
        locale = random.choice(_LOCALES)
        # 80/20 light/dark weighting
        color_scheme = random.choices(["light", "dark"], weights=[80, 20], k=1)[0]
        # Scale factor: mostly 1.0, sometimes 1.25 or 1.5
        scale = random.choices([1.0, 1.25, 1.5], weights=[70, 20, 10], k=1)[0]

        return FingerprintProfile(
            viewport_width=vw,
            viewport_height=vh,
            locale=locale,
            timezone_id=P.TIMEZONE_ID,
            color_scheme=color_scheme,
            device_scale_factor=scale,
        )
