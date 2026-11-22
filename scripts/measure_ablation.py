"""Measure Incremental Dirty Closure vs Full-Trip Replan scaling & stage bottlenecks.

Produces real before/after optimization evidence into eval/ablation_metrics.json.
"""

from __future__ import annotations

from datetime import date, time, timedelta
import json
from pathlib import Path
import statistics
import time as pytime

from tripgraph.graph.closure import compute_dirty_closure
from tripgraph.graph.freeze import FreezeSnapshot, assert_lock_guard, compute_blast_radius
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC
from tripgraph.runtime.orchestrator import _apply_solve_assignments, apply_direct_mutation
from tripgraph.schema.edits import MoveActivityEdit
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
    TripGraph,
    TruthValue,
)
from tripgraph.solver.cpsat_engine import solve_ir
from tripgraph.solver.ir import compile_dirty_ir
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc
from tripgraph.verifier.engine import verify_trip_graph

ROOT = Path(__file__).resolve().parents[1]
BASE_DATE = date(2026, 6, 14)


def _utc(d: date, h: int, m: int, reg: PinnedTzRegistry) -> int:
    res = resolve_local_to_utc(d, time(h, m), reg.get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def build_multiday_trip(
    num_days: int, acts_per_day: int, reg: PinnedTzRegistry
) -> tuple[TripGraph, dict[str, Place], MoveActivityEdit]:
    stay = CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Roppongi Central",
            tz_iana="Asia/Tokyo",
            check_in_date=BASE_DATE,
            check_out_date=BASE_DATE + timedelta(days=num_days + 1),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )

    cats = ("museum", "park", "cafe", "gallery")
    places: dict[str, Place] = {}
    acts: list[ActivityNode] = []

    for d_idx in range(num_days):
        local_d = BASE_DATE + timedelta(days=d_idx)
        for s_idx in range(acts_per_day):
            pid = f"p_d{d_idx}_s{s_idx}"
            cat = cats[(d_idx + s_idx) % len(cats)]
            places[pid] = Place(
                place_id=pid,
                name=f"Venue D{d_idx} S{s_idx}",
                city_id="tokyo",
                tz_iana="Asia/Tokyo",
                lat=35.66 + 0.005 * s_idx,
                lon=139.72 + 0.005 * d_idx,
                category=cat,
                estimated_cost_usd=18,
                rating=4.6,
                holiday_policy=HolidayPolicy.OPEN_NORMAL,
                evidence=(
                    EvidenceRecord(
                        record_id=f"ev_{pid}",
                        place_id=pid,
                        authority_tier=AuthorityTier.TIER_1_OFFICIAL,
                        specificity=EvidenceSpecificity.DATE_SPECIFIC,
                        verdict=EvidenceVerdict.OPEN,
                        source="ablation_fixture",
                        fetched_at_utc=DEFAULT_FIXTURE_NOW_UTC - 600,
                        ttl_sec=604800,
                        applicable_date=local_d,
                        rules=(
                            OpeningRule(
                                weekday=local_d.weekday(),
                                open_local=time(8, 0),
                                close_local=time(20, 0),
                            ),
                        ),
                    ),
                ),
            )
            start_h = 9 + s_idx * 2
            acts.append(
                ActivityNode(
                    id=f"act_d{d_idx}_s{s_idx}",
                    stay_id="s_tyo",
                    city_id="tokyo",
                    place_id=pid,
                    local_date=local_d,
                    start_utc=_utc(local_d, start_h, 0, reg),
                    end_utc=_utc(local_d, start_h, 0, reg) + 90 * 60,
                    duration_min=90,
                    category=cat,
                    locked_by_user=(s_idx == 0),  # First anchor each day is locked
                    node_tier=NodeTier.CRITICAL if s_idx == 0 else NodeTier.ESSENTIAL,
                )
            )

    graph = TripGraph(trip_id=f"ablation_{num_days}d", stays=(stay,), activities=tuple(acts))
    # Local edit on Day 1: shift act_d1_s1 into act_d1_s2's slot, forcing intra-day CP-SAT cascade
    target_d = BASE_DATE + timedelta(days=1)
    edit = MoveActivityEdit(
        activity_id="act_d1_s1",
        target_stay_id="s_tyo",
        target_local_date=target_d,
        target_start_utc=_utc(target_d, 12, 30, reg),
    )
    return graph, places, edit


