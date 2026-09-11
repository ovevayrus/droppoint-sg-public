"""Tests for the joint pick-up optimiser (driver A -> C, riders join en route).

All offline. The scenario is hand-computable (stop_overhead = 1.5):

    A  = driver start, C = shared destination
    B1/B2 = rider starting points; base_R1 = pt(B1,C) = 60, base_R2 = pt(B2,C) = 55
    base_D = drive(A,C) = 20

Key maths exercised: departures are synchronised so nobody waits - the driver sets
off just late enough that the slowest rider can reach their meeting point in time,
and each rider sets off so they arrive exactly when the car does.
"""

from __future__ import annotations

from datetime import UTC, datetime

from droppoint.config import OptimiserConfig
from droppoint.formatting import format_plan_options
from droppoint.multistop import optimise_pickup
from droppoint.onemap import LatLng
from droppoint.stations import Station
from support import FakeOneMapClient

DEPART = datetime(2026, 6, 28, 9, 0, tzinfo=UTC)

A = LatLng(1.30, 103.80)  # driver start
C = LatLng(1.35, 103.90)  # shared destination
B1 = LatLng(1.32, 103.88)  # rider 1 home
B2 = LatLng(1.33, 103.86)  # rider 2 home

S1 = Station(name="Bishan MRT", lat=1.34, lng=103.88, lines=["NS", "CC"])
S2 = Station(name="Toa Payoh MRT", lat=1.32, lng=103.84, lines=["NS"])

CONFIG = OptimiserConfig(
    corridor_max_offset_km=50.0,
    multi_corridor_max_offset_km=50.0,
    stop_overhead_min=1.5,
)

DRIVE_TIMES = {
    (A, C): 20.0,
    # Station legs.
    (A, S1.point): 10.0,
    (S1.point, C): 12.0,
    (A, S2.point): 7.0,
    (S2.point, C): 15.0,
    # Door pick-up legs.
    (A, B1): 9.0,
    (B1, C): 13.0,
    (A, B2): 8.0,
    (B2, C): 11.0,
    # Inter-stop legs (both directions) so verification makes every plan exact.
    (S1.point, S2.point): 6.0,
    (S2.point, S1.point): 6.0,
    (B1, B2): 5.0,
    (B2, B1): 6.0,
    (S1.point, B1): 4.0,
    (B1, S1.point): 4.0,
    (S1.point, B2): 5.0,
    (B2, S1.point): 5.0,
    (S2.point, B1): 8.0,
    (B1, S2.point): 8.0,
    (S2.point, B2): 6.0,
    (B2, S2.point): 6.0,
}

PT_TIMES = {
    (B1, C): 60.0,
    (B2, C): 55.0,
    (B1, S1.point): 6.0,
    (B1, S2.point): 25.0,
    (B2, S1.point): 12.0,
    (B2, S2.point): 9.0,
}


def station_only_client() -> FakeOneMapClient:
    """No drive legs to the riders' homes, so only station meets are possible."""
    drive = {k: v for k, v in DRIVE_TIMES.items() if k not in ((A, B1), (A, B2))}
    return FakeOneMapClient(drive_times=drive, pt_times=dict(PT_TIMES))


async def run(config: OptimiserConfig = CONFIG, client: FakeOneMapClient | None = None):
    return await optimise_pickup(
        client or FakeOneMapClient(drive_times=dict(DRIVE_TIMES), pt_times=dict(PT_TIMES)),
        origin=A,
        shared_dest=C,
        rider_origins=[B1, B2],
        stations=[S1, S2],
        depart=DEPART,
        config=config,
        rider_labels=["Rider One", "Rider Two"],
    )


