"""End-to-end optimiser orchestration tests using the swappable FakeOneMapClient.

Covers the canonical example through `optimise`, skipping a candidate that has no
route, and the infeasible case when a baseline cannot be computed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from droppoint.config import OptimiserConfig
from droppoint.onemap import LatLng
from droppoint.optimiser import optimise
from droppoint.stations import Station
from support import FakeOneMapClient

DEPART = datetime(2026, 6, 28, 9, 0, tzinfo=UTC)

A = LatLng(1.30, 103.80)  # shared start
B = LatLng(1.35, 103.90)  # driver destination
C = LatLng(1.32, 103.88)  # rider destination

# All ahead of A toward B and near the line, so the prefilter keeps them.
S1 = Station(name="Bishan MRT", lat=1.34, lng=103.88, lines=["NS", "CC"])
S2 = Station(name="Toa Payoh MRT", lat=1.32, lng=103.84, lines=["NS"])
S3 = Station(name="No Route MRT", lat=1.345, lng=103.89, lines=["XX"])

# Wide corridor so geometry never excludes our handful of test stations; the prefilter
# itself is tested in test_stations.py.
CONFIG = OptimiserConfig(corridor_max_offset_km=50.0, max_candidates_evaluated=15)


def make_client(include_baselines: bool = True) -> FakeOneMapClient:
    drive_times = {
        (A, S1.point): 10.0,
        (S1.point, B): 17.0,
        (A, S2.point): 7.0,
        (S2.point, B): 18.0,
        # S3: drive(A, S3) deliberately absent -> None -> candidate skipped.
        (S3.point, B): 5.0,
    }
    pt_times = {
        (S1.point, C): 48.0,
        (S2.point, C): 59.0,
        (S3.point, C): 40.0,
    }
    if include_baselines:
        drive_times[(A, B)] = 20.0
        pt_times[(A, C)] = 90.0
    return FakeOneMapClient(drive_times=drive_times, pt_times=pt_times)


async def test_optimise_canonical_picks_best():
    client = make_client()
    result = await optimise(
        client,
        origin=A,
        driver_dest=B,
        rider_dest=C,
        stations=[S1, S2, S3],
        depart=DEPART,
        config=CONFIG,
    )

    assert result.feasible is True
    assert result.base_d_min == 20.0
    assert result.base_r_min == 90.0
    assert result.best.name == "Bishan MRT"
    assert result.best.new_d_min == 27.0
    assert result.best.new_r_min == 58.0
    assert [a.name for a in result.alternatives] == ["Toa Payoh MRT"]


async def test_optimise_skips_candidate_with_no_route():
    client = make_client()
    result = await optimise(
        client,
        origin=A,
        driver_dest=B,
        rider_dest=C,
        stations=[S1, S2, S3],
        depart=DEPART,
        config=CONFIG,
    )
    names = {result.best.name} | {a.name for a in result.alternatives}
    assert "No Route MRT" not in names  # skipped cleanly, no crash


async def test_optimise_infeasible_when_baseline_missing():
    client = make_client(include_baselines=False)  # no drive(A, B)
    result = await optimise(
        client,
        origin=A,
        driver_dest=B,
        rider_dest=C,
        stations=[S1, S2, S3],
        depart=DEPART,
        config=CONFIG,
    )
    assert result.feasible is False
    assert result.best is None
