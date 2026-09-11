"""Candidate drop-off dataset and the geometric prefilter.

Drop-off candidates are MRT/LRT stations and major interchanges, loaded from
``data/stations.json``. The prefilter is a cheap, API-free funnel: keep stations that
lie within a corridor around the driver's A->B line and are not behind A, then keep the
ones nearest the rider's destination C.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from .onemap import LatLng

DEFAULT_STATIONS_PATH = Path(__file__).resolve().parents[2] / "data" / "stations.json"

EARTH_RADIUS_KM = 6371.0088

# Below this A->B distance the bearing is unstable; fall back to distance-from-A.
_MIN_BASELINE_KM = 0.05


class Station(BaseModel):
    """A candidate drop-off point."""

    name: str
    lat: float
    lng: float
    type: str = "MRT"
    lines: list[str] = []

    @property
    def point(self) -> LatLng:
        return LatLng(self.lat, self.lng)


def load_stations(path: Path | str | None = None) -> list[Station]:
    """Load the station dataset. Defaults to the packaged ``data/stations.json``."""
    p = Path(path) if path is not None else DEFAULT_STATIONS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    return [Station(**row) for row in data["stations"]]


@lru_cache
def default_stations() -> tuple[Station, ...]:
    """Cached load of the default dataset (immutable tuple)."""
    return tuple(load_stations())


# --- geometry -----------------------------------------------------------------


def haversine_km(a: LatLng, b: LatLng) -> float:
    """Great-circle distance between two points in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lng, b.lat, b.lng))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _initial_bearing(a: LatLng, b: LatLng) -> float:
    """Initial bearing from a to b, in radians."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lng - a.lng)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(x, y)


def cross_track_km(origin: LatLng, dest: LatLng, point: LatLng) -> tuple[float, bool]:
    """Perpendicular distance (km) from ``point`` to the origin->dest great circle.

    Returns ``(offset_km, behind)`` where ``behind`` is True when the point lies behind
    the origin relative to dest (i.e. the wrong side to be "on the way").
    """
    base = haversine_km(origin, dest)
    if base < _MIN_BASELINE_KM:
        # Degenerate line: treat distance from the origin as the offset.
        return haversine_km(origin, point), False

    d13 = haversine_km(origin, point) / EARTH_RADIUS_KM  # angular distance origin->point
    theta13 = _initial_bearing(origin, point)
    theta12 = _initial_bearing(origin, dest)

    dxt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(theta13 - theta12))))
    offset_km = abs(dxt) * EARTH_RADIUS_KM
    behind = math.cos(theta13 - theta12) < 0
    return offset_km, behind


def corridor_prefilter(
    origin: LatLng,
    driver_dest: LatLng,
    rider_dest: LatLng,
    stations: list[Station] | tuple[Station, ...],
    max_offset_km: float,
    max_candidates: int,
) -> list[Station]:
    """Cheaply narrow the station list to the most promising drop-off candidates.

    No API calls. Keep stations within ``max_offset_km`` of the A->B line and not
    behind A; then keep the ``max_candidates`` nearest the rider's destination C.
    """
    in_corridor: list[Station] = []
    for st in stations:
        offset_km, behind = cross_track_km(origin, driver_dest, st.point)
        if behind:
            continue
        if offset_km > max_offset_km:
            continue
        in_corridor.append(st)

    in_corridor.sort(key=lambda st: haversine_km(st.point, rider_dest))
    return in_corridor[:max_candidates]