async def test_meet_at_station_with_synchronised_departures():
    """Both riders met at Bishan; departures are shifted so nobody waits.

    Unshifted, the car reaches S1 at 10 and C at 23.5. Rider 2 needs 12 min by PT,
    so the driver sets off 2 min later; rider 2 sets off immediately, rider 1
    (6 min by PT) sets off at +6. Nobody waits; the driver's time in the car stays
    23.5 min (cost +3.5), rider 1 travels 6 + 13.5 = 19.5, rider 2 12 + 13.5 = 25.5.
    """
    result = await run(client=station_only_client())
    assert result.feasible
    assert result.direction == "pickup"
    best = result.plans[0]
    assert "best overall" in best.tags
    assert [s.name for s in best.stops] == ["Bishan MRT"]
    assert best.new_d_min == 23.5
    assert best.cost_d_min == 3.5
    assert best.depart_delay_min == 2.0
    r1, r2 = best.riders
    assert r1.mode == "station" and r1.stop_name == "Bishan MRT"
    assert r1.leave_after_min == 6.0
    assert r1.new_r_min == 19.5 and r1.save_r_min == 40.5
    assert r2.leave_after_min == 0.0
    assert r2.new_r_min == 25.5 and r2.save_r_min == 29.5
    assert best.net_min == 66.5
    assert best.exact


async def test_riders_met_at_different_stations_in_best_order():
    """R1 at Bishan + R2 at Toa Payoh: Toa Payoh first is faster (28 vs 34 min)."""
    result = await run(client=station_only_client())
    mixed = [
        p
        for p in result.plans
        if {r.stop_name for r in p.riders if r.mode == "station"} == {"Bishan MRT", "Toa Payoh MRT"}
    ]
    assert mixed, "expected a plan meeting the riders at two different stations"
    plan = mixed[0]
    assert [s.name for s in plan.stops] == ["Toa Payoh MRT", "Bishan MRT"]
    assert plan.new_d_min == 28.0
    assert plan.exact


async def test_door_pickup_lets_riders_leave_much_later():
    """With door legs available the collect-everyone-at-home plan wins here.

    A -> B1 (9) -> B2 (9+1.5+5 = 15.5) -> C (17+11 = 28). Riders set off when the
    car arrives, so R1's door-to-door is 28-9 = 19 (saves 41) and R2's 12.5 (42.5).
    Nobody's PT leg constrains the car, so the driver sets off on time.
    """
    result = await run()
    best = result.plans[0]
    assert "best overall" in best.tags
    assert "door-to-door for everyone" in best.tags
    assert [r.mode for r in best.riders] == ["direct", "direct"]
    assert best.new_d_min == 28.0
    assert best.depart_delay_min == 0.0
    assert best.riders[0].leave_after_min == 9.0
    assert best.riders[0].new_r_min == 19.0 and best.riders[0].save_r_min == 41.0
    assert best.riders[1].leave_after_min == 15.5
    assert best.net_min == 75.5
    # The least-detour option is picking up rider 2 at home only (+0.5 min).
    least = next(p for p in result.plans if "least detour for you" in p.tags)
    assert least.cost_d_min == 0.5


async def test_station_meet_worse_than_own_pt_is_never_offered():
    """If meeting at Bishan cannot beat rider 2's own PT trip, it is never offered."""
    pt = dict(PT_TIMES)
    pt[(B2, S1.point)] = 60.0  # slower than just taking PT all the way (55)
    client = FakeOneMapClient(drive_times=dict(DRIVE_TIMES), pt_times=pt)
    result = await run(client=client)
    for plan in result.plans:
        r2 = plan.riders[1]
        assert not (r2.mode == "station" and r2.stop_name == "Bishan MRT")


async def test_infeasible_without_driver_baseline():
    drive = {k: v for k, v in DRIVE_TIMES.items() if k != (A, C)}
    client = FakeOneMapClient(drive_times=drive, pt_times=dict(PT_TIMES))
    result = await run(client=client)
    assert result.feasible is False
    assert result.plans == []


