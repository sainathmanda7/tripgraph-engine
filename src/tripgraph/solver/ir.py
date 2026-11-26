"""Shared CompiledConstraintIR for Dual-Model CP-SAT Compilation (TripGraph v7).

Core invariants:
1. Both OptimizationModel and DiagnosticFeasibilityModel compile from this
   single immutable IR so their feasibility spaces are mathematically identical.
2. Every verification-policy exclusion is represented as an explicit
   VERIFICATION_POLICY_GATE assumption group (NEVER silently pruned from domains).
3. All primitives are natively reifiable with OnlyEnforceIf(lit) (no AddAllDifferent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from tripgraph.schema.models import (
    ActivityNode,
    NodeTier,
    Place,
    TripGraph,
    TruthValue,
    VerificationPolicy,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc
from tripgraph.verifier.evidence import (
    materialize_rule_utc_interval,
    resolve_place_availability,
)

ConstraintOrigin = Literal[
    "HARD_PHYSICAL_OR_CONTRACTUAL",
    "VERIFICATION_POLICY_GATE",
]


@dataclass(frozen=True)
class AssumptionGroupSpec:
    """Single-owner reifiable constraint group for diagnostic explanations."""

    group_id: str
    origin: ConstraintOrigin
    kind: str
    node_ids: tuple[str, ...]
    detail: str
    epistemic_code: str | None = None


@dataclass(frozen=True)
class CandidatePlaceOption:
    """One grounded venue option in an activity's CP-SAT domain."""

    place_id: str
    category: str
    cost_usd: int
    tags: tuple[str, ...]
    dietary_tags: tuple[str, ...]
    windows_min: tuple[tuple[int, int], ...]  # Concrete [open_min, close_min] relative to epoch_origin_utc
    truth: TruthValue
    epistemic_code: str
    blocked_by_policy: bool
    authoritatively_closed: bool
    rank_penalty: int = 0  # Small soft preference favoring higher-ranked candidates


@dataclass(frozen=True)
class ActivityVarSpec:
    """IR specification for one activity sitting inside a dirty day."""

    act_id: str
    stay_id: str
    local_date: date
    id_rank: int
    orig_start_min: int
    orig_place_id: str
    duration_min: int
    is_milestone: bool
    is_meal: bool
    node_tier: NodeTier
    locked_by_user: bool
    min_start_min: int
    max_start_min: int
    candidates: tuple[CandidatePlaceOption, ...]


@dataclass(frozen=True)
class DayBinIR:
    """All nodes (mutable + locked anchors) on one dirty (stay_id, local_date)."""

    stay_id: str
    local_date: date
    act_ids: tuple[str, ...]
    daily_active_limit_min: int
    transit_matrix_min: dict[tuple[str, str], int]
    same_category_penalty: int = 25


@dataclass(frozen=True)
class ResourceNoOverlapSpec:
    """Explicit scheduling resource exclusivity constraint (compiled with cloned intervals)."""

    group_id: str
    resource_name: str
    act_ids: tuple[str, ...]


@dataclass(frozen=True)
class GlobalProjectionIR:
    """Exact projected trip-global constraints over frozen nodes."""

    remaining_budget_usd: int | None
    frozen_place_ids: frozenset[str]
    remaining_cat_quotas: dict[str, int]
    used_once_tags: frozenset[str]
    once_per_trip_tags: frozenset[str]
    required_dietary_tags: frozenset[str]
    allow_auto_drop_optional: bool = False
    max_optional_drops_per_day: int = 1


@dataclass(frozen=True)
class SyntheticBaseAxiomContradiction:
    """Test-only hook allowing CI to inject an unconditional base-axiom contradiction."""

    act_id: str
    impossible_lb: int
    impossible_ub: int


@dataclass(frozen=True)
class CompiledConstraintIR:
    """Complete immutable IR consumed by OptimizationModel and DiagnosticFeasibilityModel."""

    epoch_origin_utc: int
    activities: dict[str, ActivityVarSpec]
    day_bins: tuple[DayBinIR, ...]
    resource_no_overlaps: tuple[ResourceNoOverlapSpec, ...]
    globals_ir: GlobalProjectionIR
    groups: dict[str, AssumptionGroupSpec]
    synthetic_base_contradiction: SyntheticBaseAxiomContradiction | None = None


