"""Joint multi-stop optimiser: one car, one route, up to three passengers.

The single-rider optimiser in :mod:`droppoint.optimiser` answers "where should I drop
this one friend?". This module answers the harder questions, in both directions:

- **Drop-offs** (:func:`optimise_multi`): everyone starts together at A, the driver
  ends at B, and each passenger is dropped at a station (continuing by public
  transport) or at their own destination along ONE route ``A -> stop -> ... -> B``.
- **Pick-ups** (:func:`optimise_pickup`): the driver goes from A to a shared
  destination C, and each passenger starts somewhere else, meeting the car at a
  station (reached by public transport) or being collected at their door along
  ``A -> stop -> ... -> C``.

A passenger a plan cannot help is left to travel separately by public transport.

The rules deliberately differ from the single-rider mode:

- No hard driver-overhead cap by default. Plans are ranked by net minutes saved
  (passenger savings minus the driver's detour) and the driver's extra minutes are
  always shown, so the driver decides how much detour is acceptable.
  Set ``MULTI_MAX_DRIVER_OVERHEAD_FRAC`` to restore a hard cap.
- A passenger may be dropped/collected at their own door, not only at a station.
- Several distinct plans are returned so the group can choose the trade-off.
- Drop-off plans must beat travelling separately (net > 0): everyone is already
  together at A, so saving time is the only reason to share the car. Pick-ups are
  different - the group has *decided* to ride together, and on an efficient PT
  corridor no meeting point can beat the rider's own train - so net-negative
  "convenience" plans are kept and ranked; they compete with the door-to-door
  reference, whose driver detour they exist to cut. A meet must still be sane for
  the rider: at worst ``PICKUP_RIDER_TOLERANCE_MIN`` slower than their own PT trip,
  and cutting their PT leg to at most ``PICKUP_PT_LEG_MAX_FRAC`` of that trip.

Drop-off costing for ordered stops ``s1..sk`` (everyone departs A at t=0)::

    new_D      = drive(A,s1) + drive(s1,s2) + ... + drive(sk,B) + k * stop_overhead
    arrive(si) = drive legs up to si + waits + earlier stop overheads
    new_R      = arrive(rider's stop) + pt(stop, C)     (pt = 0 for a door drop)
    cost_D     = new_D - base_D
    net        = sum of rider savings - cost_D

Pick-up costing adds synchronised departures: nobody should wait at a meeting point,
so the driver sets off ``depart_delay`` minutes after the chosen time - just late
enough that every rider can reach their meeting point by public transport
(``pt(B_i, stop)``, 0 for a door pick-up) leaving no earlier than the chosen time -
and each rider sets off so they arrive exactly when the car does. Delaying the whole
car schedule does not change anyone's arrival at C, so this dominates waiting::

    depart_delay = max(0, max over stops of (slowest rider's pt - arrive(stop)))
    new_R        = pt(B_i, stop) + (new_D - arrive(stop))     rest of the ride
    base_R       = pt(B_i, C)

Leg times come from OneMap. Legs *between* stops are first estimated with a haversine
speed model so the combinatorial search stays cheap; the top plans are then re-costed
with real OneMap times before anything is shown (``exact`` marks fully verified plans).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from .config import OptimiserConfig
from .onemap import LatLng, RouteClient
from .stations import Station, corridor_prefilter, haversine_km

# Fixed component of an estimated inter-stop leg (turning off the route, junctions).
_EST_FIXED_MIN = 2.0

# A station this close to the rider's own point IS that point: the door option
# already covers it, and asking OneMap to route a point to itself returns 404.
_SAME_PLACE_KM = 0.15

Mode = Literal["station", "direct", "separate"]
Direction = Literal["dropoff", "pickup"]

LegLookup = Callable[[LatLng, LatLng], tuple[float, bool]]
"""Returns (minutes, exact) for a driving leg; exact is False for estimates."""


@dataclass(frozen=True)
class _Option:
    """One way a single rider can be handled inside a joint plan."""

    mode: Mode
    point: LatLng | None  # None when the rider travels separately
    stop_name: str | None
    lines: tuple[str, ...] = ()
    pt_min: float = 0.0  # rider's PT leg (after a drop / to reach a meet); 0 at the door


class RiderOutcome(BaseModel):
    """What one plan means for one rider (same order as the input riders)."""

    mode: Mode
    stop_name: str | None = None
    lines: list[str] = []
    pt_min: float = 0.0
    new_r_min: float | None = None
    save_r_min: float | None = None  # None when the rider has no PT baseline
    leave_after_min: float = 0.0  # pick-ups: minutes after the chosen time they set off


class PlanStop(BaseModel):
    """One place the car pulls over, with the riders who get out there."""

    name: str
    lat: float
    lng: float
    kind: Literal["station", "direct"]
    rider_indices: list[int]
    arrive_min: float

    @property
    def point(self) -> LatLng:
        return LatLng(self.lat, self.lng)


class MultiStopPlan(BaseModel):
    """One complete option: a stop order plus every rider's outcome."""

    stops: list[PlanStop]
    riders: list[RiderOutcome]
    new_d_min: float
    cost_d_min: float
    cost_d_frac: float
    total_saved_min: float
    net_min: float
    exact: bool
    depart_delay_min: float = 0.0  # pick-ups: how much later the driver sets off
    tags: list[str] = []