# --- convenience meets (net-negative but low-detour) ------------------------------
#
# Mirror of a live failure: the passenger comes from far away down an efficient PT
# corridor, so NO meeting point beats their own train and every shared plan costs the
# group minutes. The planner must still offer low-detour meets - the door-to-door run
# (+41.5 for the driver) must not be the only option. Hand maths (overhead 1.5,
# base_D = drive(A2,C2) = 15, base_R = pt(F,C2) = 50):
#
#   KHATIB  pt 20, legs 14/16 -> new_D 31.5 (+16.5); new_R 37.5 (saves 12.5); net -4
#   NOVENA  pt 39, legs 10/13 -> new_D 24.5 (+9.5);  new_R 53.5 (3.5 slower); net -13
#           (within the 5-min tolerance; PT leg 39 <= 0.8 x 50, so still offered)
#   CITY    pt 46, legs 13/3  -> PT leg 46 > 0.8 x 50: barely shortened -> never offered
#   MARY    pt 30, legs 12/25 -> new_R 56.5, 6.5 slower than solo PT -> never offered
#   door    legs 28/27        -> new_D 56.5 (+41.5); new_R 28.5 (saves 21.5); net -20

A2 = LatLng(1.30, 103.80)  # driver start
C2 = LatLng(1.35, 103.90)  # shared destination
F = LatLng(1.45, 103.82)  # passenger, far up an efficient PT corridor

KHATIB = Station(name="Khatib MRT", lat=1.42, lng=103.84, lines=["NS"])
NOVENA = Station(name="Novena MRT", lat=1.40, lng=103.86, lines=["NS"])
CITY = Station(name="City Hall MRT", lat=1.36, lng=103.89, lines=["NS", "EW"])
MARY = Station(name="Marymount MRT", lat=1.43, lng=103.86, lines=["CC"])

FAR_DRIVE = {
    (A2, C2): 15.0,
    (A2, KHATIB.point): 14.0,
    (KHATIB.point, C2): 16.0,
    (A2, NOVENA.point): 10.0,
    (NOVENA.point, C2): 13.0,
    (A2, CITY.point): 13.0,
    (CITY.point, C2): 3.0,
    (A2, MARY.point): 12.0,
    (MARY.point, C2): 25.0,
    (A2, F): 28.0,
    (F, C2): 27.0,
}

FAR_PT = {
    (F, C2): 50.0,
    (F, KHATIB.point): 20.0,
    (F, NOVENA.point): 39.0,
    (F, CITY.point): 46.0,
    (F, MARY.point): 30.0,
}


async def run_far(config: OptimiserConfig = CONFIG):
    return await optimise_pickup(
        FakeOneMapClient(drive_times=dict(FAR_DRIVE), pt_times=dict(FAR_PT)),
        origin=A2,
        shared_dest=C2,
        rider_origins=[F],
        stations=[KHATIB, NOVENA, CITY, MARY],
        depart=DEPART,
        config=config,
        rider_labels=["Far Friend"],
    )


async def test_convenience_meets_offered_when_no_meet_saves_time():
    """Every plan is net-negative here, yet the driver still gets real choices."""
    result = await run_far()
    assert result.feasible
    assert len(result.plans) == 3
    khatib, novena, door = result.plans

    # Best trade first: meet at Khatib for a third of the door-to-door detour.
    assert khatib.riders[0].stop_name == "Khatib MRT"
    assert khatib.cost_d_min == 16.5
    assert khatib.net_min == -4.0
    assert khatib.exact

    # A meet may cost the rider a few minutes against their own PT trip.
    assert novena.riders[0].stop_name == "Novena MRT"
    assert "least detour for you" in novena.tags
    assert novena.cost_d_min == 9.5
    assert novena.riders[0].save_r_min == -3.5

    # The chauffeur run is still shown, as the reference, at the bottom.
    assert [r.mode for r in door.riders] == ["direct"]
    assert "door-to-door for everyone" in door.tags
    assert door.cost_d_min == 41.5


async def test_convenience_meets_synchronise_departures():
    """Car reaches Khatib at 14, the rider needs 20 by PT: driver sets off 6 later."""
    result = await run_far()
    khatib = next(p for p in result.plans if p.riders[0].stop_name == "Khatib MRT")
    assert khatib.depart_delay_min == 6.0
    assert khatib.riders[0].leave_after_min == 0.0


