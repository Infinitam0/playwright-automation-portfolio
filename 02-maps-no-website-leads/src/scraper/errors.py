"""Signals shared across the scraper."""


class ChallengeDetected(Exception):
    """Google served a challenge / block page. Never retry — stop and back off."""
