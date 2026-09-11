"""Tests for the pure optimiser core: scoring, eligibility, and ranking.

All deterministic, times in and out. The async orchestration (calling OneMap and the
station prefilter) is covered in test_optimiser_orchestration.py.
"""

from __future__ import annotations

from droppoint.config import OptimiserConfig
from droppoint.optimiser import rank_candidates, score_candidate


def cfg(**kw) -> OptimiserConfig:
    return OptimiserConfig(**kw)


def score(name, ap, pb, pc, base_d, base_r, config, lat=1.35, lng=103.85):
    return score_candidate(
        name=name,
        lat=lat,
        lng=lng,
        base_d_min=base_d,
        base_r_min=base_r,
        drive_ap_min=ap,
        drive_pb_min=pb,
        pt_pc_min=pc,
        config=config,
    )


# --- scoring maths ------------------------------------------------------------


def test_scoring_maths():
    c = score("X", ap=10, pb=17, pc=48, base_d=20, base_r=90, config=cfg())
    assert c.new_d_min == 27
    assert c.new_r_min == 58
    assert c.cost_d_min == 7
    assert c.save_r_min == 32
    assert c.net_min == 25
    assert round(c.cost_d_frac, 3) == 0.35
    assert round(c.save_r_frac, 4) == round(32 / 90, 4)
    assert c.eligible is True


def test_net_equals_combined_time_saved():
    # net = base_D + base_R - new_D - new_R
    c = score("X", ap=10, pb=17, pc=48, base_d=20, base_r=90, config=cfg())
    assert c.net_min == (20 + 90) - c.new_d_min - c.new_r_min


# --- canonical example --------------------------------------------------------


def test_canonical_example_best_is_chosen():
    config = cfg()
    base_d, base_r = 20, 90
    cands = [
        score("Bishan MRT", ap=10, pb=17, pc=48, base_d=base_d, base_r=base_r, config=config),
        score("Toa Payoh MRT", ap=7, pb=18, pc=59, base_d=base_d, base_r=base_r, config=config),
        score("Marymount MRT", ap=6, pb=18, pc=66, base_d=base_d, base_r=base_r, config=config),
    ]
    result = rank_candidates(cands, base_d, base_r, config)

    assert result.feasible is True
    assert result.best.name == "Bishan MRT"
    # Driver about 20 -> 27 (target 27-30); rider about 90 -> 58 (target 55-65).
    assert result.best.new_d_min == 27
    assert 27 <= result.best.new_d_min <= 30
    assert 55 <= result.best.new_r_min <= 65
    # Alternatives ranked by net descending.
    assert [a.name for a in result.alternatives] == ["Toa Payoh MRT", "Marymount MRT"]
    assert result.best.net_min > result.alternatives[0].net_min


# --- on-route drop-off --------------------------------------------------------


def test_on_route_dropoff_scores_very_well():
    # P sits on the driver's way: drive(A,P)+drive(P,B) == base_D, so cost_D ~ 0.
    config = cfg()
    base_d, base_r = 20, 90
    on_route = score("On Route", ap=12, pb=8, pc=45, base_d=base_d, base_r=base_r, config=config)
    detour = score("Detour", ap=10, pb=17, pc=48, base_d=base_d, base_r=base_r, config=config)

    assert on_route.cost_d_min == 0
    assert on_route.cost_d_frac == 0.0
    assert on_route.eligible is True

    result = rank_candidates([detour, on_route], base_d, base_r, config)
    assert result.best.name == "On Route"  # higher net wins


# --- rider destination near a station -----------------------------------------


def test_dropoff_near_rider_destination_shaves_last_leg():
    # C sits right next to an interchange, so pt(P, C) is tiny and that drop-off wins.
    config = cfg()
    base_d, base_r = 20, 90
    near_c = score("Near C MRT", ap=11, pb=11, pc=6, base_d=base_d, base_r=base_r, config=config)
    elsewhere = score(
        "Elsewhere MRT", ap=8, pb=16, pc=55, base_d=base_d, base_r=base_r, config=config
    )

    assert near_c.eligible and elsewhere.eligible
    result = rank_candidates([elsewhere, near_c], base_d, base_r, config)
    assert result.best.name == "Near C MRT"
    assert result.best.pt_pc_min == 6  # short last leg because C is near the station


