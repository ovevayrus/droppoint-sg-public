"""Reply text and map links. British English, short lines."""

from __future__ import annotations

from datetime import datetime, timedelta

from .multistop import MultiOptimiseResult, MultiStopPlan
from .onemap import LatLng
from .optimiser import OptimiseResult, ScoredCandidate

# Expose for bot import.
__all__ = ["format_result", "format_multi_result", "format_plan_options", "google_maps_link"]


def google_maps_link(
    origin: LatLng,
    destination: LatLng,
    waypoints: LatLng | list[LatLng] | None = None,
) -> str:
    """A Google Maps driving-directions deep link, optionally via waypoints in order."""
    if isinstance(waypoints, LatLng):
        waypoints = [waypoints]
    parts = [
        "https://www.google.com/maps/dir/?api=1",
        f"origin={origin.lat},{origin.lng}",
        f"destination={destination.lat},{destination.lng}",
    ]
    if waypoints:
        joined = "%7C".join(f"{w.lat},{w.lng}" for w in waypoints)  # %7C = '|'
        parts.append(f"waypoints={joined}")
    parts.append("travelmode=driving")
    return "&".join(parts)


def _lines_suffix(c: ScoredCandidate) -> str:
    return f" ({' / '.join(c.lines)})" if c.lines else ""


def _m(value: float) -> int:
    return round(value)


def _at_clock(depart: datetime | None, minutes_after: float) -> str:
    """`` (HH:MM)`` for a set-off N minutes after the chosen time; empty if unknown."""
    if depart is None:
        return ""
    return (depart + timedelta(minutes=_m(minutes_after))).strftime(" (%H:%M)")


def format_result(
    result: OptimiseResult,
    *,
    origin: LatLng,
    driver_dest: LatLng,
    rider_dest: LatLng,
) -> str:
    """Render an OptimiseResult as the bot's reply."""
    if not result.feasible or result.best is None:
        return _format_separate(result)

    b = result.best
    link = google_maps_link(origin, driver_dest, b.point)

    d_from, d_to, d_cost = _m(result.base_d_min), _m(b.new_d_min), _m(b.cost_d_min)
    r_from, r_to, r_save = _m(result.base_r_min), _m(b.new_r_min), _m(b.save_r_min)
    drive_line = f"You drive:  {d_from} min  ->  {d_to} min  (+{d_cost})"
    friend_line = f"Friend:     {r_from} min  ->  {r_to} min  (-{r_save})"

    lines = [
        f"Best drop-off: {b.name}{_lines_suffix(b)}",
        "",
        drive_line,
        friend_line,
        f"Saved together: {_m(b.net_min)} min",
        "",
        f"Map: {link}",
    ]

    if result.alternatives:
        lines.append("")
        lines.append("Other options:")
        for i, alt in enumerate(result.alternatives, start=2):
            lines.append(
                f"{i}) {alt.name}   you +{_m(alt.cost_d_min)}, friend -{_m(alt.save_r_min)}"
            )

    return "\n".join(lines)


def format_multi_result(
    rider_results: list[tuple[OptimiseResult, tuple[LatLng, str]]],
    *,
    origin: LatLng,
    driver_dest: LatLng,
) -> str:
    """Render results for one or more passengers."""
    if len(rider_results) == 1:
        result, (rider_dest, _) = rider_results[0]
        return format_result(result, origin=origin, driver_dest=driver_dest, rider_dest=rider_dest)

    sections = []
    for i, (result, (rider_dest, rider_label)) in enumerate(rider_results, start=1):
        header = f"Passenger {i} ({rider_label})"
        body = format_result(result, origin=origin, driver_dest=driver_dest, rider_dest=rider_dest)
        sections.append(f"{header}\n{body}")

    return "\n\n---\n\n".join(sections)


def format_plan_options(
    result: MultiOptimiseResult,
    *,
    origin: LatLng,
    driver_dest: LatLng,
    rider_labels: list[str],
    depart: datetime | None = None,
) -> str:
    """Render the joint multi-drop plans as the bot's reply.

    ``depart`` is the chosen departure time; when given, the header repeats it and
    every "setting off N min later" note also shows the absolute clock time.
    """
    if not result.feasible or not result.plans:
        return _format_multi_separate(result, rider_labels)

    n = len(rider_labels)
    plural = "option" if len(result.plans) == 1 else "options"
    rider_word = "passenger" if n == 1 else "passengers"
    when = f", leaving at {depart:%H:%M}" if depart is not None else ""
    lines = [f"{len(result.plans)} route {plural} for you and {n} {rider_word}{when}:"]
    for i, plan in enumerate(result.plans, start=1):
        lines.append("")
        lines.append(_format_plan(i, plan, result, origin, driver_dest, rider_labels, depart))
    return "\n".join(lines)