class MultiOptimiseResult(BaseModel):
    """The outcome of one multi-passenger query."""

    direction: Direction = "dropoff"
    base_d_min: float
    rider_base_min: list[float | None]
    feasible: bool
    plans: list[MultiStopPlan] = []


def estimate_drive_min(a: LatLng, b: LatLng, config: OptimiserConfig) -> float:
    """Cheap driving-time estimate between two points, for pre-ranking only."""
    return (haversine_km(a, b) / config.inter_stop_speed_kmh) * 60.0 + _EST_FIXED_MIN


def evaluate_assignment(
    *,
    origin: LatLng,
    driver_dest: LatLng,
    assignment: Sequence[_Option],
    base_d_min: float,
    rider_base_min: Sequence[float | None],
    leg_min: LegLookup,
    config: OptimiserConfig,
) -> MultiStopPlan | None:
    """Cost one assignment of riders to drops. Pure; returns None when not worth it.

    Riders assigned the same point share one physical stop. All stop orders are tried
    (at most 3 stops, so at most 6 permutations) and the fastest is kept.
    """
    grouped: dict[LatLng, list[int]] = {}
    for i, opt in enumerate(assignment):
        if opt.mode == "separate" or opt.point is None:
            continue
        grouped.setdefault(opt.point, []).append(i)
    if not grouped:
        return None  # everyone separate is just the baseline, not a plan

    best_total: float | None = None
    best_arrive: dict[LatLng, float] = {}
    best_exact = True
    best_order: tuple[LatLng, ...] = ()
    for perm in itertools.permutations(grouped):
        total = 0.0
        all_exact = True
        arrive: dict[LatLng, float] = {}
        prev = origin
        for p in perm:
            leg, exact = leg_min(prev, p)
            all_exact &= exact
            total += leg
            arrive[p] = total
            total += config.stop_overhead_min
            prev = p
        leg, exact = leg_min(prev, driver_dest)
        all_exact &= exact
        total += leg
        if best_total is None or total < best_total:
            best_total, best_arrive, best_exact, best_order = total, arrive, all_exact, perm

    assert best_total is not None
    cost_d = best_total - base_d_min
    cost_d_frac = cost_d / base_d_min if base_d_min > 0 else float("inf")

    cap = config.multi_max_driver_overhead_frac
    if cap is not None and cost_d_frac > cap:
        return None

    riders: list[RiderOutcome] = []
    total_saved = 0.0
    for i, opt in enumerate(assignment):
        base_r = rider_base_min[i]
        if opt.mode == "separate":
            riders.append(
                RiderOutcome(
                    mode="separate",
                    new_r_min=base_r,
                    save_r_min=0.0 if base_r is not None else None,
                )
            )
            continue
        assert opt.point is not None
        new_r = best_arrive[opt.point] + opt.pt_min
        save = base_r - new_r if base_r is not None else None
        # A station drop that does not beat the rider's own PT trip is pointless:
        # the enumeration also tries this rider as direct or separate.
        if opt.mode == "station" and (save is None or save <= 0):
            return None
        if save is not None:
            total_saved += save
        riders.append(
            RiderOutcome(
                mode=opt.mode,
                stop_name=opt.stop_name,
                lines=list(opt.lines),
                pt_min=opt.pt_min,
                new_r_min=new_r,
                save_r_min=save,
            )
        )

    net = total_saved - cost_d
    all_direct = all(opt.mode == "direct" for opt in assignment)
    # Keep only plans that beat travelling separately; the full door-to-door plan is
    # always kept as a reference so the driver can see what chauffeuring costs.
    if net <= 0 and not all_direct:
        return None

    stops: list[PlanStop] = []
    for p in best_order:
        idxs = grouped[p]
        opts = [assignment[i] for i in idxs]
        direct = next((o for o in opts if o.mode == "direct"), None)
        lead = direct or opts[0]
        stops.append(
            PlanStop(
                name=lead.stop_name or "stop",
                lat=p.lat,
                lng=p.lng,
                kind="direct" if direct else "station",
                rider_indices=idxs,
                arrive_min=best_arrive[p],
            )
        )

    return MultiStopPlan(
        stops=stops,
        riders=riders,
        new_d_min=best_total,
        cost_d_min=cost_d,
        cost_d_frac=cost_d_frac,
        total_saved_min=total_saved,
        net_min=net,
        exact=best_exact,
        tags=[],
    )


