"""Milestone 3 verification tests:
1. Exhaustive 2^n partition & candidate-assignment equivalence for all EXACT
   global hard constraints and soft objectives (including dynamic topology
   reordering around locked anchors and node drops).
2. Factor-graph dirty closure (day-bin saturation, interface cascade, zero-unlocked
   guard, >50% global escalation, dietary update invalidation).
3. SHA-256 Freeze LockGuard & Semantic vs Temporal Blast Radius.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from tripgraph.graph.closure import compute_dirty_closure, widen_to_horizon1
from tripgraph.graph.freeze import (
    FreezeSnapshot,
    LockViolationError,
    assert_lock_guard,
    compute_blast_radius,
)
from tripgraph.graph.projections import (
    AntiDuplicationProjection,
    GlobalDietaryProjection,
    IntraDayMarkovAdjacencyProjection,
    MaxPerCategoryProjection,
    OncePerTripTagProjection,
    OrderInvariantCategoryPenaltyProjection,
    TotalBudgetProjection,
    TripPacingVarianceObjective,
)
from tripgraph.schema.edits import (
    DropCityEdit,
    ExtendStayEdit,
    MoveActivityEdit,
    UpdateDietaryEdit,
)
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    Place,
    TransitLeg,
    TransitMode,
    TripGraph,
)

BASE_UTC = 1_781_251_200  # 2026-06-12T08:00:00Z


def _act(
    aid: str,
    *,
    stay_id: str = "s1",
    day: date = date(2026, 6, 12),
    offset_h: int = 0,
    place_id: str = "p1",
    cat: str = "museum",
    cost: int = 20,
    tags: tuple[str, ...] = (),
    is_meal: bool = False,
    locked: bool = False,
) -> ActivityNode:
    start = BASE_UTC + offset_h * 3600
    return ActivityNode(
        id=aid,
        stay_id=stay_id,
        city_id="tokyo" if stay_id == "s1" else "kyoto",
        place_id=place_id,
        local_date=day,
        start_utc=start,
        end_utc=start + 3600,
        duration_min=60,
        cost_usd=cost,
        category=cat,
        tags=tags,
        is_meal=is_meal,
        locked_by_user=locked,
    )


def _sample_two_city_trip() -> tuple[TripGraph, dict[str, Place]]:
    s1 = CityStay(
        id="s1",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Tokyo Hotel",
            tz_iana="Asia/Tokyo",
            check_in_date=date(2026, 6, 12),
            check_out_date=date(2026, 6, 14),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    s2 = CityStay(
        id="s2",
        city_id="kyoto",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_kyo",
            hotel_name="Kyoto Hotel",
            tz_iana="Asia/Tokyo",
            check_in_date=date(2026, 6, 14),
            check_out_date=date(2026, 6, 16),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    leg = TransitLeg(
        id="leg_1",
        from_stay_id="s1",
        to_stay_id="s2",
        origin_place_id="tyo_stn",
        dest_place_id="kyo_stn",
        mode=TransitMode.RAIL,
        depart_utc=BASE_UTC + 48 * 3600,
        arrive_utc=BASE_UTC + 50 * 3600,
        carrier_slot_id="nozomi_10",
    )
    acts = (
        _act("a1", stay_id="s1", day=date(2026, 6, 12), offset_h=0, place_id="p1", cat="museum", locked=True),
        _act("a2", stay_id="s1", day=date(2026, 6, 12), offset_h=2, place_id="p2", cat="park"),
        _act("a3", stay_id="s1", day=date(2026, 6, 12), offset_h=4, place_id="p3", cat="food", is_meal=True),
        _act("a4", stay_id="s1", day=date(2026, 6, 13), offset_h=24, place_id="p4", cat="museum"),
        _act("a5", stay_id="s2", day=date(2026, 6, 14), offset_h=52, place_id="p5", cat="shrine"),
        _act("a6", stay_id="s2", day=date(2026, 6, 15), offset_h=76, place_id="p6", cat="food", is_meal=True),
    )
    places = {
        "p3": Place(
            place_id="p3", name="Ramen Shop", city_id="tokyo", tz_iana="Asia/Tokyo",
            lat=35.68, lon=139.76, category="food", dietary_tags=("vegetarian",),
        ),
        "p6": Place(
            place_id="p6", name="Vegan Kaiseki", city_id="kyoto", tz_iana="Asia/Tokyo",
            lat=35.01, lon=135.76, category="food", dietary_tags=("vegan", "vegetarian"),
        ),
    }
    graph = TripGraph(trip_id="t1", stays=(s1, s2), transit_legs=(leg,), activities=acts)
    return graph, places


# ---------------------------------------------------------------------------
# 1. Exhaustive 2^n Hard-Constraint Projection Equivalence Tests
# ---------------------------------------------------------------------------


def test_exhaustive_hard_constraint_projection_equivalence() -> None:
    """Verify evaluate_full(F, D) == evaluate_projected(summary(F), D) across ALL
    2^4 frozen/dirty partitions and ALL discrete candidate assignments."""
    places = {
        "p_v": Place(
            place_id="p_v", name="Vegan Cafe", city_id="tokyo", tz_iana="Asia/Tokyo",
            lat=35.68, lon=139.76, category="food", dietary_tags=("vegan",),
        ),
        "p_m": Place(
            place_id="p_m", name="Steakhouse", city_id="tokyo", tz_iana="Asia/Tokyo",
            lat=35.68, lon=139.76, category="food", dietary_tags=(),
        ),
        "p_art": Place(
            place_id="p_art", name="Art Hall", city_id="tokyo", tz_iana="Asia/Tokyo",
            lat=35.68, lon=139.76, category="museum",
        ),
    }
    specs = [
        TotalBudgetProjection(total_budget_usd=65),
        AntiDuplicationProjection(),
        MaxPerCategoryProjection(max_per_category={"museum": 2, "food": 2}),
        OncePerTripTagProjection(once_tags=frozenset({"tea_ceremony"})),
        GlobalDietaryProjection(required_dietary_tags=frozenset({"vegan"}), place_registry=places),
    ]

    # Candidate pool for each node slot
    slot_candidates = [
        ("p_v", "food", 20, ("tea_ceremony",), True),
        ("p_m", "food", 35, (), True),
        ("p_art", "museum", 15, (), False),
    ]
    n = 4
    base_nodes = [
        _act(f"n{i}", offset_h=i * 2, place_id=f"orig_{i}", cat="museum", cost=15)
        for i in range(n)
    ]

    checked_states = 0
    # Enumerate ALL 2^4 = 16 frozen/dirty partitions
    for mask in range(1 << n):
        frozen_idx = [i for i in range(n) if not (mask & (1 << i))]
        dirty_idx = [i for i in range(n) if (mask & (1 << i))]

        # Test with two frozen assignments (one clean, one with duplicate/tight budget)
        for frozen_variant in (0, 1):
            frozen_acts = tuple(
                _act(
                    f"n{i}",
                    offset_h=i * 2,
                    place_id="p_art" if (frozen_variant == 1 and i == frozen_idx[0]) else f"f_{i}",
                    cat="museum" if i % 2 == 0 else "food",
                    cost=25 if frozen_variant == 1 else 15,
                    tags=("tea_ceremony",) if (frozen_variant == 1 and i == frozen_idx[0]) else (),
                    is_meal=(i % 2 == 1),
                )
                for i in frozen_idx
            )
            # Ensure frozen meals use known place IDs for dietary spec
            frozen_acts = tuple(
                a.model_copy(update={"place_id": "p_v"}) if a.is_meal else a
                for a in frozen_acts
            )

            summaries = [spec.extract_summary(frozen_acts) for spec in specs]

            # Enumerate ALL 3^|D| candidate assignments for dirty nodes
            for combo in itertools.product(slot_candidates, repeat=len(dirty_idx)):
                dirty_acts = tuple(
                    _act(
                        f"n{idx}",
                        offset_h=idx * 2,
                        place_id=pid,
                        cat=cat,
                        cost=cost,
                        tags=tags,
                        is_meal=is_meal,
                    )
                    for idx, (pid, cat, cost, tags, is_meal) in zip(dirty_idx, combo)
                )
                full_acts = frozen_acts + dirty_acts
                for spec, summary in zip(specs, summaries):
                    full_val = spec.evaluate_full(full_acts)
                    proj_val = spec.evaluate_projected(summary, dirty_acts)
                    assert full_val == proj_val, (
                        f"Projection mismatch in {spec.name} for mask={mask:04b}"
                    )
                checked_states += 1

    assert checked_states == sum(3 ** bin(m).count("1") for m in range(1 << n)) * 2


# ---------------------------------------------------------------------------
# 2. Exhaustive Dynamic-Topology Soft-Objective Equivalence Tests
# ---------------------------------------------------------------------------


def test_exhaustive_dynamic_topology_markov_and_quota_equivalence() -> None:
    """Proves exact difference invariance across:
    - Frozen A (locked 08:00) -> Dirty B -> Frozen C (locked 12:00)
    - Reordering B after C: A -> C -> B
    - Dropping B entirely: A -> C
    - Moving B across days to Day 2
    """
    d1, d2, d3 = date(2026, 6, 12), date(2026, 6, 13), date(2026, 6, 14)

    # Untouched frozen Day 3 nodes (must fold strictly into constant C(frozen))
    untouched_d3 = (
        _act("f_d3_1", day=d3, offset_h=48, place_id="u1", cat="museum"),
        _act("f_d3_2", day=d3, offset_h=50, place_id="u2", cat="museum"),
        _act("f_d3_3", day=d3, offset_h=52, place_id="u3", cat="park"),
    )
    # Locked anchors sitting inside dirty Day 1 & Day 2
    locked_a = _act("A_lock", day=d1, offset_h=0, place_id="pa", cat="museum", locked=True)
    locked_c = _act("C_lock", day=d1, offset_h=4, place_id="pc", cat="museum", locked=True)
    locked_d2 = _act("D2_lock", day=d2, offset_h=24, place_id="pd2", cat="park", locked=True)

    dirty_days = frozenset({("s1", d1), ("s1", d2)})
    frozen_all = untouched_d3 + (locked_a, locked_c, locked_d2)

    soft_specs = [
        IntraDayMarkovAdjacencyProjection(same_category_penalty=25),
        OrderInvariantCategoryPenaltyProjection(weight=10),
    ]
    summaries = [spec.extract_summary(frozen_all, dirty_days) for spec in soft_specs]

    # Candidate states for Dirty B: dropped OR placed on d1/d2 at before-A, between-A-C, after-C, with 3 categories
    b_states: list[tuple[ActivityNode, ...]] = [()]  # dropped B -> A directly precedes C!
    for day, base_off in ((d1, 0), (d2, 24)):
        for rel_h in (-2, 2, 6):  # before A, between A & C, after C
            for cat in ("museum", "park", "food"):
                b_states.append(
                    (_act("B_dirty", day=day, offset_h=base_off + rel_h, place_id="pb", cat=cat),)
                )

    # Reference assignment x_D^(0): B dropped
    ref_dirty_day_acts = (locked_a, locked_c, locked_d2)
    ref_full_acts = untouched_d3 + ref_dirty_day_acts

    for spec, summary in zip(soft_specs, summaries):
        ref_full = spec.evaluate_full(ref_full_acts)
        ref_proj = spec.evaluate_projected(summary, ref_dirty_day_acts)
        for b_tuple in b_states:
            cur_dirty_day_acts = (locked_a, locked_c, locked_d2) + b_tuple
            cur_full_acts = untouched_d3 + cur_dirty_day_acts

            full_diff = spec.evaluate_full(cur_full_acts) - ref_full
            proj_diff = spec.evaluate_projected(summary, cur_dirty_day_acts) - ref_proj
            assert full_diff == proj_diff, (
                f"Dynamic topology equivalence failed for {spec.name}: "
                f"full_diff={full_diff} != proj_diff={proj_diff}"
            )


def test_non_projectable_objective_is_tagged() -> None:
    obj = TripPacingVarianceObjective()
    assert obj.projection_class == "NON_PROJECTABLE"


# ---------------------------------------------------------------------------
# 3. Factor-Graph Closure, Escalation & LockGuard Tests
# ---------------------------------------------------------------------------


def test_day_bin_saturation_pulls_unlocked_siblings_and_pins_locked_nodes() -> None:
    """Editing a2 on Jun 12 must pull unlocked sibling a3 into dirty_activity_ids
    (even though Jun 12 is currently feasible), pin locked a1, and keep Jun 13 + Kyoto frozen."""
    graph, places = _sample_two_city_trip()
    edit = MoveActivityEdit(
        activity_id="a2",
        target_stay_id="s1",
        target_local_date=date(2026, 6, 12),
        target_start_utc=BASE_UTC + 3 * 3600,
    )
    res = compute_dirty_closure(graph, edit, places)

    assert res.scope_tier == "DAY_BIN"
    assert res.dirty_activity_ids == frozenset({"a2", "a3"})
    assert res.pinned_day_activity_ids == frozenset({"a1"})
    # Tokyo Day 2 (a4) and Kyoto (a5, a6) stay completely untouched
    assert "a4" not in res.dirty_activity_ids
    assert "a5" not in res.dirty_activity_ids
    assert res.dirty_ratio is not None and res.dirty_ratio < 0.50


def test_dirty_ratio_over_50_percent_escalates_to_global_replan() -> None:
    graph, places = _sample_two_city_trip()
    # Extending Tokyo cascades into Kyoto (invalidating >50% of unlocked nodes)
    edit = ExtendStayEdit(stay_id="s1", delta_nights=1)
    res = compute_dirty_closure(graph, edit, places, max_dirty_ratio=0.50)

    assert res.scope_tier == "GLOBAL_REPLAN"
    assert res.escalation_reason is not None
    assert "DIRTY_RATIO_EXCEEDED" in res.escalation_reason


def test_all_locked_trip_never_divides_by_zero() -> None:
    graph, places = _sample_two_city_trip()
    locked_graph = graph.model_copy(
        update={
            "stays": tuple(s.model_copy(update={"locked_by_user": True}) for s in graph.stays),
            "transit_legs": tuple(l.model_copy(update={"locked_by_user": True}) for l in graph.transit_legs),
            "activities": tuple(a.model_copy(update={"locked_by_user": True}) for a in graph.activities),
        }
    )
    edit = ExtendStayEdit(stay_id="s1", delta_nights=1)
    res = compute_dirty_closure(locked_graph, edit, places)

    assert res.scope_tier == "ALL_LOCKED_VERIFY_ONLY"
    assert res.unlocked_total == 0
    assert res.dirty_ratio is None


def test_update_dietary_pulls_violating_frozen_meals_only() -> None:
    """Requiring 'vegan' pulls Tokyo ramen shop (a3, vegetarian only) into dirty set,
    while Kyoto vegan kaiseki (a6) stays frozen."""
    graph, places = _sample_two_city_trip()
    edit = UpdateDietaryEdit(required_dietary_tags=("vegan",))
    res = compute_dirty_closure(graph, edit, places)

    assert res.scope_tier == "DAY_BIN"
    assert "a3" in res.dirty_activity_ids
    assert "a6" not in res.dirty_activity_ids


def test_intercity_markov_drop_city_escalates_as_non_projectable() -> None:
    graph, places = _sample_two_city_trip()
    res = compute_dirty_closure(
        graph,
        DropCityEdit(stay_id="s2"),
        places,
        has_intercity_markov_objective=True,
    )
    assert res.scope_tier == "GLOBAL_REPLAN"
    assert res.escalation_reason == "NON_PROJECTABLE_INTERCITY_TOPOLOGY_MUTATION"


def test_horizon1_widens_stay_and_respects_max_cities() -> None:
    graph, places = _sample_two_city_trip()
    edit = MoveActivityEdit(
        activity_id="a2", target_stay_id="s1", target_local_date=date(2026, 6, 12)
    )
    h0 = compute_dirty_closure(graph, edit, places)
    h1 = widen_to_horizon1(graph, h0, frozenset({"s1"}), max_dirty_ratio=0.75)

    assert h1.scope_tier == "STAY_WIDENED"
    assert "a4" in h1.dirty_activity_ids  # Day 2 of Tokyo pulled in
    assert "a5" not in h1.dirty_activity_ids  # Kyoto stays frozen


def test_sha256_lock_guard_blocks_frozen_and_user_locked_mutations() -> None:
    graph, places = _sample_two_city_trip()
    edit = MoveActivityEdit(
        activity_id="a2", target_stay_id="s1", target_local_date=date(2026, 6, 12)
    )
    closure = compute_dirty_closure(graph, edit, places)
    snap = FreezeSnapshot.capture(
        graph, closure.dirty_stay_ids, closure.dirty_leg_ids, closure.dirty_activity_ids
    )

    # Allowed: mutating dirty node a2 passes LockGuard
    valid_cand = graph.model_copy(
        update={
            "activities": tuple(
                a.model_copy(update={"start_utc": a.start_utc + 1800, "end_utc": a.end_utc + 1800})
                if a.id == "a2"
                else a
                for a in graph.activities
            )
        }
    )
    assert_lock_guard(snap, valid_cand)

    # Forbidden: mutating frozen Kyoto node a5 fails LockGuard
    bad_frozen = graph.model_copy(
        update={
            "activities": tuple(
                a.model_copy(update={"place_id": "p_hacked"}) if a.id == "a5" else a
                for a in graph.activities
            )
        }
    )
    with pytest.raises(LockViolationError, match="FROZEN_ACTIVITY_MUTATED:a5"):
        assert_lock_guard(snap, bad_frozen)

    # Forbidden: dropping user-locked node a1 fails LockGuard
    bad_locked = graph.model_copy(
        update={"activities": tuple(a for a in graph.activities if a.id != "a1")}
    )
    with pytest.raises(LockViolationError, match="USER_LOCKED_ACTIVITY_DROPPED:a1"):
        assert_lock_guard(snap, bad_locked)

    # Blast radius accurately detects the single semantic mutation on a5
    br = compute_blast_radius(graph, bad_frozen, logical_scope_ids=frozenset({"a2", "a3"}))
    assert br.semantic_changed_count == 1
    assert br.temporal_changed_count == 0
    assert br.changed_node_ids == ("a5",)