def is_epistemic_allowed(
    truth: TruthValue,
    epistemic_code: str,
    policy: VerificationPolicy,
) -> bool:
    """Determine whether an UNKNOWN epistemic code is permitted under policy."""
    if truth == TruthValue.TRUE:
        return True
    if truth == TruthValue.FALSE:
        return False
    # Community-reported closures, conflicts, stale, and unparseable are blocked under ALL policies
    if epistemic_code in (
        "COMMUNITY_REPORTED_CLOSED",
        "CONFLICTING_EVIDENCE_ON_FRONTIER",
        "STALE_CLOSED_UNCONFIRMED",
        "STALE_OPEN_ASSUMED",
        "NO_USABLE_EVIDENCE",
    ):
        return False
    if policy == VerificationPolicy.STRICT:
        return False
    if policy == VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY:
        return epistemic_code in (
            "WEEKLY_SCHEDULE_ASSUMED",
            "UNCORROBORATED_MIXED_EVIDENCE",
            "COMMUNITY_DATE_UNCORROBORATED",
        )
    if policy == VerificationPolicy.ALLOW_UNVERIFIED_HOLIDAYS:
        return epistemic_code in (
            "WEEKLY_SCHEDULE_ASSUMED",
            "UNCORROBORATED_MIXED_EVIDENCE",
            "COMMUNITY_DATE_UNCORROBORATED",
            "HOLIDAY_UNVERIFIED",
        )
    return False


