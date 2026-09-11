"""Tests for reply formatting and map links."""

from __future__ import annotations

from droppoint.config import OptimiserConfig
from droppoint.formatting import format_result, google_maps_link
from droppoint.onemap import LatLng
from droppoint.optimiser import OptimiseResult, rank_candidates, score_candidate

A = LatLng(1.30, 103.80)
B = LatLng(1.35, 103.90)
C = LatLng(1.32, 103.88)


def test_google_maps_link_has_waypoint_and_driving():
    link = google_maps_link(A, B, LatLng(1.34, 103.88))
    assert link.startswith("https://www.google.com/maps/dir/?api=1")
    assert "origin=1.3,103.8" in link
    assert "destination=1.35,103.9" in link
    assert "waypoints=1.34,103.88" in link
    assert "travelmode=driving" in link


def test_format_feasible_result():
    config = OptimiserConfig()
    best = score_candidate(
        name="Bishan MRT",
        lat=1.34,
        lng=103.88,
        lines=["NS", "CC"],
        base_d_min=20,
        base_r_min=90,
        drive_ap_min=10,
        drive_pb_min=17,
        pt_pc_min=48,
        config=config,
    )
    result = rank_candidates([best], 20, 90, config)
    text = format_result(result, origin=A, driver_dest=B, rider_dest=C)
    assert "Best drop-off: Bishan MRT (NS / CC)" in text
    assert "Saved together: 25 min" in text
    assert "Map: https://www.google.com/maps/dir/" in text


def test_format_travel_separately_with_near_miss():
    config = OptimiserConfig(show_near_miss=True)
    # net > 0 but unfair -> ineligible, surfaces as a near-miss.
    unfair = score_candidate(
        name="Unfair MRT",
        lat=1.33,
        lng=103.85,
        base_d_min=20,
        base_r_min=90,
        drive_ap_min=10,
        drive_pb_min=18,
        pt_pc_min=70,
        config=config,
    )
    result = rank_candidates([unfair], 20, 90, config)
    assert isinstance(result, OptimiseResult)
    text = format_result(result, origin=A, driver_dest=B, rider_dest=C)
    assert "Travelling separately is best here." in text
    assert "You drive:  20 min" in text
    assert "Closest near-miss: Unfair MRT" in text
