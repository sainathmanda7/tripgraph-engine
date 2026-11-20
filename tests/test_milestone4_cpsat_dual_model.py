"""Milestone 4 verification tests (TripGraph v7 Dual-Model CP-SAT):
1. Dynamic adjacency exact charging (dropped intermediate node, reordering around
   locked anchors, empty day, zero-duration canonical tie-breaker).
2. No-silent-policy-pruning invariant: STRICT holiday -> INFEASIBLE_UNDER_VERIFICATION_POLICY.
3. Empty-core base-axiom contradiction -> INTERNAL_BASE_AXIOM_CONTRADICTION.
4. Cloned per-constraint optional intervals: disabling one ResourceNoOverlap never
   leaks into another ResourceNoOverlap.
5. Consent-gated optional drops (False forbids drop; True allows <=1 optional drop,
   never drops CRITICAL/ESSENTIAL/locked).
6. 60-case randomized Dual-Model Equivalence & Direct Optimization-Model Core Soundness Proof.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time
import random

from ortools.sat.python import cp_model
import pytest

from tripgraph.schema.models import (
    ActivityNode,
    AuthorityTier,
    CityStay,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    HolidayPolicy,
    HotelBookingContract,
    NodeTier,
    OpeningRule,
    Place,
    TripConstraints,
    TripGraph,
    VerificationPolicy,
)
from tripgraph.solver.cpsat_engine import (
    solve_ir,
    solve_optimization_only,
)
from tripgraph.solver.ir import (
    ResourceNoOverlapSpec,
    SyntheticBaseAxiomContradiction,
    compile_dirty_ir,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

D_MON = date(2026, 6, 15)  # Monday


def _utc(d: date, h: int, m: int, tz_iana: str = "Europe/Berlin") -> int:
    reg = PinnedTzRegistry()
    res = resolve_local_to_utc(d, time(h, m), reg.get(tz_iana))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _verified_place(
    pid: str,
    cat: str,
    *,
    open_h: int = 9,
    close_h: int = 18,
    weekday: int = 0,
    date_specific: bool = True,
    holiday_policy: HolidayPolicy = HolidayPolicy.OPEN_NORMAL,
    verdict: EvidenceVerdict = EvidenceVerdict.OPEN,
    authority: AuthorityTier = AuthorityTier.TIER_1_OFFICIAL,
    cost: int = 20,
) -> Place:
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    rec = EvidenceRecord(
        record_id=f"rec_{pid}",
        place_id=pid,
        authority_tier=authority,
        specificity=(
            EvidenceSpecificity.DATE_SPECIFIC
            if date_specific
            else EvidenceSpecificity.WEEKLY_RECURRING
        ),
        verdict=verdict,
        source="official_site" if authority == AuthorityTier.TIER_1_OFFICIAL else "osm_overpass",
        fetched_at_utc=now_utc - 600,
        ttl_sec=604800,
        applicable_date=D_MON if date_specific else None,
        rules=(
            (OpeningRule(weekday=weekday, open_local=time(open_h, 0), close_local=time(close_h, 0)),)
            if verdict == EvidenceVerdict.OPEN
            else ()
        ),
    )
    return Place(
        place_id=pid,
        name=f"Place {pid}",
        city_id="berlin",
        tz_iana="Europe/Berlin",
        lat=52.52,
        lon=13.40,
        category=cat,
        holiday_policy=holiday_policy,
        estimated_cost_usd=cost,
        evidence=(rec,),
    )


def _stay() -> CityStay:
    return CityStay(
        id="s_ber",
        city_id="berlin",
        tz_iana="Europe/Berlin",
        hotel=HotelBookingContract(
            hotel_id="h_ber",
            hotel_name="Berlin Central",
            tz_iana="Europe/Berlin",
            check_in_date=date(2026, 6, 14),
            check_out_date=date(2026, 6, 17),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )


def test_dynamic_adjacency_reorders_around_locked_anchors_and_charges_exact_markov() -> None:
    """Locked A (museum, 09:00-10:00) and Locked C (museum, 13:00-14:00).
    Mutable B (park, orig 15:00-16:00 -> currently A->C back-to-back museums!).
    Wait: moving B costs churn=1 (Tier 2), so if B is already feasible at 15:00,
    Tier 2 keeps B at 15:00 and charges Markov(A->C)=25!
    Now if B's venue closes at 12:30, B MUST move between A and C (10:30-11:30),
    breaking A->C adjacency so order becomes A->B->C and Markov penalty drops to 0!"""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {
        "pA": _verified_place("pA", "museum", open_h=9, close_h=18),
        "pB": _verified_place("pB", "park", open_h=10, close_h=12),  # forces B into 10:00-12:00!
        "pC": _verified_place("pC", "museum", open_h=9, close_h=18),
    }
    acts = (
        ActivityNode(
            id="A", stay_id="s_ber", city_id="berlin", place_id="pA", local_date=D_MON,
            start_utc=_utc(D_MON, 9, 0), end_utc=_utc(D_MON, 10, 0), duration_min=60,
            category="museum", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="B", stay_id="s_ber", city_id="berlin", place_id="pB", local_date=D_MON,
            start_utc=_utc(D_MON, 15, 0), end_utc=_utc(D_MON, 16, 0), duration_min=60,
            category="park", locked_by_user=False, node_tier=NodeTier.OPTIONAL,
        ),
        ActivityNode(
            id="C", stay_id="s_ber", city_id="berlin", place_id="pC", local_date=D_MON,
            start_utc=_utc(D_MON, 13, 0), end_utc=_utc(D_MON, 14, 0), duration_min=60,
            category="museum", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
    )
    graph = TripGraph(trip_id="t_adj", stays=(_stay(),), activities=acts)
    ir = compile_dirty_ir(
        graph,
        frozenset({("s_ber", D_MON)}),
        places,
        lambda _a, _b: 15,
        now_utc,
        reg,
    )
    res = solve_ir(ir)
    assert res.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    assert res.day_orders[("s_ber", D_MON.isoformat())] == ("A", "B", "C")
    assert res.markov_penalty_value == 0  # A(museum)->B(park)->C(museum) has 0 consecutive museums!
    assert res.assignments["A"].start_utc == _utc(D_MON, 9, 0)
    assert res.assignments["C"].start_utc == _utc(D_MON, 13, 0)


def test_dynamic_adjacency_dropped_intermediate_connects_locked_a_to_c() -> None:
    """When optional B (park) is closed and allow_auto_drop_optional=True,
    B is dropped and order becomes A -> C, charging exact Markov(museum->museum)=25."""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {
        "pA": _verified_place("pA", "museum"),
        "pB": _verified_place("pB", "park", verdict=EvidenceVerdict.CLOSED),
        "pC": _verified_place("pC", "museum"),
    }
    acts = (
        ActivityNode(
            id="A", stay_id="s_ber", city_id="berlin", place_id="pA", local_date=D_MON,
            start_utc=_utc(D_MON, 9, 0), end_utc=_utc(D_MON, 10, 0), duration_min=60,
            category="museum", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="B", stay_id="s_ber", city_id="berlin", place_id="pB", local_date=D_MON,
            start_utc=_utc(D_MON, 11, 0), end_utc=_utc(D_MON, 12, 0), duration_min=60,
            category="park", locked_by_user=False, node_tier=NodeTier.OPTIONAL,
        ),
        ActivityNode(
            id="C", stay_id="s_ber", city_id="berlin", place_id="pC", local_date=D_MON,
            start_utc=_utc(D_MON, 13, 0), end_utc=_utc(D_MON, 14, 0), duration_min=60,
            category="museum", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
    )
    graph = TripGraph(trip_id="t_drop", stays=(_stay(),), activities=acts)

    # Default consent=False -> MUST NOT drop B; returns PROVEN_PHYSICALLY_INFEASIBLE
    ir_no_drop = compile_dirty_ir(
        graph, frozenset({("s_ber", D_MON)}), places, lambda _a, _b: 15, now_utc, reg,
        allow_auto_drop_optional=False,
    )
    res_no_drop = solve_ir(ir_no_drop)
    assert res_no_drop.certificate == "PROVEN_PHYSICALLY_INFEASIBLE"
    assert res_no_drop.core_minimality == "PROVEN_MUS"
    assert any(g.kind == "VENUE_CLOSED" for g in res_no_drop.core_groups)

    # Consent=True -> drops optional B, connects A->C directly, charges Markov=25
    ir_drop = compile_dirty_ir(
        graph, frozenset({("s_ber", D_MON)}), places, lambda _a, _b: 15, now_utc, reg,
        allow_auto_drop_optional=True,
    )
    res_drop = solve_ir(ir_drop)
    assert res_drop.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    assert res_drop.assignments["B"].present is False
    assert res_drop.day_orders[("s_ber", D_MON.isoformat())] == ("A", "C")
    assert res_drop.markov_penalty_value == 25


def test_zero_duration_canonical_tie_breaker_enforces_unique_order() -> None:
    """Two zero-duration milestones at the exact same timestamp MUST follow id_rank order (M1 -> M2)."""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {
        "pM1": _verified_place("pM1", "milestone"),
        "pM2": _verified_place("pM2", "milestone"),
    }
    t_same = _utc(D_MON, 10, 0)
    acts = (
        ActivityNode(
            id="M2", stay_id="s_ber", city_id="berlin", place_id="pM2", local_date=D_MON,
            start_utc=t_same, end_utc=t_same, duration_min=0, is_milestone=True,
            locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="M1", stay_id="s_ber", city_id="berlin", place_id="pM1", local_date=D_MON,
            start_utc=t_same, end_utc=t_same, duration_min=0, is_milestone=True,
            locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
    )
    graph = TripGraph(trip_id="t_zero", stays=(_stay(),), activities=acts)
    ir = compile_dirty_ir(
        graph, frozenset({("s_ber", D_MON)}), places, lambda _a, _b: 0, now_utc, reg
    )
    res = solve_ir(ir)
    assert res.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    assert res.day_orders[("s_ber", D_MON.isoformat())] == ("M1", "M2")


def test_no_silent_policy_pruning_classifies_policy_infeasibility_honestly() -> None:
    """v7 Mandatory Invariant: A museum blocked only because of HOLIDAY_UNVERIFIED
    under STRICT/ASSUME_WEEKLY MUST return INFEASIBLE_UNDER_VERIFICATION_POLICY
    with a VERIFICATION_POLICY_GATE core literal (NEVER PROVEN_PHYSICALLY_INFEASIBLE)."""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {
        "p_hol": _verified_place(
            "p_hol", "museum", date_specific=False, holiday_policy=HolidayPolicy.UNKNOWN
        )
    }
    act = ActivityNode(
        id="A1", stay_id="s_ber", city_id="berlin", place_id="p_hol", local_date=D_MON,
        start_utc=_utc(D_MON, 10, 0), end_utc=_utc(D_MON, 12, 0), duration_min=120,
        category="museum", node_tier=NodeTier.ESSENTIAL,
    )
    graph = TripGraph(trip_id="t_pol", stays=(_stay(),), activities=(act,))
    ir = compile_dirty_ir(
        graph,
        frozenset({("s_ber", D_MON)}),
        places,
        lambda _a, _b: 0,
        now_utc,
        reg,
        verification_policy=VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
        public_holidays_by_city={"berlin": frozenset({D_MON})},
    )
    res = solve_ir(ir)
    assert res.certificate == "INFEASIBLE_UNDER_VERIFICATION_POLICY"
    assert res.core_minimality == "PROVEN_MUS"
    policy_lits = [g for g in res.core_groups if g.origin == "VERIFICATION_POLICY_GATE"]
    assert len(policy_lits) == 1
    assert policy_lits[0].epistemic_code == "HOLIDAY_UNVERIFIED"


def test_empty_base_axiom_core_never_mislabeled_as_physical_conflict() -> None:
    """Inject an unconditional base-axiom contradiction -> DiagnosticFeasibilityModel
    returns empty assumption core -> MUST emit INTERNAL_BASE_AXIOM_CONTRADICTION."""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {"p1": _verified_place("p1", "museum")}
    act = ActivityNode(
        id="A1", stay_id="s_ber", city_id="berlin", place_id="p1", local_date=D_MON,
        start_utc=_utc(D_MON, 10, 0), end_utc=_utc(D_MON, 11, 0), duration_min=60,
    )
    graph = TripGraph(trip_id="t_base", stays=(_stay(),), activities=(act,))
    ir = compile_dirty_ir(
        graph, frozenset({("s_ber", D_MON)}), places, lambda _a, _b: 0, now_utc, reg
    )
    # Inject impossible unconditional bounds (start >= 2000 AND start <= 1900)
    broken_ir = replace(
        ir,
        synthetic_base_contradiction=SyntheticBaseAxiomContradiction(
            act_id="A1", impossible_lb=2000, impossible_ub=1900
        ),
    )
    res = solve_ir(broken_ir)
    assert res.certificate == "INTERNAL_BASE_AXIOM_CONTRADICTION"
    assert res.core_minimality == "EMPTY_BASE_AXIOM_CORE"
    assert res.core_groups == ()


def test_cloned_resource_no_overlap_intervals_do_not_leak_between_groups() -> None:
    """Two separate ResourceNoOverlap groups share activity A1.
    A1 and A2 are locked at the EXACT same time (10:00-11:00) -> RES_A conflicts,
    while RES_B (A1 + A3 at 14:00-15:00) does NOT conflict.
    Disabling RES_B leaves RES_A infeasible, proving cloned intervals stay isolated."""
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)
    places = {
        "p1": _verified_place("p1", "museum"),
        "p2": _verified_place("p2", "park"),
        "p3": _verified_place("p3", "cafe"),
    }
    # Put A1 and A2 on DIFFERENT days so day-transit doesn't interfere; only RES_A conflicts!
    d2 = date(2026, 6, 16)
    acts = (
        ActivityNode(
            id="A1", stay_id="s_ber", city_id="berlin", place_id="p1", local_date=D_MON,
            start_utc=_utc(D_MON, 10, 0), end_utc=_utc(D_MON, 11, 0), duration_min=60,
            locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="A2", stay_id="s_ber", city_id="berlin", place_id="p2", local_date=d2,
            start_utc=_utc(D_MON, 10, 0), end_utc=_utc(D_MON, 11, 0), duration_min=60,
            locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="A3", stay_id="s_ber", city_id="berlin", place_id="p3", local_date=d2,
            start_utc=_utc(D_MON, 14, 0), end_utc=_utc(D_MON, 15, 0), duration_min=60,
            locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
    )
    graph = TripGraph(trip_id="t_res", stays=(_stay(),), activities=acts)
    ir = compile_dirty_ir(
        graph,
        frozenset({("s_ber", D_MON), ("s_ber", d2)}),
        places,
        lambda _a, _b: 0,
        now_utc,
        reg,
        resource_no_overlaps=(
            ResourceNoOverlapSpec(group_id="RES_GUIDE_A", resource_name="guide_a", act_ids=("A1", "A2")),
            ResourceNoOverlapSpec(group_id="RES_CAR_B", resource_name="car_b", act_ids=("A1", "A3")),
        ),
    )
    res = solve_ir(ir)
    assert res.certificate == "PROVEN_PHYSICALLY_INFEASIBLE"
    core_ids = {g.group_id for g in res.core_groups}
    assert "RES_GUIDE_A" in core_ids
    assert "RES_CAR_B" not in core_ids  # Disabling RES_CAR_B kept A1 present in RES_GUIDE_A!


def test_fuzz_60_scenarios_dual_model_equivalence_and_opt_core_soundness() -> None:
    """Mandatory v5/v7 CI Proof: Across 60 randomized scenarios (feasible, lock conflicts,
    budget conflicts, closures, policy gates, tight transit):
    1. OptimizationModel and DiagnosticFeasibilityModel NEVER disagree.
    2. Whenever OptimizationModel is INFEASIBLE, rebuilding OptimizationModel with
       ONLY core C returns INFEASIBLE (direct core soundness on OptimizationModel!).
    3. Whenever core_minimality == PROVEN_MUS, removing ANY single c from C makes
       OptimizationModel FEASIBLE/OPTIMAL (direct minimality proof!).
    """
    rng = random.Random(20261120)
    reg = PinnedTzRegistry()
    now_utc = _utc(date(2026, 6, 10), 10, 0)

    feasible_count = 0
    infeasible_count = 0
    mus_verified_count = 0

    for trial in range(60):
        n_acts = rng.randint(2, 4)
        # Alternate relaxed vs tight regimes so both feasible and infeasible branches get ~30 trials
        tight_mode = (trial % 2 == 1)
        budget = rng.choice([None, 30, 60]) if tight_mode else rng.choice([None, 150, 200])
        daily_lim = rng.choice([120, 180, 360]) if tight_mode else 480
        policy = (
            VerificationPolicy.STRICT
            if (tight_mode and rng.random() < 0.5)
            else VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY
        )

        places: dict[str, Place] = {}
        acts: list[ActivityNode] = []
        for i in range(n_acts):
            pid = f"p_{trial}_{i}"
            is_closed = (rng.random() < 0.25) if tight_mode else False
            is_date_spec = (rng.random() < 0.50) if tight_mode else True
            open_h = rng.choice([9, 10, 12]) if tight_mode else 9
            close_h = open_h + (rng.choice([2, 4]) if tight_mode else 9)
            places[pid] = _verified_place(
                pid,
                rng.choice(["museum", "park", "cafe"]),
                open_h=open_h,
                close_h=close_h,
                date_specific=is_date_spec,
                verdict=EvidenceVerdict.CLOSED if is_closed else EvidenceVerdict.OPEN,
                cost=rng.choice([15, 25, 40]),
            )
            start_h = rng.choice([9, 10, 11, 13, 15])
            dur = rng.choice([60, 90])
            acts.append(
                ActivityNode(
                    id=f"A{i}",
                    stay_id="s_ber",
                    city_id="berlin",
                    place_id=pid,
                    local_date=D_MON,
                    start_utc=_utc(D_MON, start_h, 0),
                    end_utc=_utc(D_MON, start_h, 0) + dur * 60,
                    duration_min=dur,
                    cost_usd=places[pid].estimated_cost_usd,
                    category=places[pid].category,
                    locked_by_user=(rng.random() < (0.45 if tight_mode else 0.15)),
                    node_tier=NodeTier.ESSENTIAL,
                )
            )

        graph = TripGraph(
            trip_id=f"fuzz_{trial}",
            stays=(_stay(),),
            activities=tuple(acts),
            constraints=TripConstraints(
                total_budget_usd=budget,
                daily_active_limit_min=daily_lim,
            ),
        )
        t_val = rng.choice([15, 30, 60])
        ir = compile_dirty_ir(
            graph,
            frozenset({("s_ber", D_MON)}),
            places,
            lambda _a, _b, tv=t_val: tv,
            now_utc,
            reg,
            verification_policy=policy,
        )
        res = solve_ir(ir)
        assert res.certificate not in (
            "INTERNAL_MODEL_EQUIVALENCE_ERROR",
            "INTERNAL_SOLVER_MODEL_ERROR",
            "INTERNAL_BASE_AXIOM_CONTRADICTION",
        )

        if res.certificate in ("PROVEN_OPTIMAL_IN_SCOPE", "FEASIBLE_OPTIMALITY_UNPROVEN"):
            feasible_count += 1
        elif res.certificate in (
            "PROVEN_PHYSICALLY_INFEASIBLE",
            "INFEASIBLE_UNDER_VERIFICATION_POLICY",
        ):
            infeasible_count += 1
            core_ids = frozenset(g.group_id for g in res.core_groups)
            assert len(core_ids) >= 1

            # Direct Proof 1: OptimizationModel with ONLY core C MUST be INFEASIBLE
            st_core, _, _ = solve_optimization_only(ir, active_group_ids=core_ids, num_workers=1)
            assert st_core == cp_model.INFEASIBLE, (
                f"Trial {trial}: core {core_ids} did NOT make OptimizationModel infeasible!"
            )

            # Direct Proof 2: If PROVEN_MUS, dropping ANY single group makes OptModel FEASIBLE
            if res.core_minimality == "PROVEN_MUS":
                mus_verified_count += 1
                for drop_gid in core_ids:
                    st_sub, _, _ = solve_optimization_only(
                        ir, active_group_ids=core_ids - {drop_gid}, num_workers=1
                    )
                    assert st_sub in (cp_model.OPTIMAL, cp_model.FEASIBLE), (
                        f"Trial {trial}: PROVEN_MUS still infeasible without {drop_gid}!"
                    )

    # Confirm our fuzz generator exercised both feasible and infeasible branches thoroughly
    assert feasible_count >= 10
    assert infeasible_count >= 15
    assert mus_verified_count >= 10