def _format_plan(
    index: int,
    plan: MultiStopPlan,
    result: MultiOptimiseResult,
    origin: LatLng,
    driver_dest: LatLng,
    rider_labels: list[str],
    depart: datetime | None = None,
) -> str:
    header = f"Option {index}"
    if plan.tags:
        header += f" - {', '.join(plan.tags)}"

    pickup = result.direction == "pickup"
    approx = "" if plan.exact else " (approx.)"
    drive_line = (
        f"You drive: {_m(result.base_d_min)} -> {_m(plan.new_d_min)} min "
        f"({_m(plan.cost_d_min):+d}){approx}"
    )
    if pickup and plan.depart_delay_min > 0.5:
        drive_line += (
            f", setting off {_m(plan.depart_delay_min)} min later"
            f"{_at_clock(depart, plan.depart_delay_min)}"
        )
    end_name = "shared destination" if pickup else "your destination"
    route_line = "Route: " + " -> ".join(s.name for s in plan.stops) + f" -> {end_name}"

    rider_lines = []
    for i, outcome in enumerate(plan.riders):
        label = rider_labels[i]
        base = result.rider_base_min[i]
        if outcome.mode == "separate":
            if base is None:
                rider_lines.append(f"- {label}: no public-transport route found")
            else:
                rider_lines.append(f"- {label}: own way by PT - {_m(base)} min (no change)")
            continue
        if outcome.mode == "direct":
            how = "picked up at their door" if pickup else "dropped at destination"
        else:
            suffix = f" ({' / '.join(outcome.lines)})" if outcome.lines else ""
            if pickup:
                how = f"meet at {outcome.stop_name}{suffix}, {_m(outcome.pt_min)} min by PT"
            else:
                how = f"drop at {outcome.stop_name}{suffix}, then PT"
        note = ""
        if pickup and outcome.leave_after_min > 0.5:
            note = (
                f", setting off {_m(outcome.leave_after_min)} min later"
                f"{_at_clock(depart, outcome.leave_after_min)}"
            )
        if base is None or outcome.save_r_min is None:
            rider_lines.append(f"- {label}: {how} - {_m(outcome.new_r_min)} min by car{note}")
        else:
            rider_lines.append(
                f"- {label}: {how} - {_m(base)} -> {_m(outcome.new_r_min)} min "
                f"({-_m(outcome.save_r_min):+d}){note}"
            )

    if plan.net_min > 0:
        summary = f"Together you save {_m(plan.net_min)} min."
    else:
        summary = f"Costs the group {_m(-plan.net_min)} min overall - pure convenience."
    link = google_maps_link(origin, driver_dest, [s.point for s in plan.stops])

    return "\n".join([header, drive_line, route_line, *rider_lines, summary, f"Map: {link}"])


def _format_multi_separate(result: MultiOptimiseResult, rider_labels: list[str]) -> str:
    lines = [
        "Travelling separately is best here.",
        "",
        f"You drive:  {_m(result.base_d_min)} min",
    ]
    for label, base in zip(rider_labels, result.rider_base_min, strict=True):
        if base is None:
            lines.append(f"{label}: no public-transport route found")
        else:
            lines.append(f"{label} (public transport):  {_m(base)} min")
    lines += ["", "No shared route saves enough time to be worth the detour."]
    return "\n".join(lines)


def _format_separate(result: OptimiseResult) -> str:
    lines = [
        "Travelling separately is best here.",
        "",
        f"You drive:  {_m(result.base_d_min)} min",
        f"Friend (public transport):  {_m(result.base_r_min)} min",
        "",
        "No drop-off point saves enough time to be worth the detour.",
    ]
    if result.near_miss is not None:
        nm = result.near_miss
        lines += [
            "",
            f"Closest near-miss: {nm.name} "
            f"(you +{_m(nm.cost_d_min)}, friend -{_m(nm.save_r_min)}) - "
            "just outside the fairness rule.",
        ]
    return "\n".join(lines)