def evaluate_pickup_assignment(
    *,
    origin: LatLng,
    driver_dest: LatLng,
    assignment: Sequence[_Option],
    base_d_min: float,
    rider_base_min: Sequence[float | None],
    leg_min: LegLookup,
    config: OptimiserConfig,
) -> MultiStopPlan | None:
    """Cost one assignment of riders to pick-up points. Pure; None when not worth it.

    Mirror of :func:`evaluate_assignment` with synchronised departures: the driver
    sets off ``depart_delay_min`` after the chosen time - just late enough that no
    one waits at any meeting point - and each rider sets off (``leave_after_min``)
    so they arrive exactly when the car does. Shifting the whole car schedule does
    not change anyone's arrival time at C, so waiting would be pure waste; a rider's
    door-to-door time is their PT leg plus the remaining ride.

    Unlike drop-offs, net-negative plans are kept: the group is riding together
    either way, so a meet competes with the door-to-door reference (cutting the
    driver's detour), not with staying home. A station meet is only rejected when
    it is a bad deal for that rider - slower than their own PT trip by more than
    ``pickup_rider_tolerance_min``, or barely shortening their PT leg (more than
    ``pickup_pt_leg_max_frac`` of doing the whole trip themselves).
    """
    grouped: dict[LatLng, list[int]] = {}
    for i, opt in enumerate(assignment):
        if opt.mode == "separate" or opt.point is None:
            continue
        grouped.setdefault(opt.point, []).append(i)
    if not grouped:
        return None  # everyone separate is just the baseline, not a plan

    best_total: float | None = None
    best_arrive: dict[LatLng, float] = {}
    best_exact = True
    best_order: tuple[LatLng, ...] = ()
    for perm in itertools.permutations(grouped):
        total = 0.0
        all_exact = True
        arrive: dict[LatLng, float] = {}
        prev = origin
        for p in perm:
            leg, exact = leg_min(prev, p)
            all_exact &= exact
            total += leg
            arrive[p] = total
            total += config.stop_overhead_min
            prev = p
        leg, exact = leg_min(prev, driver_dest)
        all_exact &= exact
        total += leg
        if best_total is None or total < best_total:
            best_total, best_arrive, best_exact, best_order = total, arrive, all_exact, perm

    assert best_total is not None
    cost_d = best_total - base_d_min
    cost_d_frac = cost_d / base_d_min if base_d_min > 0 else float("inf")

    cap = config.multi_max_driver_overhead_frac
    if cap is not None and cost_d_frac > cap:
        return None

    # Set off just late enough that every rider can reach their meeting point
    # (leaving no earlier than the chosen time) by the moment the car arrives.
    delay = max(
        0.0,
        max(
            max(assignment[i].pt_min for i in idxs) - best_arrive[p] for p, idxs in grouped.items()
        ),
    )

    riders: list[RiderOutcome] = []
    total_saved = 0.0
    for i, opt in enumerate(assignment):
        base_r = rider_base_min[i]
        if opt.mode == "separate":
            riders.append(
                RiderOutcome(
                    mode="separate",
                    new_r_min=base_r,
                    save_r_min=0.0 if base_r is not None else None,
                )
            )
            continue
        assert opt.point is not None
        car_at_stop = best_arrive[opt.point]
        leave_after = delay + car_at_stop - opt.pt_min  # >= 0 by construction of delay
        new_r = opt.pt_min + (best_total - car_at_stop)
        save = base_r - new_r if base_r is not None else None
        if opt.mode == "station":
            # Station options are only offered to riders with a PT baseline.
            assert base_r is not None and save is not None
            # A meet may cost this rider a few minutes against their own PT trip -
            # being collected is the point - but only within tolerance, and only if
            # it genuinely shortens their PT leg.
            if save <= 0 and (
                save < -config.pickup_rider_tolerance_min
                or opt.pt_min > config.pickup_pt_leg_max_frac * base_r
            ):
                return None
        if save is not None:
            total_saved += save
        riders.append(
            RiderOutcome(
                mode=opt.mode,
                stop_name=opt.stop_name,
                lines=list(opt.lines),
                pt_min=opt.pt_min,
                new_r_min=new_r,
                save_r_min=save,
                leave_after_min=leave_after,
            )
        )

    # No net > 0 gate here (unlike drop-offs): convenience plans stay, priced and
    # ranked, so the driver can trade the group's minutes against their own detour.
    net = total_saved - cost_d

    stops: list[PlanStop] = []
    for p in best_order:
        idxs = grouped[p]
        opts = [assignment[i] for i in idxs]
        direct = next((o for o in opts if o.mode == "direct"), None)
        lead = direct or opts[0]
        stops.append(
            PlanStop(
                name=lead.stop_name or "stop",
                lat=p.lat,
                lng=p.lng,
                kind="direct" if direct else "station",
                rider_indices=idxs,
                arrive_min=best_arrive[p],
            )
        )

    return MultiStopPlan(
        stops=stops,
        riders=riders,
        new_d_min=best_total,
        cost_d_min=cost_d,
        cost_d_frac=cost_d_frac,
        total_saved_min=total_saved,
        net_min=net,
        exact=best_exact,
        depart_delay_min=delay,
        tags=[],
    )


