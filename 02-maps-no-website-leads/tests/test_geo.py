import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.geo import Tile, haversine_km, km_per_deg_lng, subdivide, tile_grid  # noqa: E402

UTRECHT = (52.0907, 5.1214)


def test_haversine_known_distance():
    # Utrecht -> Amsterdam is ~35km
    d = haversine_km(52.0907, 5.1214, 52.3676, 4.9041)
    assert 30 < d < 40


def test_haversine_zero():
    assert haversine_km(51.0, 5.0, 51.0, 5.0) == pytest.approx(0.0)


def test_longitude_degrees_shrink_toward_poles():
    """Ignoring this makes northern tiles too wide and leaves coverage gaps."""
    assert km_per_deg_lng(0) > km_per_deg_lng(51) > km_per_deg_lng(70)
    assert km_per_deg_lng(51) == pytest.approx(70.0, abs=2.0)


def test_grid_covers_the_radius():
    lat, lng = UTRECHT
    tiles = tile_grid(lat, lng, radius_km=3, cell_km=1.0)
    assert tiles
    # Every tile centre sits within the requested radius plus half a cell.
    for t in tiles:
        assert haversine_km(lat, lng, t.lat, t.lng) <= 3 + 0.5 + 1e-6


def test_grid_has_no_gaps():
    """Every point on a fine probe grid inside the radius is near some tile."""
    lat, lng = UTRECHT
    cell = 1.0
    tiles = tile_grid(lat, lng, radius_km=2, cell_km=cell)
    for frac_lat in (-0.9, -0.4, 0.0, 0.4, 0.9):
        for frac_lng in (-0.9, -0.4, 0.0, 0.4, 0.9):
            plat = lat + frac_lat * 2 / 110.574
            plng = lng + frac_lng * 2 / km_per_deg_lng(lat)
            if haversine_km(lat, lng, plat, plng) > 2:
                continue
            nearest = min(haversine_km(plat, plng, t.lat, t.lng) for t in tiles)
            assert nearest <= cell, f"gap at {plat},{plng}: nearest tile {nearest:.2f}km"


def test_grid_drops_corners_outside_radius():
    """A circle should not pay for the corners of its bounding box."""
    tiles = tile_grid(*UTRECHT, radius_km=5, cell_km=1.0)
    steps = 5  # ceil(5/1)
    bounding_box = (2 * steps + 1) ** 2
    assert len(tiles) < bounding_box


def test_single_tile_for_tiny_radius():
    tiles = tile_grid(*UTRECHT, radius_km=0.5, cell_km=2.0)
    assert len(tiles) >= 1


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_radius_rejected(bad):
    with pytest.raises(ValueError):
        tile_grid(*UTRECHT, radius_km=bad)


def test_invalid_cell_rejected():
    with pytest.raises(ValueError):
        tile_grid(*UTRECHT, radius_km=1, cell_km=0)


def test_subdivide_makes_four_smaller_tiles():
    t = Tile(52.0907, 5.1214, 15, 2.0)
    kids = subdivide(t)
    assert len(kids) == 4
    assert all(k.size_km == 1.0 for k in kids)
    assert all(k.zoom == 16 for k in kids)
    # Children straddle the parent centre rather than piling up on it.
    assert len({(round(k.lat, 6), round(k.lng, 6)) for k in kids}) == 4


def test_subdivision_terminates():
    """Without a floor this recurses forever and the run never finishes."""
    tiles = [Tile(52.0907, 5.1214, 15, 4.0)]
    depth = 0
    while tiles and depth < 50:
        tiles = [kid for t in tiles for kid in subdivide(t)]
        depth += 1
    assert not tiles, "subdivision never bottomed out"
    assert depth < 20
