"""Tests for the station dataset loader and the geometric prefilter (no API calls)."""

from __future__ import annotations

from droppoint.onemap import LatLng
from droppoint.stations import (
    corridor_prefilter,
    cross_track_km,
    haversine_km,
    load_stations,
)

# West and east ends of the island, plus a rider destination in the east.
WEST = LatLng(1.3385, 103.7060)  # Boon Lay area
EAST = LatLng(1.3536, 103.9450)  # Tampines area
RIDER_C = LatLng(1.3240, 103.9300)  # Bedok area


def test_load_default_dataset():
    stations = load_stations()
    assert len(stations) > 40
    for st in stations:
        assert 1.2 < st.lat < 1.5
        assert 103.6 < st.lng < 104.1
        assert st.name


def test_haversine_known_distance():
    # Boon Lay to Tampines is roughly 26-27 km in a straight line.
    d = haversine_km(WEST, EAST)
    assert 24 < d < 29


def test_prefilter_caps_to_max_candidates():
    stations = load_stations()
    result = corridor_prefilter(WEST, EAST, RIDER_C, stations, max_offset_km=3.0, max_candidates=5)
    assert len(result) == 5  # plenty of corridor stations exist, so it is capped


def test_prefilter_default_cap_is_respected():
    stations = load_stations()
    result = corridor_prefilter(WEST, EAST, RIDER_C, stations, max_offset_km=3.0, max_candidates=15)
    assert 0 < len(result) <= 15


def test_prefilter_keeps_only_corridor_and_ahead():
    stations = load_stations()
    result = corridor_prefilter(WEST, EAST, RIDER_C, stations, max_offset_km=3.0, max_candidates=15)
    for st in result:
        offset_km, behind = cross_track_km(WEST, EAST, st.point)
        assert offset_km <= 3.0
        assert not behind


def test_prefilter_nearest_to_c_is_first():
    stations = load_stations()
    result = corridor_prefilter(WEST, EAST, RIDER_C, stations, max_offset_km=5.0, max_candidates=15)
    distances = [haversine_km(st.point, RIDER_C) for st in result]
    assert distances == sorted(distances)


def test_prefilter_excludes_far_offset_station():
    # A station far north of an east-west corridor must be dropped.
    far_north = next(st for st in load_stations() if st.name == "Woodlands MRT")
    result = corridor_prefilter(
        WEST, EAST, RIDER_C, load_stations(), max_offset_km=3.0, max_candidates=50
    )
    assert far_north.name not in {st.name for st in result}


def test_cross_track_detects_behind_origin():
    # A point west of WEST (further from EAST) is behind the origin.
    behind_point = LatLng(1.3375, 103.6973)  # Pioneer, west of Boon Lay
    _, behind = cross_track_km(WEST, EAST, behind_point)
    assert behind is True