def select_plans(plans: list[MultiStopPlan], config: OptimiserConfig) -> list[MultiStopPlan]:
    """Pick up to ``multi_num_options`` distinct plans, tagging why each is shown.

    Idempotent: tags are recomputed from scratch, so it can run once on estimated
    plans (to decide what to verify) and again on the verified ones. Returned in
    display order: best net first, so the door-to-door reference sinks to the
    bottom whenever cheaper-for-the-driver plans exist.
    """
    for p in plans:
        p.tags = []
    ranked = sorted(plans, key=lambda p: (-p.net_min, p.cost_d_min))
    positive = [p for p in ranked if p.net_min > 0]
    chauffeur = next(
        (p for p in ranked if all(r.mode == "direct" for r in p.riders)),
        None,
    )

    picks: list[MultiStopPlan] = []

    def add(plan: MultiStopPlan | None, tag: str | None) -> None:
        if plan is None:
            return
        for existing in picks:
            if existing is plan or existing == plan:
                if tag and tag not in existing.tags:
                    existing.tags.append(tag)
                return
        if tag:
            plan.tags.append(tag)
        picks.append(plan)

    if positive:
        add(positive[0], "best overall")
    if ranked:
        # Over ALL plans, not just net-positive ones: a pick-up menu can be pure
        # convenience (every plan net-negative), and the driver's cheapest detour
        # is exactly what it exists to surface.
        add(min(ranked, key=lambda p: p.cost_d_min), "least detour for you")
    if positive:
        add(max(positive, key=lambda p: p.total_saved_min), "most passenger time saved")
    add(chauffeur, "door-to-door for everyone")
    for p in ranked:
        if len(picks) >= config.multi_num_options:
            break
        add(p, None)
    picks = picks[: config.multi_num_options]
    picks.sort(key=lambda p: (-p.net_min, p.cost_d_min))
    return picks


def _spread(stations: Sequence[Station], k: int) -> list[Station]:
    """Up to ``k`` stations spread evenly over an ordered list, both ends included."""
    if k <= 0:
        return []
    if len(stations) <= k:
        return list(stations)
    if k == 1:
        return [stations[0]]
    step = (len(stations) - 1) / (k - 1)
    return [stations[round(i * step)] for i in range(k)]