def _run_once(
    graph: TripGraph,
    places: dict[str, Place],
    edit: MoveActivityEdit,
    reg: PinnedTzRegistry,
    *,
    incremental: bool,
) -> dict[str, float]:
    transit_fn = lambda a, b: 0 if a == b else 15

    t0 = pytime.perf_counter()
    if incremental:
        closure = compute_dirty_closure(graph, edit, places)
        snap = FreezeSnapshot.capture(
            graph, closure.dirty_stay_ids, closure.dirty_leg_ids, closure.dirty_activity_ids
        )
        dirty_days = closure.dirty_days
        scope_ids = closure.dirty_stay_ids | closure.dirty_leg_ids | closure.dirty_activity_ids
    else:
        closure = None
        snap = None
        dirty_days = frozenset((a.stay_id, a.local_date) for a in graph.activities)
        scope_ids = frozenset(a.id for a in graph.activities)
    t1 = pytime.perf_counter()

    mutated = apply_direct_mutation(graph, edit)
    ir = compile_dirty_ir(
        mutated, dirty_days, places, transit_fn, DEFAULT_FIXTURE_NOW_UTC, reg
    )
    t2 = pytime.perf_counter()

    res = solve_ir(ir, run_mus=False)
    assert res.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    t3 = pytime.perf_counter()

    solved = _apply_solve_assignments(mutated, res, places)
    if snap is not None:
        assert_lock_guard(snap, solved)
    rep = verify_trip_graph(
        solved, places, transit_fn, DEFAULT_FIXTURE_NOW_UTC, reg
    )
    assert rep.hard_constraints_status != TruthValue.FALSE
    t4 = pytime.perf_counter()

    br = compute_blast_radius(graph, solved, logical_scope_ids=scope_ids)
    return {
        "closure_ms": (t1 - t0) * 1000.0,
        "compile_ms": (t2 - t1) * 1000.0,
        "solve_ms": (t3 - t2) * 1000.0,
        "verify_ms": (t4 - t3) * 1000.0,
        "total_ms": (t4 - t0) * 1000.0,
        "dirty_acts": float(len(ir.activities)),
        "semantic_blast": br.semantic_blast_radius,
    }


def main() -> None:
    reg = PinnedTzRegistry()
    repeats = 10
    results = []

    print(f"{'Days':>4} | {'Acts':>4} | {'Inc p50 (ms)':>12} | {'Full p50 (ms)':>13} | {'Speedup':>7} | {'Compile/Solve/Verify (Inc)'}")
    print("-" * 82)

    for num_days in (3, 5, 7, 10):
        graph, places, edit = build_multiday_trip(num_days, 4, reg)
        # Warm-up 1 run per mode
        _run_once(graph, places, edit, reg, incremental=True)
        _run_once(graph, places, edit, reg, incremental=False)

        inc_runs = [_run_once(graph, places, edit, reg, incremental=True) for _ in range(repeats)]
        full_runs = [_run_once(graph, places, edit, reg, incremental=False) for _ in range(repeats)]

        def _agg(runs: list[dict[str, float]]) -> dict[str, float]:
            keys = ("closure_ms", "compile_ms", "solve_ms", "verify_ms", "total_ms")
            out = {k: round(statistics.median(r[k] for r in runs), 2) for k in keys}
            totals = sorted(r["total_ms"] for r in runs)
            out["total_p95_ms"] = round(totals[int(0.95 * (len(totals) - 1))], 2)
            out["dirty_acts"] = int(runs[0]["dirty_acts"])
            out["semantic_blast"] = runs[0]["semantic_blast"]
            return out

        inc_agg = _agg(inc_runs)
        full_agg = _agg(full_runs)
        speedup = round(full_agg["total_ms"] / max(0.01, inc_agg["total_ms"]), 2)

        results.append(
            {
                "num_days": num_days,
                "total_activities": len(graph.activities),
                "repeats": repeats,
                "incremental": inc_agg,
                "full_replan": full_agg,
                "speedup_x": speedup,
            }
        )
        print(
            f"{num_days:>4} | {len(graph.activities):>4} | "
            f"{inc_agg['total_ms']:>12.2f} | {full_agg['total_ms']:>13.2f} | "
            f"{speedup:>6.2f}x | "
            f"{inc_agg['compile_ms']:.1f} / {inc_agg['solve_ms']:.1f} / {inc_agg['verify_ms']:.1f} ms"
        )

    out_path = ROOT / "eval" / "ablation_metrics.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
