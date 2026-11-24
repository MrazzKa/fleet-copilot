"""Small geo helpers for real-farm scenarios (equirectangular projection, fine at farm scale).

Grid convention (same as world.py): x grows east, y grows SOUTH (y=0 is the north row).
A scenario's optional "geo" block is {"origin": [lat, lon], "cell_m": meters}, where origin is
the south-west corner of the grid. Grid coordinates may be floats (cell centers are at x+0.5).
"""
from __future__ import annotations

import math

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQ = 111320.0


def _k_lon(origin_lat: float) -> float:
    return M_PER_DEG_LON_EQ * math.cos(math.radians(origin_lat))


def ll_to_m(lat: float, lon: float, origin) -> tuple[float, float]:
    """lat/lon -> (east, north) meters from origin."""
    return (lon - origin[1]) * _k_lon(origin[0]), (lat - origin[0]) * M_PER_DEG_LAT


def m_to_ll(e: float, n: float, origin) -> tuple[float, float]:
    return origin[0] + n / M_PER_DEG_LAT, origin[1] + e / _k_lon(origin[0])


def m_to_cell(e: float, n: float, cell_m: float, height: int) -> tuple[int, int]:
    return int(math.floor(e / cell_m)), height - 1 - int(math.floor(n / cell_m))


def ll_to_cell(lat: float, lon: float, geo: dict, height: int) -> tuple[int, int]:
    e, n = ll_to_m(lat, lon, geo["origin"])
    return m_to_cell(e, n, geo["cell_m"], height)


def cell_to_ll(x: float, y: float, geo: dict, height: int) -> tuple[float, float]:
    """Center of grid cell (x, y); floats allowed (interpolated positions)."""
    c = geo["cell_m"]
    return m_to_ll((x + 0.5) * c, (height - y - 0.5) * c, geo["origin"])


def point_in_ring(lat: float, lon: float, ring) -> bool:
    """Ray casting; ring is a list of [lat, lon]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def rasterize(ring, geo: dict, width: int, height: int) -> list[tuple[int, int]]:
    """Grid cells whose center lies inside the polygon ring ([lat, lon] points)."""
    out = []
    for y in range(height):
        for x in range(width):
            lat, lon = cell_to_ll(x, y, geo, height)
            if point_in_ring(lat, lon, ring):
                out.append((x, y))
    return out


def ring_area_ha(ring, origin) -> float:
    pts = [ll_to_m(lat, lon, origin) for lat, lon in ring]
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2 / 1e4