def _rider_candidates(
    direction: Direction,
    origin: LatLng,
    final_dest: LatLng,
    rider_point: LatLng,
    stations: Sequence[Station],
    config: OptimiserConfig,
) -> list[Station]:
    """Candidate stations for one rider: on the driver's way, plus on the rider's own.

    ``rider_point`` is the rider's destination for drop-offs and their starting point
    for pick-ups. "Along" nominates stations in the driver's A->end corridor nearest
    the rider; drop-offs add stations on the rider's own leg A->rider ("toward").

    Pick-ups instead add an even spread of the rider's own corridor (rider -> C).
    The useful meets live anywhere on that corridor, from the rider's door out to
    where it joins the driver's route - and when the rider starts behind the driver,
    the whole useful stretch is "behind" too (that backtrack IS the detour, priced
    by the drive legs, so it must not be filtered out on geometry). Geographic
    nearest-the-rider is also a poor proxy for a short PT leg (a station 8 km away
    on the right line beats one 3 km away on the wrong one), which is why the spread
    covers the corridor end to end instead of clustering at either extreme.
    """
    along = corridor_prefilter(
        origin,
        final_dest,
        rider_point,
        list(stations),
        config.multi_corridor_max_offset_km,
        config.multi_route_candidates,
    )
    if direction == "dropoff":
        toward = corridor_prefilter(
            origin,
            rider_point,
            rider_point,
            list(stations),
            config.multi_corridor_max_offset_km,
            config.multi_dest_candidates,
        )
    else:
        rider_corridor = corridor_prefilter(
            rider_point,
            final_dest,
            rider_point,
            list(stations),
            config.multi_corridor_max_offset_km,
            len(stations),
        )
        toward = _spread(rider_corridor, config.pickup_spread_candidates)

    merged: list[Station] = []
    seen: set[str] = set()
    for st in along + toward:
        if st.name in seen:
            continue
        if haversine_km(st.point, rider_point) < _SAME_PLACE_KM:
            continue  # stopping here IS the door option; that option covers it
        seen.add(st.name)
        merged.append(st)
    return merged


async def optimise_multi(
    client: RouteClient,
    *,
    origin: LatLng,
    driver_dest: LatLng,
    rider_dests: Sequence[LatLng],
    stations: Sequence[Station],
    depart: datetime,
    config: OptimiserConfig,
    rider_labels: Sequence[str] | None = None,
) -> MultiOptimiseResult:
    """Drop-offs: everyone starts at A together; each rider ends at their own place."""
    return await _optimise_joint(
        client,
        direction="dropoff",
        origin=origin,
        final_dest=driver_dest,
        rider_points=rider_dests,
        stations=stations,
        depart=depart,
        config=config,
        rider_labels=rider_labels,
    )


async def optimise_pickup(
    client: RouteClient,
    *,
    origin: LatLng,
    shared_dest: LatLng,
    rider_origins: Sequence[LatLng],
    stations: Sequence[Station],
    depart: datetime,
    config: OptimiserConfig,
    rider_labels: Sequence[str] | None = None,
) -> MultiOptimiseResult:
    """Pick-ups: the driver goes A -> C; each rider starts elsewhere and joins en route."""
    return await _optimise_joint(
        client,
        direction="pickup",
        origin=origin,
        final_dest=shared_dest,
        rider_points=rider_origins,
        stations=stations,
        depart=depart,
        config=config,
        rider_labels=rider_labels,
    )


