"""Tests for the joint multi-drop optimiser and its bot/formatting integration.

All offline: times come from FakeOneMapClient lookup tables. The scenario has two
riders whose public-transport baselines are poor, so door-to-door plans, shared
station drops, and mixed plans all appear and can be checked by hand:

    A = start, B = driver destination, C1/C2 = rider destinations
    base_D = 20, base_R1 = 90 (PT), base_R2 = 70 (PT)
"""

from __future__ import annotations

from datetime import UTC, datetime

from droppoint.config import OptimiserConfig
from droppoint.formatting import format_plan_options, google_maps_link
from droppoint.multistop import optimise_multi
from droppoint.onemap import LatLng
from droppoint.stations import Station
from support import FakeOneMapClient

DEPART = datetime(2026, 6, 28, 9, 0, tzinfo=UTC)

A = LatLng(1.30, 103.80)
B = LatLng(1.35, 103.90)
C1 = LatLng(1.32, 103.88)
C2 = LatLng(1.33, 103.86)

S1 = Station(name="Bishan MRT", lat=1.34, lng=103.88, lines=["NS", "CC"])
S2 = Station(name="Toa Payoh MRT", lat=1.32, lng=103.84, lines=["NS"])

# Wide corridor so geometry never excludes the test stations (the prefilter itself is
# covered in test_stations.py). stop_overhead fixed so the maths below stays exact.
CONFIG = OptimiserConfig(
    corridor_max_offset_km=50.0,
    multi_corridor_max_offset_km=50.0,
    stop_overhead_min=1.5,
)

DRIVE_TIMES = {
    (A, B): 20.0,
    # Station legs.
    (A, S1.point): 10.0,
    (S1.point, B): 17.0,
    (A, S2.point): 7.0,
    (S2.point, B): 18.0,
    # Door-drop legs.
    (A, C1): 18.0,
    (C1, B): 12.0,
    (A, C2): 15.0,
    (C2, B): 9.0,
    # Inter-stop legs (both directions) so verification makes every plan exact.
    (S1.point, S2.point): 6.0,
    (S2.point, S1.point): 6.0,
    (S1.point, C1): 4.0,
    (C1, S1.point): 4.0,
    (S1.point, C2): 5.0,
    (C2, S1.point): 5.0,
    (S2.point, C1): 8.0,
    (C1, S2.point): 8.0,
    (S2.point, C2): 6.0,
    (C2, S2.point): 6.0,
    (C1, C2): 5.0,
    (C2, C1): 6.0,
}

PT_TIMES = {
    (A, C1): 90.0,
    (A, C2): 70.0,
    (S1.point, C1): 30.0,
    (S1.point, C2): 25.0,
    (S2.point, C1): 59.0,
    (S2.point, C2): 30.0,
}


def make_client() -> FakeOneMapClient:
    return FakeOneMapClient(drive_times=dict(DRIVE_TIMES), pt_times=dict(PT_TIMES))


async def run(config: OptimiserConfig = CONFIG, client: FakeOneMapClient | None = None):
    return await optimise_multi(
        client or make_client(),
        origin=A,
        driver_dest=B,
        rider_dests=[C1, C2],
        stations=[S1, S2],
        depart=DEPART,
        config=config,
        rider_labels=["Rider One", "Rider Two"],
    )


async def test_best_plan_is_door_to_door_here():
    """With terrible PT baselines the chauffeur run wins, and the maths is exact.

    A -> C1 -> C2 -> B = 18 + 5 + 9 + 2 * 1.5 = 35 min; cost_D = 15.
    R1 arrives at 18 (saves 72); R2 arrives at 18 + 1.5 + 5 = 24.5 (saves 45.5).
    net = 117.5 - 15 = 102.5.
    """
    result = await run()
    assert result.feasible
    best = result.plans[0]
    assert "best overall" in best.tags
    assert [r.mode for r in best.riders] == ["direct", "direct"]
    assert [s.name for s in best.stops] == ["Rider One", "Rider Two"]
    assert best.new_d_min == 35.0
    assert best.cost_d_min == 15.0
    assert best.riders[0].new_r_min == 18.0
    assert best.riders[1].new_r_min == 24.5
    assert best.net_min == 102.5
    assert best.exact


async def test_driver_overhead_above_50_percent_is_allowed():
    """The old 50% cap must not apply in multi mode: 15/20 = 75% overhead is shown."""
    result = await run()
    assert any(p.cost_d_frac > 0.5 for p in result.plans)


async def test_offers_distinct_tagged_alternatives():
    result = await run()
    all_tags = [t for p in result.plans for t in p.tags]
    assert "best overall" in all_tags
    assert "least detour for you" in all_tags
    assert "door-to-door for everyone" in all_tags
    # The least-detour option really is the cheapest net-positive plan for the driver:
    # rider 2 at their door, rider 1 at Toa Payoh (7 + 6 + 9 + 2 * 1.5 = 25, cost 5).
    least = next(p for p in result.plans if "least detour for you" in p.tags)
    assert least.cost_d_min == 5.0
    # Every plan shown either helps the group or is the door-to-door reference.
    for plan in result.plans:
        assert plan.net_min > 0 or all(r.mode == "direct" for r in plan.riders)


