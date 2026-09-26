"""Geocoding and coordinate tiling.

A single Maps search returns at most ~120 results, so covering a real area means
running the same query from many map positions and deduping. Two pieces:

  geocode()   — place name -> lat/lng, via OpenStreetMap Nominatim (no API key)
  tile_grid() — a centre + radius -> the list of positions to search from

Tile size is not derived from a zoom formula. Google does not document the
effective search radius per zoom, and it interacts with the viewport, so any
formula would be a guess dressed up as arithmetic. Instead the caller starts
coarse and subdivides tiles that come back at the result cap — the data reports
its own density. See subdivide().
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_LAT = 110.574

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim requires a genuine identifying User-Agent (with contact info) and
# allows 1 req/s. Set NOMINATIM_USER_AGENT to identify yourself.
_UA = (
    os.environ.get("NOMINATIM_USER_AGENT")
    or "maps-no-website-leads/1.0 (set NOMINATIM_USER_AGENT)"
)
_last_call = 0.0


@dataclass(frozen=True)
class Tile:
    lat: float
    lng: float
    zoom: int
    size_km: float

    @property
    def label(self) -> str:
        return f"{self.lat:.4f},{self.lng:.4f}@{self.zoom}z"


def km_per_deg_lng(lat: float) -> float:
    """Longitude degrees shrink toward the poles; latitude degrees do not.

    Ignoring this makes tiles far too wide in northern Europe and leaves gaps.
    """
    return 111.320 * math.cos(math.radians(lat))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def geocode(place: str, *, timeout: float = 20.0) -> tuple[float, float]:
    """Place name -> (lat, lng) via Nominatim."""
    global _last_call

    elapsed = time.monotonic() - _last_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)  # Nominatim's published rate limit

    resp = httpx.get(
        _NOMINATIM,
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": _UA},
        timeout=timeout,
        follow_redirects=True,
    )
    _last_call = time.monotonic()
    resp.raise_for_status()

    results = resp.json()
    if not results:
        raise ValueError(f"no geocoding result for {place!r}")
    # Nominatim's key is "lon", not "lng".
    return float(results[0]["lat"]), float(results[0]["lon"])


def tile_grid(
    lat: float,
    lng: float,
    radius_km: float,
    cell_km: float = 1.5,
    zoom: int = 15,
) -> list[Tile]:
    """Square grid of search positions covering a circle of `radius_km`.

    Cells whose centre falls outside the radius are dropped, so a round area does
    not pay for the corners of its bounding box. The tolerance below keeps edge
    cells that still cover ground inside the circle.
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    if cell_km <= 0:
        raise ValueError("cell_km must be positive")

    dlat = cell_km / KM_PER_DEG_LAT
    dlng = cell_km / max(km_per_deg_lng(lat), 1e-6)
    steps = max(int(math.ceil(radius_km / cell_km)), 0)
    keep_within = radius_km + cell_km * 0.5

    tiles = [
        Tile(t_lat, t_lng, zoom, cell_km)
        for i in range(-steps, steps + 1)
        for j in range(-steps, steps + 1)
        if haversine_km(
            lat, lng, (t_lat := lat + i * dlat), (t_lng := lng + j * dlng)
        ) <= keep_within
    ]

    logger.info(
        "grid: %.1fkm radius / %.1fkm cells -> %d tiles at zoom %d",
        radius_km, cell_km, len(tiles), zoom,
    )
    return tiles


def subdivide(tile: Tile, max_depth_km: float = 0.9) -> list[Tile]:
    """Split a saturated tile into four, zoomed one step in.

    Called when a tile returns at/near the 120-result cap, which proves it had
    more to give. Returns [] once cells get small enough that splitting further
    costs more requests than it surfaces new businesses.
    """
    half = tile.size_km / 2
    if half < max_depth_km:
        logger.debug("tile %s already at minimum size, not subdividing", tile.label)
        return []

    offset = half / 2
    dlat = offset / KM_PER_DEG_LAT
    dlng = offset / max(km_per_deg_lng(tile.lat), 1e-6)

    return [
        Tile(tile.lat + i * dlat, tile.lng + j * dlng, tile.zoom + 1, half)
        for i in (-1, 1)
        for j in (-1, 1)
    ]