async def test_meet_that_barely_shortens_the_pt_leg_is_never_offered():
    """City Hall leaves the rider doing 46 of their 50 PT minutes anyway."""
    result = await run_far()
    for plan in result.plans:
        assert plan.riders[0].stop_name != "City Hall MRT"


async def test_meet_slower_than_the_tolerance_is_never_offered_by_default():
    """Marymount makes the rider 6.5 min slower than solo PT - beyond the 5-min default."""
    result = await run_far()
    for plan in result.plans:
        assert plan.riders[0].stop_name != "Marymount MRT"


async def test_rider_tolerance_is_configurable():
    config = CONFIG.model_copy(update={"pickup_rider_tolerance_min": 10.0})
    result = await run_far(config=config)
    assert any(p.riders[0].stop_name == "Marymount MRT" for p in result.plans)


# --- formatting -----------------------------------------------------------------


async def test_format_mentions_meeting_and_departure_times():
    result = await run(client=station_only_client())
    text = format_plan_options(
        result, origin=A, driver_dest=C, rider_labels=["Rider One", "Rider Two"]
    )
    assert "meet at Bishan MRT (NS / CC), 6 min by PT" in text
    # Driver sets off 2 min later so rider 2 (12 min by PT) makes the meeting point.
    assert "You drive: 20 -> 24 min (+4), setting off 2 min later" in text
    # Rider 1 only needs 6 min by PT, so they set off 6 min after the chosen time.
    assert ", setting off 6 min later" in text
    assert "-> shared destination" in text


async def test_format_door_pickup():
    result = await run()
    text = format_plan_options(
        result, origin=A, driver_dest=C, rider_labels=["Rider One", "Rider Two"]
    )
    assert (
        "- Rider One: picked up at their door - 60 -> 19 min (-41), setting off 9 min later"
    ) in text
    assert "Map: https://www.google.com/maps/dir/" in text


async def test_format_shows_chosen_time_and_absolute_set_offs():
    result = await run_far()
    depart = datetime(2026, 6, 28, 10, 0, tzinfo=UTC)
    text = format_plan_options(
        result, origin=A2, driver_dest=C2, rider_labels=["Far Friend"], depart=depart
    )
    assert "for you and 1 passenger, leaving at 10:00:" in text
    # The driver's delayed set-off gets an absolute time (Novena meet: +29 min)...
    assert "setting off 29 min later (10:29)" in text
    # ...and so does a rider's (door pick-up: the car reaches them 28 min in).
    assert "setting off 28 min later (10:28)" in text


async def test_format_without_depart_keeps_relative_times_only():
    result = await run_far()
    text = format_plan_options(result, origin=A2, driver_dest=C2, rider_labels=["Far Friend"])
    assert "leaving at" not in text
    assert "setting off 29 min later" in text
    assert "(10:29)" not in text


# --- bot flow ---------------------------------------------------------------------


async def test_bot_pickup_flow():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from telegram.ext import ConversationHandler

    from droppoint.bot import ASK_ALL, ASK_DEPART, pickup_start, received_all, received_depart
    from droppoint.onemap import GeocodeResult

    geocodes = {
        "start": GeocodeResult(label="Driver Home", point=A),
        "dest": GeocodeResult(label="Shared Dest", point=C),
        "rider1": GeocodeResult(label="Rider One Home", point=B1),
        "rider2": GeocodeResult(label="Rider Two Home", point=B2),
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

    assert await pickup_start(update("/pickup"), context) == ASK_ALL
    assert "Pick-up mode" in replies[-1]
    assert await received_all(update("start\ndest\nrider1\nrider2"), context) == ASK_DEPART
    assert "Picking up 2 passengers" in replies[-1]
    assert await received_depart(update("10:00"), context) == ConversationHandler.END

    final = "\n".join(replies)
    assert "route options for you and 2 passengers, leaving at 10:00" in final
    assert "picked up at their door" in final
    assert "shared destination" in final
    assert "Rider One Home" in final and "Rider Two Home" in final
    assert "Map: https://www.google.com/maps/dir/" in final