# --- eligibility filter -------------------------------------------------------


def test_rejects_when_rider_does_not_save():
    c = score("Bad", ap=10, pb=15, pc=85, base_d=20, base_r=90, config=cfg())
    assert c.save_r_min < 0
    assert c.eligible is False


def test_rejects_when_driver_overburdened():
    # cost_D_frac > 0.5
    c = score("TooFar", ap=20, pb=15, pc=20, base_d=20, base_r=90, config=cfg())
    assert c.cost_d_frac > 0.5
    assert c.eligible is False


def test_fairness_filter_rejects_unbalanced_even_if_net_positive():
    # +8 driver (frac 0.40) for only 10 saved (frac 0.111): net = 2 > 0 but unfair.
    config = cfg()
    c = score("Unfair", ap=10, pb=18, pc=70, base_d=20, base_r=90, config=config)
    assert c.cost_d_min == 8
    assert c.save_r_min == 10
    assert c.net_min == 2
    assert c.save_r_frac < c.cost_d_frac
    assert c.eligible is False

    # With the fairness rule disabled, the same candidate becomes eligible.
    lenient = cfg(require_fair_proportion=False)
    c2 = score("Unfair", ap=10, pb=18, pc=70, base_d=20, base_r=90, config=lenient)
    assert c2.eligible is True


# --- no eligible candidate ----------------------------------------------------


def test_no_eligible_candidate_returns_travel_separately():
    config = cfg()
    base_d, base_r = 20, 90
    cands = [
        score("Hurts", ap=10, pb=15, pc=85, base_d=base_d, base_r=base_r, config=config),
        score("TooFar", ap=20, pb=15, pc=20, base_d=base_d, base_r=base_r, config=config),
    ]
    result = rank_candidates(cands, base_d, base_r, config)
    assert result.feasible is False
    assert result.best is None
    assert result.alternatives == []
    assert result.base_d_min == 20
    assert result.base_r_min == 90


def test_near_miss_shown_only_when_flag_set():
    base_d, base_r = 20, 90
    # net > 0 but unfair -> ineligible but still helps the rider.
    args = dict(ap=10, pb=18, pc=70, base_d=base_d, base_r=base_r)
    off = score("Unfair", config=cfg(show_near_miss=False), **args)
    on = score("Unfair", config=cfg(show_near_miss=True), **args)

    assert rank_candidates([off], base_d, base_r, cfg(show_near_miss=False)).near_miss is None
    nm = rank_candidates([on], base_d, base_r, cfg(show_near_miss=True)).near_miss
    assert nm is not None and nm.name == "Unfair"


# --- objectives ---------------------------------------------------------------


def test_balanced_objective_differs_from_efficient():
    base_d, base_r = 20, 80

    # A: bigger net (26) but a fairness gap of 0.025.
    # B: smaller net (12) but a perfectly balanced gap of 0.0.
    def build(config):
        a = score("BigNet", ap=10, pb=18, pc=36, base_d=base_d, base_r=base_r, config=config)
        b = score("Balanced", ap=8, pb=16, pc=56, base_d=base_d, base_r=base_r, config=config)
        return [a, b]

    eff = cfg(objective="efficient")
    bal = cfg(objective="balanced")

    a, b = build(eff)
    assert a.net_min == 26 and b.net_min == 12
    assert a.eligible and b.eligible

    eff_result = rank_candidates(build(eff), base_d, base_r, eff)
    bal_result = rank_candidates(build(bal), base_d, base_r, bal)

    assert eff_result.best.name == "BigNet"
    assert bal_result.best.name == "Balanced"
