"""The optimiser core.

Pure functions: times in, ranked results out. No HTTP, no I/O. The maths follows
PROMPT.md exactly.

For a candidate drop-off point P, given the baselines::

    base_D = drive(A, B)            base_R = pt(A, C)
    new_D  = drive(A, P) + drive(P, B)
    new_R  = drive(A, P) + pt(P, C)
    cost_D = new_D - base_D         save_R = base_R - new_R
    net    = save_R - cost_D
    cost_D_frac = cost_D / base_D   save_R_frac = save_R / base_R

The async orchestration that calls OneMap lives in :func:`optimise` (added with the
station dataset). Everything here is deterministic and unit-tested offline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import BaseModel

from .config import OptimiserConfig
from .onemap import LatLng, RouteClient
from .stations import Station, corridor_prefilter


class ScoredCandidate(BaseModel):
    """A drop-off point with all derived metrics."""

    name: str
    lat: float
    lng: float
    lines: list[str] = []

    drive_ap_min: float
    drive_pb_min: float
    pt_pc_min: float

    new_d_min: float
    new_r_min: float
    cost_d_min: float
    save_r_min: float
    net_min: float
    cost_d_frac: float
    save_r_frac: float
    eligible: bool

    @property
    def point(self) -> LatLng:
        return LatLng(self.lat, self.lng)


class OptimiseResult(BaseModel):
    """The outcome of one query."""

    base_d_min: float
    base_r_min: float
    feasible: bool
    best: ScoredCandidate | None = None
    alternatives: list[ScoredCandidate] = []
    near_miss: ScoredCandidate | None = None


def _is_eligible(
    save_r: float,
    net: float,
    cost_d_frac: float,
    save_r_frac: float,
    config: OptimiserConfig,
) -> bool:
    """Apply the eligibility filter from the spec."""
    if save_r <= 0:
        return False
    if net <= 0:
        return False
    if cost_d_frac > config.max_driver_overhead_frac:
        return False
    if config.require_fair_proportion and save_r_frac < cost_d_frac:
        return False
    return True


def score_candidate(
    *,
    name: str,
    lat: float,
    lng: float,
    lines: list[str] | None = None,
    base_d_min: float,
    base_r_min: float,
    drive_ap_min: float,
    drive_pb_min: float,
    pt_pc_min: float,
    config: OptimiserConfig,
) -> ScoredCandidate:
    """Score one candidate drop-off point. Pure."""
    new_d = drive_ap_min + drive_pb_min
    new_r = drive_ap_min + pt_pc_min
    cost_d = new_d - base_d_min
    save_r = base_r_min - new_r
    net = save_r - cost_d

    # base_d_min is a real drive time (> 0) in practice; guard the degenerate case so a
    # zero baseline yields an infinite fraction and is filtered out, never a divide error.
    cost_d_frac = cost_d / base_d_min if base_d_min > 0 else float("inf")
    save_r_frac = save_r / base_r_min if base_r_min > 0 else 0.0

    eligible = _is_eligible(save_r, net, cost_d_frac, save_r_frac, config)

    return ScoredCandidate(
        name=name,
        lat=lat,
        lng=lng,
        lines=lines or [],
        drive_ap_min=drive_ap_min,
        drive_pb_min=drive_pb_min,
        pt_pc_min=pt_pc_min,
        new_d_min=new_d,
        new_r_min=new_r,
        cost_d_min=cost_d,
        save_r_min=save_r,
        net_min=net,
        cost_d_frac=cost_d_frac,
        save_r_frac=save_r_frac,
        eligible=eligible,
    )


def _efficient_key(c: ScoredCandidate) -> tuple[float, float, float]:
    # Rank by net descending, then smallest fairness gap, then smallest driver cost.
    return (-c.net_min, abs(c.save_r_frac - c.cost_d_frac), c.cost_d_min)


def _balanced_key(c: ScoredCandidate) -> tuple[float, float]:
    # Rank by smallest fairness gap, then largest net.
    return (abs(c.save_r_frac - c.cost_d_frac), -c.net_min)


def _closest_near_miss(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    """Best ineligible option that still helps the rider, for an optional caveat."""
    helped = [c for c in scored if not c.eligible and c.save_r_min > 0]
    if not helped:
        return None
    return max(helped, key=lambda c: c.net_min)


def rank_candidates(
    scored: list[ScoredCandidate],
    base_d_min: float,
    base_r_min: float,
    config: OptimiserConfig,
) -> OptimiseResult:
    """Filter to eligible candidates, rank them, and pick the best plus alternatives."""
    eligible = [c for c in scored if c.eligible]

    if not eligible:
        near = _closest_near_miss(scored) if config.show_near_miss else None
        return OptimiseResult(
            base_d_min=base_d_min,
            base_r_min=base_r_min,
            feasible=False,
            near_miss=near,
        )

    key = _efficient_key if config.objective == "efficient" else _balanced_key
    ranked = sorted(eligible, key=key)

    return OptimiseResult(
        base_d_min=base_d_min,
        base_r_min=base_r_min,
        feasible=True,
        best=ranked[0],
        alternatives=ranked[1:3],
    )


# --- async orchestration ------------------------------------------------------


async def _score_all(
    client: RouteClient,
    origin: LatLng,
    driver_dest: LatLng,
    rider_dest: LatLng,
    candidates: list[Station],
    base_d_min: float,
    base_r_min: float,
    depart: datetime,
    config: OptimiserConfig,
) -> list[ScoredCandidate]:
    async def score_one(st: Station) -> ScoredCandidate | None:
        drive_ap, drive_pb, pt_pc = await asyncio.gather(
            client.drive(origin, st.point, depart),
            client.drive(st.point, driver_dest, depart),
            client.pt(st.point, rider_dest, depart),
        )
        # A missing leg means OneMap found no route; skip this candidate.
        if drive_ap is None or drive_pb is None or pt_pc is None:
            return None
        return score_candidate(
            name=st.name,
            lat=st.lat,
            lng=st.lng,
            lines=st.lines,
            base_d_min=base_d_min,
            base_r_min=base_r_min,
            drive_ap_min=drive_ap,
            drive_pb_min=drive_pb,
            pt_pc_min=pt_pc,
            config=config,
        )

    results = await asyncio.gather(*(score_one(st) for st in candidates))
    return [r for r in results if r is not None]


async def optimise(
    client: RouteClient,
    *,
    origin: LatLng,
    driver_dest: LatLng,
    rider_dest: LatLng,
    stations: list[Station],
    depart: datetime,
    config: OptimiserConfig,
) -> OptimiseResult:
    """Full query: baselines, prefilter, exact scoring, and ranking.

    Calls OneMap via ``client`` (anything matching :class:`~droppoint.onemap.RouteClient`,
    so a mock drops straight in). The per-candidate calls run concurrently; the client's
    own semaphore caps real concurrency.
    """
    base_d_min = await client.drive(origin, driver_dest, depart)
    base_r_min = await client.pt(origin, rider_dest, depart)
    if base_d_min is None or base_r_min is None:
        # Without baselines there is nothing to compare against.
        return OptimiseResult(
            base_d_min=base_d_min or 0.0,
            base_r_min=base_r_min or 0.0,
            feasible=False,
        )

    candidates = corridor_prefilter(
        origin,
        driver_dest,
        rider_dest,
        stations,
        config.corridor_max_offset_km,
        config.max_candidates_evaluated,
    )
    scored = await _score_all(
        client,
        origin,
        driver_dest,
        rider_dest,
        candidates,
        base_d_min,
        base_r_min,
        depart,
        config,
    )
    return rank_candidates(scored, base_d_min, base_r_min, config)