def extract_day_open_windows_min(
    place: Place,
    local_d: date,
    epoch_origin_utc: int,
    tz_registry: PinnedTzRegistry,
) -> tuple[tuple[int, int], ...]:
    """Materialize concrete open intervals [open_min, close_min] for local_d from place evidence."""
    pinned_tz = tz_registry.get(place.tz_iana)
    windows: list[tuple[int, int]] = []

    day_start_res = resolve_local_to_utc(local_d, _t(0, 0), pinned_tz)
    day_end_res = resolve_local_to_utc(local_d + timedelta(days=1), _t(0, 0), pinned_tz)
    assert day_start_res.utc_epoch is not None and day_end_res.utc_epoch is not None

    for rec in place.evidence:
        if rec.verdict.value != "OPEN":
            continue
        if rec.applicable_date is not None and rec.applicable_date != local_d:
            continue
        if not rec.rules:
            # All-day verified OPEN record without restricted hourly rules
            o_min = (day_start_res.utc_epoch - epoch_origin_utc) // 60
            c_min = (day_end_res.utc_epoch - epoch_origin_utc) // 60
            windows.append((o_min, c_min))
            continue
        for rule in rec.rules:
            if rule.weekday != local_d.weekday():
                continue
            span = materialize_rule_utc_interval(local_d, rule, pinned_tz)
            if span is None:
                continue
            o_utc, c_utc = span
            windows.append(((o_utc - epoch_origin_utc) // 60, (c_utc - epoch_origin_utc) // 60))

    # Deduplicate while preserving deterministic sorted order
    return tuple(sorted(set(windows)))


def _t(h: int, m: int):
    from datetime import time
    return time(h, m)


def compile_dirty_ir(
    graph: TripGraph,
    dirty_days: frozenset[tuple[str, date]],
    place_registry: dict[str, Place],
    transit_minutes_fn,
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    *,
    verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
    extra_candidates_by_act: dict[str, list[tuple[str, int]]] | None = None,
    public_holidays_by_city: dict[str, frozenset[date]] | None = None,
    allow_auto_drop_optional: bool = False,
    same_category_penalty: int = 25,
    resource_no_overlaps: tuple[ResourceNoOverlapSpec, ...] = (),
) -> CompiledConstraintIR:
    """Compile dirty days + exact frozen summaries into a single CompiledConstraintIR."""
    holidays_map = public_holidays_by_city or {}
    extra_map = extra_candidates_by_act or {}
    stays_by_id = {s.id: s for s in graph.stays}

    # Partition activities into dirty-day nodes vs strictly frozen-day nodes
    dirty_day_acts: list[ActivityNode] = []
    frozen_day_acts: list[ActivityNode] = []
    for act in graph.activities:
        if (act.stay_id, act.local_date) in dirty_days:
            dirty_day_acts.append(act)
        else:
            frozen_day_acts.append(act)

    # Choose a compact epoch origin (keeps all CP-SAT integer start/end values small)
    all_starts = [a.start_utc for a in dirty_day_acts]
    epoch_origin_utc = (min(all_starts) - 24 * 3600) if all_starts else now_utc

    # Exact frozen summaries for global constraints
    frozen_cost = sum(a.cost_usd for a in frozen_day_acts) + sum(
        l.cost_usd for l in graph.transit_legs
    )
    rem_budget = (
        graph.constraints.total_budget_usd - frozen_cost
        if graph.constraints.total_budget_usd is not None
        else None
    )
    frozen_place_ids = frozenset(a.place_id for a in frozen_day_acts)
    frozen_cat_counts: dict[str, int] = {}
    used_once_tags: set[str] = set()
    for a in frozen_day_acts:
        frozen_cat_counts[a.category] = frozen_cat_counts.get(a.category, 0) + 1
        for tag in a.tags:
            if tag in graph.constraints.once_per_trip_tags:
                used_once_tags.add(tag)

    rem_cat_quotas = {
        cat: lim - frozen_cat_counts.get(cat, 0)
        for cat, lim in graph.constraints.max_per_category.items()
    }

    groups: dict[str, AssumptionGroupSpec] = {}
    act_specs: dict[str, ActivityVarSpec] = {}

    # Deterministic canonical id_rank across dirty-day activities
    sorted_dirty_acts = sorted(dirty_day_acts, key=lambda a: a.id)
    id_ranks = {a.id: idx + 1 for idx, a in enumerate(sorted_dirty_acts)}

    for act in sorted_dirty_acts:
        stay = stays_by_id[act.stay_id]
        pinned_tz = tz_registry.get(stay.tz_iana)
        day_start_utc = resolve_local_to_utc(act.local_date, _t(0, 0), pinned_tz).utc_epoch
        day_end_utc = resolve_local_to_utc(
            act.local_date + timedelta(days=1), _t(0, 0), pinned_tz
        ).utc_epoch
        assert day_start_utc is not None and day_end_utc is not None

        orig_start_min = (act.start_utc - epoch_origin_utc) // 60
        min_start_min = min(orig_start_min, (day_start_utc - epoch_origin_utc) // 60)
        max_start_min = max(orig_start_min, (day_end_utc - epoch_origin_utc) // 60)

        # Build candidate list: original place + any injected extra candidates (NO silent policy pruning!)
        cand_pairs: list[tuple[str, int]] = [(act.place_id, 0)]
        for pid, rank_pen in extra_map.get(act.id, []):
            if pid != act.place_id and all(p != pid for p, _ in cand_pairs):
                cand_pairs.append((pid, rank_pen))

        cand_options: list[CandidatePlaceOption] = []
        city_holidays = holidays_map.get(act.city_id, frozenset())

        for pid, rank_pen in cand_pairs:
            place = place_registry.get(pid)
            if place is None:
                continue

            if place.city_id != act.city_id:
                windows = ()
                truth_val = TruthValue.FALSE
                ep_code = "CROSS_CITY_PLACE_MISMATCH"
                blocked_by_policy = False
                authoritatively_closed = True
            else:
                windows = extract_day_open_windows_min(place, act.local_date, epoch_origin_utc, tz_registry)
                # Probe availability inside first valid window (or original slot if none) to get K3 status
                if windows:
                    probe_s = epoch_origin_utc + windows[0][0] * 60
                    probe_e = probe_s + max(act.duration_min, 1) * 60
                else:
                    probe_s, probe_e = act.start_utc, max(act.end_utc, act.start_utc + 60)

                avail = resolve_place_availability(
                    place,
                    probe_s,
                    probe_e,
                    now_utc,
                    tz_registry,
                    public_holidays=city_holidays,
                )
                truth_val = avail.truth
                ep_code = avail.epistemic_code
                allowed = is_epistemic_allowed(truth_val, ep_code, verification_policy)
                blocked_by_policy = (truth_val == TruthValue.UNKNOWN) and (not allowed)
                authoritatively_closed = truth_val == TruthValue.FALSE

            cand_options.append(
                CandidatePlaceOption(
                    place_id=place.place_id,
                    category=place.category,
                    cost_usd=place.estimated_cost_usd if pid != act.place_id else act.cost_usd,
                    tags=place.tags,
                    dietary_tags=place.dietary_tags,
                    windows_min=windows,
                    truth=truth_val,
                    epistemic_code=ep_code,
                    blocked_by_policy=blocked_by_policy,
                    authoritatively_closed=authoritatively_closed,
                    rank_penalty=rank_pen,
                )
            )

            # Register explicit assumption groups for closures, policy gates, and opening windows
            if blocked_by_policy:
                gid = f"POLICY_GATE:{act.id}:{pid}"
                groups[gid] = AssumptionGroupSpec(
                    group_id=gid,
                    origin="VERIFICATION_POLICY_GATE",
                    kind="POLICY_GATE",
                    node_ids=(act.id,),
                    detail=f"Candidate {pid} excluded under {verification_policy.value} ({ep_code}).",
                    epistemic_code=ep_code,
                )
            elif authoritatively_closed:
                gid = f"VENUE_CLOSED:{act.id}:{pid}"
                groups[gid] = AssumptionGroupSpec(
                    group_id=gid,
                    origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                    kind="VENUE_CLOSED",
                    node_ids=(act.id,),
                    detail=f"Candidate {pid} is authoritatively closed ({ep_code}).",
                    epistemic_code=ep_code,
                )
            elif windows:
                gid = f"OPENING_WINDOW:{act.id}:{pid}"
                groups[gid] = AssumptionGroupSpec(
                    group_id=gid,
                    origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                    kind="OPENING_WINDOW",
                    node_ids=(act.id,),
                    detail=f"Activity {act.id} at {pid} must fit inside verified opening window.",
                )

            # Dietary check on meal nodes
            if act.is_meal and graph.constraints.required_dietary_tags:
                if not set(graph.constraints.required_dietary_tags).issubset(set(place.dietary_tags)):
                    gid = f"DIETARY:{act.id}:{pid}"
                    groups[gid] = AssumptionGroupSpec(
                        group_id=gid,
                        origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                        kind="DIETARY",
                        node_ids=(act.id,),
                        detail=f"Meal candidate {pid} misses required dietary tags.",
                    )

        # Presence & lock assumption groups
        can_drop = (
            allow_auto_drop_optional
            and (act.node_tier == NodeTier.OPTIONAL)
            and (not act.locked_by_user)
        )
        if not can_drop:
            gid = f"PRESENCE_REQUIRED:{act.id}"
            groups[gid] = AssumptionGroupSpec(
                group_id=gid,
                origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                kind="PRESENCE_REQUIRED",
                node_ids=(act.id,),
                detail=f"Activity {act.id} ({act.node_tier.value}) cannot be dropped.",
            )
        if act.locked_by_user:
            gid = f"LOCK_PIN:{act.id}"
            groups[gid] = AssumptionGroupSpec(
                group_id=gid,
                origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                kind="LOCK_PIN",
                node_ids=(act.id,),
                detail=f"Activity {act.id} is locked_by_user at its original slot and venue.",
            )

        act_specs[act.id] = ActivityVarSpec(
            act_id=act.id,
            stay_id=act.stay_id,
            local_date=act.local_date,
            id_rank=id_ranks[act.id],
            orig_start_min=orig_start_min,
            orig_place_id=act.place_id,
            duration_min=act.duration_min,
            is_milestone=act.is_milestone,
            is_meal=act.is_meal,
            node_tier=act.node_tier,
            locked_by_user=act.locked_by_user,
            min_start_min=min_start_min,
            max_start_min=max_start_min,
            candidates=tuple(cand_options),
        )

    # Build DayBinIRs
    day_bins: list[DayBinIR] = []
    for stay_id, local_d in sorted(dirty_days):
        day_act_ids = tuple(
            a.id for a in sorted_dirty_acts if a.stay_id == stay_id and a.local_date == local_d
        )
        # Precompute transit durations across all candidate place pairs in this day
        all_day_pids = sorted(
            {c.place_id for aid in day_act_ids for c in act_specs[aid].candidates}
        )
        t_mat: dict[tuple[str, str], int] = {}
        for p1 in all_day_pids:
            for p2 in all_day_pids:
                t_mat[(p1, p2)] = 0 if p1 == p2 else int(transit_minutes_fn(p1, p2))

        day_bins.append(
            DayBinIR(
                stay_id=stay_id,
                local_date=local_d,
                act_ids=day_act_ids,
                daily_active_limit_min=graph.constraints.daily_active_limit_min,
                transit_matrix_min=t_mat,
                same_category_penalty=same_category_penalty,
            )
        )

        if day_act_ids:
            t_gid = f"DAY_TRANSIT_ORDER:{stay_id}:{local_d}"
            groups[t_gid] = AssumptionGroupSpec(
                group_id=t_gid,
                origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                kind="DAY_TRANSIT_ORDER",
                node_ids=day_act_ids,
                detail=f"Intra-day transit physics and non-overlap on {local_d}.",
            )
            b_gid = f"DAILY_BUDGET:{stay_id}:{local_d}"
            groups[b_gid] = AssumptionGroupSpec(
                group_id=b_gid,
                origin="HARD_PHYSICAL_OR_CONTRACTUAL",
                kind="DAILY_BUDGET",
                node_ids=day_act_ids,
                detail=f"Daily active minutes <= {graph.constraints.daily_active_limit_min} on {local_d}.",
            )

    # Global constraint groups
    if rem_budget is not None:
        groups["GLOBAL_BUDGET"] = AssumptionGroupSpec(
            group_id="GLOBAL_BUDGET",
            origin="HARD_PHYSICAL_OR_CONTRACTUAL",
            kind="GLOBAL_BUDGET",
            node_ids=tuple(act_specs.keys()),
            detail=f"Projected remaining trip budget <= {rem_budget} USD.",
        )

    groups["GLOBAL_ANTI_DUP"] = AssumptionGroupSpec(
        group_id="GLOBAL_ANTI_DUP",
        origin="HARD_PHYSICAL_OR_CONTRACTUAL",
        kind="GLOBAL_ANTI_DUP",
        node_ids=tuple(act_specs.keys()),
        detail="Trip-wide canonical place_id anti-duplication.",
    )

    for cat, rem_q in rem_cat_quotas.items():
        gid = f"GLOBAL_CAT_QUOTA:{cat}"
        groups[gid] = AssumptionGroupSpec(
            group_id=gid,
            origin="HARD_PHYSICAL_OR_CONTRACTUAL",
            kind="GLOBAL_CAT_QUOTA",
            node_ids=tuple(act_specs.keys()),
            detail=f"Projected remaining category quota for {cat!r} <= {rem_q}.",
        )

    for rspec in resource_no_overlaps:
        groups[rspec.group_id] = AssumptionGroupSpec(
            group_id=rspec.group_id,
            origin="HARD_PHYSICAL_OR_CONTRACTUAL",
            kind="RESOURCE_NO_OVERLAP",
            node_ids=rspec.act_ids,
            detail=f"Exclusive resource {rspec.resource_name!r} cannot overlap.",
        )

    return CompiledConstraintIR(
        epoch_origin_utc=epoch_origin_utc,
        activities=act_specs,
        day_bins=tuple(day_bins),
        resource_no_overlaps=resource_no_overlaps,
        globals_ir=GlobalProjectionIR(
            remaining_budget_usd=rem_budget,
            frozen_place_ids=frozen_place_ids,
            remaining_cat_quotas=rem_cat_quotas,
            used_once_tags=frozenset(used_once_tags),
            once_per_trip_tags=frozenset(graph.constraints.once_per_trip_tags),
            required_dietary_tags=frozenset(graph.constraints.required_dietary_tags),
            allow_auto_drop_optional=allow_auto_drop_optional,
            max_optional_drops_per_day=1,
        ),
        groups=groups,
    )
