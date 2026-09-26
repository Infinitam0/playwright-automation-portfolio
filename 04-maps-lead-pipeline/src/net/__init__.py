"""Shared networking: polite HTTP client, robots.txt compliance, rate limiting."""

from __future__ import annotations

from urllib.parse import urlsplit


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"