async def test_riders_dropped_at_different_stations_share_one_route():
    """Each rider at their own station, along ONE driving route in the best order.

    Without door drops the best mixed plan is A -> Toa Payoh (drop rider 2... rider 1)
    ordered by driving time: A->S2->S1->B = 7 + 6 + 17 + 2 * 1.5 = 33, not the naive
    S1-first order (10 + 6 + 18 + 3 = 37).
    """
    drive = {k: v for k, v in DRIVE_TIMES.items() if k not in ((A, C1), (A, C2))}
    client = FakeOneMapClient(drive_times=drive, pt_times=dict(PT_TIMES))
    result = await run(client=client)
    mixed = [
        p
        for p in result.plans
        if {r.stop_name for r in p.riders if r.mode == "station"} == {"Bishan MRT", "Toa Payoh MRT"}
    ]
    assert mixed, "expected a plan dropping the riders at two different stations"
    plan = mixed[0]
    assert [s.name for s in plan.stops] == ["Toa Payoh MRT", "Bishan MRT"]
    assert plan.stops[0].arrive_min == 7.0
    assert plan.stops[1].arrive_min == 14.5  # 7 + 1.5 stop + 6 drive
    assert plan.new_d_min == 33.0
    assert plan.exact


async def test_station_drop_that_does_not_help_rider_is_never_offered():
    """Toa Payoh PT for rider 1 (7 + 59 = 66 < 90) helps, but make it useless (95)."""
    pt = dict(PT_TIMES)
    pt[(S2.point, C1)] = 95.0  # worse than going by PT from the start
    client = FakeOneMapClient(drive_times=dict(DRIVE_TIMES), pt_times=pt)
    result = await run(client=client)
    for plan in result.plans:
        r1 = plan.riders[0]
        assert not (r1.mode == "station" and r1.stop_name == "Toa Payoh MRT")


async def test_optional_hard_cap_is_respected_when_set():
    config = CONFIG.model_copy(update={"multi_max_driver_overhead_frac": 0.35})
    result = await run(config=config)
    assert result.feasible
    for plan in result.plans:
        assert plan.cost_d_frac <= 0.35


async def test_infeasible_without_driver_baseline():
    drive = {k: v for k, v in DRIVE_TIMES.items() if k != (A, B)}
    client = FakeOneMapClient(drive_times=drive, pt_times=dict(PT_TIMES))
    result = await run(client=client)
    assert result.feasible is False
    assert result.plans == []


async def test_rider_without_pt_baseline_can_still_be_dropped_at_door():
    pt = {k: v for k, v in PT_TIMES.items() if k[1] != C1}  # no PT anywhere for rider 1
    client = FakeOneMapClient(drive_times=dict(DRIVE_TIMES), pt_times=pt)
    result = await run(client=client)
    assert result.feasible
    for plan in result.plans:
        assert plan.riders[0].mode in ("direct", "separate")
    dropped = [p for p in result.plans if p.riders[0].mode == "direct"]
    assert dropped, "rider without a PT baseline should still get door-to-door plans"
    assert all(p.riders[0].save_r_min is None for p in dropped)


# --- formatting -----------------------------------------------------------------


async def test_format_plan_options_renders_all_sections():
    result = await run()
    text = format_plan_options(
        result, origin=A, driver_dest=B, rider_labels=["Rider One", "Rider Two"]
    )
    assert "Option 1 - " in text
    assert "You drive: 20 -> 35 min (+15)" in text
    assert "Rider One" in text and "Rider Two" in text
    assert "Map: https://www.google.com/maps/dir/" in text
    assert "your destination" in text


async def test_format_separate_when_nothing_works():
    client = FakeOneMapClient(drive_times={(A, B): 20.0}, pt_times=dict(PT_TIMES))
    result = await run(client=client)
    text = format_plan_options(
        result, origin=A, driver_dest=B, rider_labels=["Rider One", "Rider Two"]
    )
    assert "Travelling separately is best here." in text
    assert "Rider One (public transport):  90 min" in text


def test_google_maps_link_multiple_waypoints_in_order():
    link = google_maps_link(A, B, [S1.point, C2])
    assert f"waypoints={S1.lat},{S1.lng}%7C{C2.lat},{C2.lng}" in link
    assert "travelmode=driving" in link


# --- bot flow ---------------------------------------------------------------------


async def test_bot_flow_two_riders_gets_joint_plans():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from telegram.ext import ConversationHandler

    from droppoint.bot import ASK_ALL, ASK_DEPART, received_all, received_depart, start
    from droppoint.onemap import GeocodeResult

    geocodes = {
        "start": GeocodeResult(label="Start Point", point=A),
        "driver": GeocodeResult(label="Driver Dest", point=B),
        "rider1": GeocodeResult(label="Rider One Dest", point=C1),
        "rider2": GeocodeResult(label="Rider Two Dest", point=C2),
    }
    client = FakeOneMapClient(
        drive_times=dict(DRIVE_TIMES), pt_times=dict(PT_TIMES), geocodes=geocodes
    )
    context = SimpleNamespace(
        bot_data={"client": client, "stations": [S1, S2], "config": CONFIG},
        user_data={},
    )
    replies: list[str] = []

    def update(text):
        async def reply_text(t, **kwargs):
            replies.append(t)

        chat = SimpleNamespace(send_action=AsyncMock())
        message = SimpleNamespace(text=text, location=None, reply_text=reply_text)
        return SimpleNamespace(message=message, effective_chat=chat)

    assert await start(update("/start"), context) == ASK_ALL
    assert await received_all(update("start\ndriver\nrider1\nrider2"), context) == ASK_DEPART
    assert await received_depart(update("now"), context) == ConversationHandler.END

    final = "\n".join(replies)
    assert "route options for you and 2 passengers" in final
    assert "Option 1 - " in final
    assert "Rider One Dest" in final and "Rider Two Dest" in final
    assert "Map: https://www.google.com/maps/dir/" in final
