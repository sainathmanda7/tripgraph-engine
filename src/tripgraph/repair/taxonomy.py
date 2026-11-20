"""Deterministic controls: Frozen S2_taxonomy ontology expander & S2_greedy scheduler.

Both operate at 0 LLM tokens and share the exact same BudgetedProviderProxy
retrieval budgets and verification policies as S3 (Phase B).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from tripgraph.grounding.fixtures import haversine_km
from tripgraph.grounding.provider import (
    BudgetedProviderProxy,
    CandidateSearchIntent,
)
from tripgraph.schema.models import (
    ActivityNode,
    Place,
    TripGraph,
    VerificationPolicy,
)
from tripgraph.solver.ir import (
    CompiledConstraintIR,
    extract_day_open_windows_min,
    is_epistemic_allowed,
)
from tripgraph.time.tz_registry import PinnedTzRegistry
from tripgraph.verifier.evidence import resolve_place_availability

# Frozen v1 taxonomy ontology (hashed into eval/FROZEN_CONTROLS.lock before held-out runs)
TAXONOMY_V1: dict[str, dict[str, float]] = {
    "contemporary_art": {
        "contemporary_art": 1.00,
        "art_gallery": 0.85,
        "design_museum": 0.80,
        "craft_gallery": 0.72,
        "interactive_art": 0.65,
    },
    "art_gallery": {
        "art_gallery": 1.00,
        "contemporary_art": 0.85,
        "design_museum": 0.78,
        "craft_gallery": 0.75,
    },
    "design_museum": {
        "design_museum": 1.00,
        "contemporary_art": 0.80,
        "craft_gallery": 0.80,
        "art_gallery": 0.75,
    },
    "craft_gallery": {
        "craft_gallery": 1.00,
        "design_museum": 0.80,
        "art_gallery": 0.75,
        "contemporary_art": 0.70,
    },
    "museum": {
        "museum": 1.00,
        "contemporary_art": 0.85,
        "art_gallery": 0.85,
        "design_museum": 0.80,
        "craft_gallery": 0.75,
    },
    "park": {"park": 1.00, "shrine": 0.70, "cafe": 0.40},
    "shrine": {"shrine": 1.00, "park": 0.75, "craft_gallery": 0.50},
    "food": {"food": 1.00, "cafe": 0.65},
    "cafe": {"cafe": 1.00, "food": 0.65, "craft_gallery": 0.45},
}

RANKER_WEIGHTS_V1 = {
    "tax_sim": 0.45,
    "detour": 0.30,
    "rating": 0.15,
    "same_day_dup": 0.10,
}


def taxonomy_controls_sha256() -> str:
    payload = {"taxonomy": TAXONOMY_V1, "weights": RANKER_WEIGHTS_V1}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expand_candidates_deterministic(
    target_act: ActivityNode,
    graph: TripGraph,
    proxy: BudgetedProviderProxy,
    place_registry: dict[str, Place],
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    *,
    verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
    public_holidays: frozenset = frozenset(),
) -> list[tuple[str, int]]:
    """S2_taxonomy: deterministic ontology broadening + multi-factor ranking (0 LLM tokens).

    Returns list of (place_id, rank_penalty) capped at proxy.max_candidates_to_cpsat.
    """
    orig_place = place_registry.get(target_act.place_id)
    orig_cat = orig_place.category if orig_place is not None else target_act.category
    tax_map = TAXONOMY_V1.get(orig_cat, {orig_cat: 1.0})

    # Exclude all currently used place_ids across the entire trip
    excluded = frozenset(a.place_id for a in graph.activities)
    same_day_cats = {
        a.category
        for a in graph.activities
        if a.stay_id == target_act.stay_id
        and a.local_date == target_act.local_date
        and a.id != target_act.id
    }

    intent = CandidateSearchIntent(
        city_id=target_act.city_id,
        target_categories=tuple(tax_map.keys()),
        required_dietary_tags=(
            graph.constraints.required_dietary_tags if target_act.is_meal else ()
        ),
        excluded_place_ids=excluded,
        near_lat=orig_place.lat if orig_place else None,
        near_lon=orig_place.lon if orig_place else None,
    )
    records = proxy.search_candidates(intent)

    scored: list[tuple[float, str]] = []
    for rec in records:
        p = rec.payload
        # Epistemic pre-check on target date
        windows = extract_day_open_windows_min(
            p, target_act.local_date, target_act.start_utc, tz_registry
        )
        if not windows:
            continue
        probe_s = target_act.start_utc + windows[0][0] * 60
        probe_e = probe_s + max(target_act.duration_min, 1) * 60
        avail = resolve_place_availability(
            p, probe_s, probe_e, now_utc, tz_registry, public_holidays=public_holidays
        )
        if not is_epistemic_allowed(avail.truth, avail.epistemic_code, verification_policy):
            continue

        tax_sim = tax_map.get(p.category, 0.3)
        if orig_place is not None:
            dist_km = haversine_km(orig_place.lat, orig_place.lon, p.lat, p.lon)
            norm_detour = min(1.0, dist_km / 10.0)
        else:
            norm_detour = 0.0
        norm_rating = p.rating / 5.0
        same_day_dup = 1.0 if p.category in same_day_cats else 0.0

        score = (
            RANKER_WEIGHTS_V1["tax_sim"] * tax_sim
            - RANKER_WEIGHTS_V1["detour"] * norm_detour
            + RANKER_WEIGHTS_V1["rating"] * norm_rating
            - RANKER_WEIGHTS_V1["same_day_dup"] * same_day_dup
        )
        scored.append((score, p.place_id))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    top_ids = proxy.record_cpsat_injection(
        [pid for _, pid in scored[: proxy.max_candidates_to_cpsat]]
    )
    # Assign small deterministic rank penalties (0, 1, 2, 3, 4)
    return [(pid, idx) for idx, pid in enumerate(top_ids)]


@dataclass(frozen=True)
class GreedySolveResult:
    feasible: bool
    assignments: dict[str, tuple[str, int, int]]  # act_id -> (place_id, start_utc, end_utc)
    failure_reason: str | None = None


def solve_ir_greedy(ir: CompiledConstraintIR) -> GreedySolveResult:
    """S2_greedy: Matched deterministic non-CP-SAT greedy temporal scheduler.

    Processes activities chronologically in original-start order, greedily locking
    each activity into the earliest feasible slot without backtracking.
    Demonstrates exactly where greedy fails on coupled locked windows while CP-SAT succeeds.
    """
    assignments: dict[str, tuple[str, int, int]] = {}
    used_places: set[str] = set(ir.globals_ir.frozen_place_ids)
    total_cost = 0

    for day in ir.day_bins:
        # Sort by original start time (classic greedy dispatch)
        ordered_ids = sorted(
            day.act_ids, key=lambda aid: (ir.activities[aid].orig_start_min, aid)
        )
        prev_end_min: int | None = None
        prev_pid: str | None = None
        day_active = 0

        for aid in ordered_ids:
            spec = ir.activities[aid]
            day_active += spec.duration_min
            if day_active > day.daily_active_limit_min:
                return GreedySolveResult(False, {}, f"DAILY_BUDGET_EXCEEDED:{aid}")

            placed = False
            for cand in spec.candidates:
                if cand.blocked_by_policy or cand.authoritatively_closed:
                    continue
                if spec.locked_by_user and cand.place_id != spec.orig_place_id:
                    continue
                if cand.place_id in used_places:
                    continue
                if spec.is_meal and ir.globals_ir.required_dietary_tags:
                    if not ir.globals_ir.required_dietary_tags.issubset(set(cand.dietary_tags)):
                        continue
                if (
                    ir.globals_ir.remaining_budget_usd is not None
                    and total_cost + cand.cost_usd > ir.globals_ir.remaining_budget_usd
                ):
                    continue

                transit = (
                    day.transit_matrix_min.get((prev_pid, cand.place_id), 0)
                    if prev_pid is not None
                    else 0
                )
                earliest_after_prev = (
                    prev_end_min + transit if prev_end_min is not None else spec.min_start_min
                )

                if spec.locked_by_user:
                    chosen_s = spec.orig_start_min
                    chosen_e = chosen_s + spec.duration_min
                    if chosen_s < earliest_after_prev:
                        continue
                    if cand.windows_min and not any(
                        o <= chosen_s and chosen_e <= c for o, c in cand.windows_min
                    ):
                        continue
                else:
                    # Greedily pick earliest valid start inside opening windows
                    chosen_s = None
                    if not cand.windows_min:
                        chosen_s = max(earliest_after_prev, spec.min_start_min)
                    else:
                        for o, c in cand.windows_min:
                            s_try = max(earliest_after_prev, o)
                            if s_try + spec.duration_min <= c:
                                chosen_s = s_try
                                break
                    if chosen_s is None:
                        continue
                    chosen_e = chosen_s + spec.duration_min

                used_places.add(cand.place_id)
                total_cost += cand.cost_usd
                prev_end_min = chosen_e
                prev_pid = cand.place_id
                assignments[aid] = (
                    cand.place_id,
                    ir.epoch_origin_utc + chosen_s * 60,
                    ir.epoch_origin_utc + chosen_e * 60,
                )
                placed = True
                break

            if not placed:
                return GreedySolveResult(False, {}, f"GREEDY_TRAPPED_AT:{aid}")

    return GreedySolveResult(True, assignments, None)
