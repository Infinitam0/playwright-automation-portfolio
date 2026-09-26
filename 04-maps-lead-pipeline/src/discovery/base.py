"""Pluggable discovery interface.

The pipeline builds `SearchTask`s (one per vertical x city) from config and
feeds each to every enabled `DiscoverySource`. Sources are blind to each other;
dedup/merge happens downstream in the DB.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from ..models import RawCandidate


class SearchTask(BaseModel):
    query: str  # e.g. "plumber Springfield"
    vertical: str  # a key from config/verticals.yml, or "generic"
    city: str = ""
    province: str = ""


class DiscoverySource(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, task: SearchTask) -> list[RawCandidate]:
        """Return candidate companies for one search task."""

    async def aclose(self) -> None:  # optional cleanup
        return None