async def _optimise_joint(
    client: RouteClient,
    *,
    direction: Direction,
    origin: LatLng,
    final_dest: LatLng,
    rider_points: Sequence[LatLng],
    stations: Sequence[Station],
    depart: datetime,
    config: OptimiserConfig,
    rider_labels: Sequence[str] | None = None,
) -> MultiOptimiseResult:
    """Full joint query: baselines, menus, combinatorial search, verify, select.

    ``rider_points`` are destinations for drop-offs, starting points for pick-ups;
    every rider's PT baseline and station legs run in the matching direction.
    """
    labels = (
        list(rider_labels)
        if rider_labels
        else [f"Passenger {i + 1}" for i in range(len(rider_points))]
    )

    def rider_pt(r: LatLng, station: LatLng | None = None):
        if direction == "dropoff":
            start, end = (station or origin), r  # towards the rider's destination
        else:
            start, end = r, (station or final_dest)  # from the rider's home
        return client.pt(start, end, depart)

    baselines = await asyncio.gather(
        client.drive(origin, final_dest, depart),
        *(rider_pt(r) for r in rider_points),
    )
    base_d, rider_base = baselines[0], list(baselines[1:])
    if base_d is None:
        return MultiOptimiseResult(
            direction=direction, base_d_min=0.0, rider_base_min=rider_base, feasible=False
        )

    candidates = [
        _rider_candidates(direction, origin, final_dest, r, stations, config) for r in rider_points
    ]

    # Exact legs: A->P and P->end for every unique stop point, and each rider's PT leg
    # to/from their candidate stations. The client caches and deduplicates, so
    # overlapping candidates cost nothing extra.
    points = list({st.point for group in candidates for st in group} | set(rider_points))
    a_times, b_times = await asyncio.gather(
        asyncio.gather(*(client.drive(origin, p, depart) for p in points)),
        asyncio.gather(*(client.drive(p, final_dest, depart) for p in points)),
    )
    exact_legs: dict[tuple[LatLng, LatLng], float] = {(origin, final_dest): base_d}
    for p, t in zip(points, a_times, strict=True):
        if t is not None:
            exact_legs[(origin, p)] = t
    for p, t in zip(points, b_times, strict=True):
        if t is not None:
            exact_legs[(p, final_dest)] = t

    pt_tasks = [
        (i, st, rider_pt(rider_points[i], st.point))
        for i, group in enumerate(candidates)
        for st in group
    ]
    pt_times = await asyncio.gather(*(task for _, _, task in pt_tasks))
    pt_by_rider: list[dict[str, float]] = [{} for _ in rider_points]
    for (i, st, _), t in zip(pt_tasks, pt_times, strict=True):
        if t is not None:
            pt_by_rider[i][st.name] = t

    def leg_min(a: LatLng, b: LatLng) -> tuple[float, bool]:
        t = exact_legs.get((a, b))
        if t is not None:
            return t, True
        return estimate_drive_min(a, b, config), False

    menus: list[list[_Option]] = []
    for i, rider_point in enumerate(rider_points):
        menu = [_Option(mode="separate", point=None, stop_name=None)]
        if (origin, rider_point) in exact_legs:  # rider's own place reachable by car
            menu.append(_Option(mode="direct", point=rider_point, stop_name=labels[i]))
        if rider_base[i] is not None:
            for st in candidates[i]:
                pt_min = pt_by_rider[i].get(st.name)
                if pt_min is None or (origin, st.point) not in exact_legs:
                    continue
                menu.append(
                    _Option(
                        mode="station",
                        point=st.point,
                        stop_name=st.name,
                        lines=tuple(st.lines),
                        pt_min=pt_min,
                    )
                )
        menus.append(menu)

    evaluator = evaluate_assignment if direction == "dropoff" else evaluate_pickup_assignment

    def evaluate(combo: tuple[_Option, ...]) -> MultiStopPlan | None:
        return evaluator(
            origin=origin,
            driver_dest=final_dest,
            assignment=combo,
            base_d_min=base_d,
            rider_base_min=rider_base,
            leg_min=leg_min,
            config=config,
        )

    evaluated: list[tuple[tuple[_Option, ...], MultiStopPlan]] = []
    for combo in itertools.product(*menus):
        plan = evaluate(combo)
        if plan is not None:
            evaluated.append((combo, plan))
    if not evaluated:
        return MultiOptimiseResult(
            direction=direction, base_d_min=base_d, rider_base_min=rider_base, feasible=False
        )

    # Verify with real driving times before showing anything. Each round re-costs the
    # top plans by net PLUS whatever selection would currently show (the tagged picks
    # - least detour, door-to-door - can rank well below the top), then repeats in
    # case re-ranking changed what selection shows. Converges in one or two rounds.
    attempted: set[tuple[LatLng, LatLng]] = set()
    for _ in range(3):
        evaluated.sort(key=lambda cp: -cp[1].net_min)
        prelim_ids = {id(p) for p in select_plans([plan for _, plan in evaluated], config)}
        to_verify = [
            cp
            for i, cp in enumerate(evaluated)
            if i < config.multi_verify_top or id(cp[1]) in prelim_ids
        ]
        needed = {
            (a, b)
            for _, plan in to_verify
            for a, b in itertools.permutations([s.point for s in plan.stops], 2)
            if (a, b) not in exact_legs and (a, b) not in attempted
        }
        if not needed:
            break
        attempted |= needed
        pairs = list(needed)
        times = await asyncio.gather(*(client.drive(a, b, depart) for a, b in pairs))
        for (a, b), t in zip(pairs, times, strict=True):
            if t is not None:
                exact_legs[(a, b)] = t
        verify_ids = {id(plan) for _, plan in to_verify}
        redone: list[tuple[tuple[_Option, ...], MultiStopPlan]] = []
        for combo, _ in to_verify:
            plan = evaluate(combo)
            if plan is not None:
                redone.append((combo, plan))
        evaluated = redone + [cp for cp in evaluated if id(cp[1]) not in verify_ids]

    picks = select_plans([plan for _, plan in evaluated], config)
    return MultiOptimiseResult(
        direction=direction,
        base_d_min=base_d,
        rider_base_min=rider_base,
        feasible=bool(picks),
        plans=picks,
    )
