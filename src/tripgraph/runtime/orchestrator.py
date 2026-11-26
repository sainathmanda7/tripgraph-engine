"""End-to-end TripGraph v7 Edit Orchestrator & Async GlobalReplanMode Worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
import time
import uuid
from typing import Any, Literal

from tripgraph.graph.closure import compute_dirty_closure
from tripgraph.graph.freeze import FreezeSnapshot, LockViolationError, assert_lock_guard, compute_blast_radius
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC
from tripgraph.grounding.provider import BudgetedProviderProxy, GroundingProvider
from tripgraph.repair.phase_b import PhaseBLLMClient, run_phase_b_cycle
from tripgraph.repair.taxonomy import expand_candidates_deterministic
from tripgraph.runtime.store import FencedTripStore, StaleVersionOrAbortedError
from tripgraph.runtime.supervisor import (
    SolverOverloadedError,
    SolverProcessSupervisor,
    SolverTimeoutError,
)
from tripgraph.schema.edits import (
    DropActivityEdit,
    EditCommand,
    ExtendStayEdit,
    LateNightArrivalEdit,
    MoveActivityEdit,
    ShiftStartDateEdit,
    SwapHotelEdit,
    UpdateDietaryEdit,
)
from tripgraph.schema.models import (
    ActivityNode,
    TripGraph,
    TruthValue,
    VerificationPolicy,
)
from tripgraph.solver.types import DualSolveResult
from tripgraph.solver.ir import compile_dirty_ir, is_epistemic_allowed
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc, shift_utc_preserving_local_time
from tripgraph.verifier.engine import VerificationReport, verify_trip_graph


@dataclass
class DeadlineContext:
    """Monotonic 8,000 ms synchronous request deadline tracker with 500 ms commit reserve."""

    budget_ms: float = 8000.0
    commit_reserve_ms: float = 500.0
    started_mono: float = field(default_factory=time.monotonic)

    def remaining_ms(self) -> float:
        return self.budget_ms - (time.monotonic() - self.started_mono) * 1000.0

    def stage_cap_sec(self, cap_ms: float) -> float:
        usable = min(cap_ms, self.remaining_ms() - self.commit_reserve_ms)
        return max(0.0, usable / 1000.0)


@dataclass
class GlobalReplanJob:
    job_id: str
    trip_id: str
    request_id: str
    base_version: int
    reason: str
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"] = "QUEUED"
    events: list[dict[str, Any]] = field(default_factory=list)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    final_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class EditOutcome:
    http_status: int
    payload: dict[str, Any]


def _shift_leg_dst_safe(
    leg,
    stays_by_id: dict,
    delta_days: int,
    tz_registry: PinnedTzRegistry,
):
    """Shift a TransitLeg by delta_days preserving origin wall-clock departure and physical duration."""
    origin_stay = stays_by_id[leg.from_stay_id]
    dur_sec = leg.arrive_utc - leg.depart_utc
    _, new_depart = shift_utc_preserving_local_time(
        leg.depart_utc, origin_stay.tz_iana, delta_days, tz_registry
    )
    return leg.model_copy(
        update={
            "depart_utc": new_depart,
            "arrive_utc": new_depart + dur_sec,
        }
    )


def apply_direct_mutation(
    graph: TripGraph,
    edit: EditCommand,
    tz_registry: PinnedTzRegistry | None = None,
) -> TripGraph:
    """Apply the user's direct structural edit using DST-safe timezone resolution."""
    reg = tz_registry or PinnedTzRegistry()
    stays_by_id = {s.id: s for s in graph.stays}

    if isinstance(edit, MoveActivityEdit):
        target_stay = stays_by_id.get(edit.target_stay_id)
        if target_stay is None:
            raise ValueError(f"Unknown target_stay_id: {edit.target_stay_id!r}")
        new_acts = []
        for a in graph.activities:
            if a.id == edit.activity_id:
                if edit.target_start_utc is not None:
                    new_start = edit.target_start_utc
                else:
                    src_stay = stays_by_id[a.stay_id]
                    src_tz = reg.get(src_stay.tz_iana)
                    src_local = datetime.fromtimestamp(a.start_utc, tz=timezone.utc).astimezone(src_tz.tzinfo)
                    dst_tz = reg.get(target_stay.tz_iana)
                    resolved = resolve_local_to_utc(
                        edit.target_local_date,
                        dt_time(src_local.hour, src_local.minute, src_local.second),
                        dst_tz,
                        ambiguous_policy="fold_1" if src_local.fold == 1 else "fold_0",
                        gap_policy="shift_forward",
                    )
                    assert resolved.utc_epoch is not None
                    new_start = resolved.utc_epoch
                new_acts.append(
                    a.model_copy(
                        update={
                            "stay_id": target_stay.id,
                            "city_id": target_stay.city_id,
                            "local_date": edit.target_local_date,
                            "start_utc": new_start,
                            "end_utc": new_start + a.duration_min * 60,
                        }
                    )
                )
            else:
                new_acts.append(a)
        return graph.model_copy(update={"activities": tuple(new_acts)})

    if isinstance(edit, DropActivityEdit):
        return graph.model_copy(
            update={"activities": tuple(a for a in graph.activities if a.id != edit.activity_id)}
        )

    if isinstance(edit, SwapHotelEdit):
        return graph.model_copy(
            update={
                "stays": tuple(
                    s.model_copy(update={"hotel": edit.new_hotel}) if s.id == edit.stay_id else s
                    for s in graph.stays
                )
            }
        )

    if isinstance(edit, LateNightArrivalEdit):
        new_legs = tuple(
            l.model_copy(
                update={
                    "depart_utc": edit.new_depart_utc,
                    "arrive_utc": edit.new_arrive_utc,
                    "carrier_slot_id": edit.new_carrier_slot_id,
                }
            )
            if l.id == edit.leg_id
            else l
            for l in graph.transit_legs
        )
        return graph.model_copy(update={"transit_legs": new_legs})

    if isinstance(edit, UpdateDietaryEdit):
        return graph.model_copy(
            update={
                "constraints": graph.constraints.model_copy(
                    update={"required_dietary_tags": edit.required_dietary_tags}
                )
            }
        )

    if isinstance(edit, ExtendStayEdit):
        stays = list(graph.stays)
        idx = next(i for i, s in enumerate(stays) if s.id == edit.stay_id)
        target = stays[idx]
        delta = timedelta(days=edit.delta_nights)
        new_out = target.check_out_date + delta
        stays[idx] = target.model_copy(
            update={"hotel": target.hotel.model_copy(update={"check_out_date": new_out})}
        )
        downstream_stay_ids: set[str] = set()
        for j in range(idx + 1, len(stays)):
            st = stays[j]
            downstream_stay_ids.add(st.id)
            stays[j] = st.model_copy(
                update={
                    "hotel": st.hotel.model_copy(
                        update={
                            "check_in_date": st.check_in_date + delta,
                            "check_out_date": st.check_out_date + delta,
                        }
                    )
                }
            )

        new_acts = []
        for a in graph.activities:
            if a.stay_id in downstream_stay_ids:
                st = stays_by_id[a.stay_id]
                new_date, new_start = shift_utc_preserving_local_time(
                    a.start_utc, st.tz_iana, edit.delta_nights, reg
                )
                new_acts.append(
                    a.model_copy(
                        update={
                            "local_date": new_date,
                            "start_utc": new_start,
                            "end_utc": new_start + a.duration_min * 60,
                        }
                    )
                )
            else:
                new_acts.append(a)

        new_legs = tuple(
            _shift_leg_dst_safe(l, stays_by_id, edit.delta_nights, reg)
            if (l.from_stay_id == target.id or l.from_stay_id in downstream_stay_ids or l.to_stay_id in downstream_stay_ids)
            else l
            for l in graph.transit_legs
        )
        return graph.model_copy(
            update={"stays": tuple(stays), "activities": tuple(new_acts), "transit_legs": new_legs}
        )

    if isinstance(edit, ShiftStartDateEdit):
        delta = timedelta(days=edit.delta_days)
        new_stays = tuple(
            s.model_copy(
                update={
                    "hotel": s.hotel.model_copy(
                        update={
                            "check_in_date": s.check_in_date + delta,
                            "check_out_date": s.check_out_date + delta,
                        }
                    )
                }
            )
            for s in graph.stays
        )
        new_acts = []
        for a in graph.activities:
            st = stays_by_id[a.stay_id]
            new_date, new_start = shift_utc_preserving_local_time(
                a.start_utc, st.tz_iana, edit.delta_days, reg
            )
            new_acts.append(
                a.model_copy(
                    update={
                        "local_date": new_date,
                        "start_utc": new_start,
                        "end_utc": new_start + a.duration_min * 60,
                    }
                )
            )
        new_legs = tuple(
            _shift_leg_dst_safe(l, stays_by_id, edit.delta_days, reg)
            for l in graph.transit_legs
        )
        return graph.model_copy(
            update={"stays": new_stays, "activities": tuple(new_acts), "transit_legs": new_legs}
        )

    return graph


def _apply_solve_assignments(
    graph: TripGraph,
    solve_res: DualSolveResult,
    place_registry: dict,
) -> TripGraph:
    updated: list[ActivityNode] = []
    for a in graph.activities:
        asgn = solve_res.assignments.get(a.id)
        if asgn is None:
            updated.append(a)
            continue
        if not asgn.present:
            continue
        assert asgn.place_id is not None and asgn.start_utc is not None and asgn.end_utc is not None
        p = place_registry.get(asgn.place_id)
        updated.append(
            a.model_copy(
                update={
                    "place_id": asgn.place_id,
                    "start_utc": asgn.start_utc,
                    "end_utc": asgn.end_utc,
                    "category": p.category if p is not None else a.category,
                    "cost_usd": p.estimated_cost_usd if (p is not None and asgn.place_id != a.place_id) else a.cost_usd,
                }
            )
        )
    return graph.model_copy(update={"activities": tuple(updated)})


class TripOrchestrator:
    """Coordinates synchronous 8s edits and asynchronous 30s GlobalReplanMode."""

    def __init__(
        self,
        *,
        store: FencedTripStore,
        supervisor: SolverProcessSupervisor,
        provider: GroundingProvider,
        tz_registry: PinnedTzRegistry | None = None,
        llm_client: PhaseBLLMClient | None = None,
        now_utc: int = DEFAULT_FIXTURE_NOW_UTC,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.provider = provider
        self.tz_registry = tz_registry or PinnedTzRegistry()
        self.llm_client = llm_client
        self.now_utc = now_utc
        self.jobs: dict[str, GlobalReplanJob] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    def verify_only(self, graph: TripGraph) -> VerificationReport:
        places = self.provider.all_places()
        holidays = {s.city_id: self.provider.get_public_holidays(s.city_id) for s in graph.stays}
        return verify_trip_graph(
            graph,
            places,
            self.provider.transit_minutes,
            self.now_utc,
            self.tz_registry,
            public_holidays_by_city=holidays,
        )

    @staticmethod
    def _check_verification_commit_gate(
        rep, verification_policy: VerificationPolicy
    ) -> tuple[bool, list[str], list[str]]:
        """Enforce single-source-of-truth K3 + VerificationPolicy commit gate.

        Blocks any FALSE violation and any UNKNOWN assumption disallowed under
        `verification_policy` via `is_epistemic_allowed`.
        """
        violations = [v.code for v in rep.violations]
        disallowed_unknowns = [
            a.code
            for a in rep.assumptions_accepted
            if not is_epistemic_allowed(a.truth, a.code, verification_policy)
        ]
        blocked = (rep.hard_constraints_status == TruthValue.FALSE) or bool(disallowed_unknowns)
        return blocked, violations, disallowed_unknowns

    async def apply_edit(
        self,
        *,
        trip_id: str,
        edit: EditCommand,
        verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
        allow_auto_drop_optional: bool = False,
        budget_ms: float = 8000.0,
        test_hang_sec: float = 0.0,
    ) -> EditOutcome:
        ctx = DeadlineContext(budget_ms=budget_ms)
        committed = self.store.get_trip(trip_id)
        if committed is None:
            return EditOutcome(404, {"error": f"Trip {trip_id!r} not found."})

        base_graph = committed.graph
        base_version = committed.version
        request_id = str(uuid.uuid4())
        deadline_utc = time.time() + (budget_ms / 1000.0)
        self.store.register_request(request_id, trip_id, deadline_utc)

        try:
            places = self.provider.all_places()
            holidays = {s.city_id: self.provider.get_public_holidays(s.city_id) for s in base_graph.stays}

            # 0. Explicit locked-node refusal contract for ShiftStartDateEdit
            if isinstance(edit, ShiftStartDateEdit) and not edit.allow_shift_locked:
                locked_stays = [s.id for s in base_graph.stays if s.locked_by_user]
                locked_legs = [l.id for l in base_graph.transit_legs if l.locked_by_user]
                locked_acts = [a.id for a in base_graph.activities if a.locked_by_user]
                if locked_stays or locked_legs or locked_acts:
                    self.store.abort_request(request_id)
                    return EditOutcome(
                        409,
                        {
                            "status": "LOCKED_NODE_CONFLICT",
                            "detail": (
                                "ShiftStartDateEdit refused because user-locked nodes exist. "
                                "Pass allow_shift_locked=true to explicitly calendar-shift locked nodes."
                            ),
                            "locked_stays": locked_stays,
                            "locked_legs": locked_legs,
                            "locked_activities": locked_acts,
                        },
                    )

            # 1. Compute Factor-Graph Dirty Closure
            closure = compute_dirty_closure(base_graph, edit, places)

            # 2. All-locked fast path (no divide-by-zero; enforce LockGuard + K3/policy gate, no CP-SAT solve)
            if closure.scope_tier == "ALL_LOCKED_VERIFY_ONLY":
                snap = FreezeSnapshot.capture(
                    base_graph,
                    closure.dirty_stay_ids,
                    closure.dirty_leg_ids,
                    closure.dirty_activity_ids,
                )
                mutated = apply_direct_mutation(base_graph, edit, self.tz_registry)
                assert_lock_guard(snap, mutated)
                rep = verify_trip_graph(
                    mutated, places, self.provider.transit_minutes, self.now_utc, self.tz_registry,
                    public_holidays_by_city=holidays,
                )
                blocked, violations, disallowed_unknowns = self._check_verification_commit_gate(
                    rep, verification_policy
                )
                if blocked:
                    self.store.abort_request(request_id)
                    return EditOutcome(
                        409,
                        {
                            "status": "NEEDS_USER_RESOLUTION",
                            "scope_tier": "ALL_LOCKED_VERIFY_ONLY",
                            "solver_certificate": "NOT_INVOKED_VERIFY_ONLY",
                            "hard_constraints_status": rep.hard_constraints_status.value,
                            "verification_certificate": rep.verification_certificate,
                            "violations": violations,
                            "disallowed_assumptions": disallowed_unknowns,
                        },
                    )
                saved = self.store.commit_fenced(
                    trip_id=trip_id, base_version=base_version, request_id=request_id, new_graph=mutated
                )
                return EditOutcome(
                    200,
                    {
                        "status": "COMMITTED",
                        "version": saved.version,
                        "scope_tier": "ALL_LOCKED_VERIFY_ONLY",
                        "solver_certificate": "NOT_INVOKED_VERIFY_ONLY",
                        "hard_constraints_status": rep.hard_constraints_status.value,
                        "verification_certificate": rep.verification_certificate,
                        "assumptions_accepted": [
                            {"node_ids": list(a.node_ids), "code": a.code}
                            for a in rep.assumptions_accepted
                        ],
                        "graph": saved.graph.model_dump(mode="json"),
                    },
                )

            # 3. Escalate >50% dirty ratio or NON_PROJECTABLE edits to 30s Async GlobalReplanMode
            if closure.scope_tier == "GLOBAL_REPLAN":
                job_id = f"job_{uuid.uuid4().hex[:12]}"
                job = GlobalReplanJob(
                    job_id=job_id,
                    trip_id=trip_id,
                    request_id=request_id,
                    base_version=base_version,
                    reason=closure.escalation_reason or "GLOBAL_ESCALATION",
                )
                self.jobs[job_id] = job
                # Extend request deadline for the separate 30s async contract
                self.store.abort_request(request_id)
                async_req_id = f"async_{job_id}"
                self.store.register_request(async_req_id, trip_id, time.time() + 30.0)
                job.request_id = async_req_id

                task = asyncio.create_task(
                    self._run_global_replan_job(
                        job, base_graph, edit, verification_policy, allow_auto_drop_optional
                    )
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

                return EditOutcome(
                    202,
                    {
                        "mode": "GLOBAL_REPLAN_ESCALATED",
                        "reason": job.reason,
                        "job_id": job_id,
                        "events_url": f"/jobs/{job_id}/events",
                        "dirty_ratio": closure.dirty_ratio,
                    },
                )

            # 4. Capture SHA-256 FreezeSnapshot BEFORE mutation & compile Horizon 0 IR
            snap = FreezeSnapshot.capture(
                base_graph,
                closure.dirty_stay_ids,
                closure.dirty_leg_ids,
                closure.dirty_activity_ids,
            )
            mutated = apply_direct_mutation(base_graph, edit, self.tz_registry)
            ir = compile_dirty_ir(
                mutated,
                closure.dirty_days,
                places,
                self.provider.transit_minutes,
                self.now_utc,
                self.tz_registry,
                verification_policy=verification_policy,
                public_holidays_by_city=holidays,
                allow_auto_drop_optional=allow_auto_drop_optional,
            )

            # 5. Dispatch Horizon 0 solve to isolated subprocess pool (NON-BLOCKING!)
            h0_cap = ctx.stage_cap_sec(950.0)
            solve_res = await self.supervisor.solve(
                ir,
                hard_deadline_sec=h0_cap,
                opt_timeout_sec=min(0.75, max(0.05, h0_cap - 0.15)),
                diag_total_budget_ms=250.0,
                run_mus=True,
                test_hang_sec=test_hang_sec,
            )

            # 6. If INFEASIBLE on a venue/dietary conflict (not pure lock/policy), attempt recovery
            recovery_mode = "PHASE_A_EXACT_CPSAT"
            if solve_res.certificate == "PROVEN_PHYSICALLY_INFEASIBLE":
                closed_nodes = [
                    g.node_ids[0]
                    for g in solve_res.core_groups
                    if g.kind in ("VENUE_CLOSED", "DIETARY") and g.node_ids
                ]
                if closed_nodes:
                    target_act = next(
                        (a for a in mutated.activities if a.id == closed_nodes[0] and not a.locked_by_user),
                        None,
                    )
                    if target_act is not None:
                        proxy = BudgetedProviderProxy(self.provider)
                        if self.llm_client is not None:
                            async def _phase_b_solve(
                                pb_ir, *, opt_timeout_sec: float, run_mus: bool = False
                            ) -> DualSolveResult:
                                cap = ctx.stage_cap_sec(max(opt_timeout_sec * 1000.0 + 150.0, 450.0))
                                return await self.supervisor.solve(
                                    pb_ir,
                                    hard_deadline_sec=cap,
                                    opt_timeout_sec=min(opt_timeout_sec, max(0.05, cap - 0.10)),
                                    run_mus=run_mus,
                                )

                            pb = await run_phase_b_cycle(
                                graph=mutated,
                                dirty_days=closure.dirty_days,
                                target_act=target_act,
                                initial_core=solve_res.core_groups,
                                proxy=proxy,
                                place_registry=places,
                                now_utc=self.now_utc,
                                tz_registry=self.tz_registry,
                                llm_client=self.llm_client,
                                solve_fn=_phase_b_solve,
                                verification_policy=verification_policy,
                                public_holidays_by_city=holidays,
                                allow_auto_drop_optional=allow_auto_drop_optional,
                            )
                            if pb.status == "RETRYABLE_UPSTREAM_LLM_UNAVAILABLE":
                                self.store.abort_request(request_id)
                                return EditOutcome(
                                    503,
                                    {
                                        "status": "RETRYABLE_UPSTREAM_LLM_UNAVAILABLE",
                                        "has_uncommitted_fallback_preview": pb.suggested_fallback_preview is not None,
                                    },
                                )
                            if pb.solve_result is not None and pb.solve_result.certificate in (
                                "PROVEN_OPTIMAL_IN_SCOPE",
                                "FEASIBLE_OPTIMALITY_UNPROVEN",
                            ):
                                solve_res = pb.solve_result
                                recovery_mode = pb.status
                        else:
                            # Zero-key S2_taxonomy deterministic fallback
                            cands = expand_candidates_deterministic(
                                target_act,
                                mutated,
                                proxy,
                                places,
                                self.now_utc,
                                self.tz_registry,
                                verification_policy=verification_policy,
                                public_holidays=holidays.get(target_act.city_id, frozenset()),
                            )
                            if cands:
                                ir_tax = compile_dirty_ir(
                                    mutated,
                                    closure.dirty_days,
                                    places,
                                    self.provider.transit_minutes,
                                    self.now_utc,
                                    self.tz_registry,
                                    verification_policy=verification_policy,
                                    extra_candidates_by_act={target_act.id: cands},
                                    public_holidays_by_city=holidays,
                                    allow_auto_drop_optional=allow_auto_drop_optional,
                                )
                                tax_res = await self.supervisor.solve(
                                    ir_tax,
                                    hard_deadline_sec=ctx.stage_cap_sec(650.0),
                                    opt_timeout_sec=0.55,
                                    run_mus=False,
                                )
                                if tax_res.certificate in (
                                    "PROVEN_OPTIMAL_IN_SCOPE",
                                    "FEASIBLE_OPTIMALITY_UNPROVEN",
                                ):
                                    solve_res = tax_res
                                    recovery_mode = "S2_TAXONOMY_DETERMINISTIC"

            # 7. Map terminal solver certificates cleanly to HTTP status codes
            if solve_res.certificate in (
                "INTERNAL_BASE_AXIOM_CONTRADICTION",
                "INTERNAL_MODEL_EQUIVALENCE_ERROR",
                "INTERNAL_SOLVER_MODEL_ERROR",
            ):
                self.store.abort_request(request_id)
                return EditOutcome(500, {"solver_certificate": solve_res.certificate})

            if solve_res.certificate == "INDETERMINATE_SOLVER_TIMEOUT":
                self.store.abort_request(request_id)
                return EditOutcome(503, {"solver_certificate": solve_res.certificate})

            if solve_res.certificate in (
                "PROVEN_PHYSICALLY_INFEASIBLE",
                "INFEASIBLE_UNDER_VERIFICATION_POLICY",
                "INFEASIBLE_CAUSE_UNCLASSIFIED",
            ):
                self.store.abort_request(request_id)
                return EditOutcome(
                    409,
                    {
                        "status": "NEEDS_USER_RESOLUTION",
                        "scope_tier": closure.scope_tier,
                        "solver_certificate": solve_res.certificate,
                        "core_minimality": solve_res.core_minimality,
                        "core_groups": [
                            {
                                "group_id": g.group_id,
                                "origin": g.origin,
                                "kind": g.kind,
                                "node_ids": list(g.node_ids),
                                "epistemic_code": g.epistemic_code,
                                "detail": g.detail,
                            }
                            for g in solve_res.core_groups
                        ],
                    },
                )

            # 8. Materialize solved graph, enforce SHA-256 LockGuard & Independent K3 Verifier
            candidate_graph = _apply_solve_assignments(mutated, solve_res, places)
            assert_lock_guard(snap, candidate_graph)

            rep = verify_trip_graph(
                candidate_graph,
                places,
                self.provider.transit_minutes,
                self.now_utc,
                self.tz_registry,
                public_holidays_by_city=holidays,
            )
            blocked, violations, disallowed_unknowns = self._check_verification_commit_gate(
                rep, verification_policy
            )
            if blocked:
                self.store.abort_request(request_id)
                return EditOutcome(
                    500,
                    {
                        "error": "INDEPENDENT_VERIFIER_REJECTED_SOLVED_GRAPH",
                        "hard_constraints_status": rep.hard_constraints_status.value,
                        "violations": violations,
                        "disallowed_assumptions": disallowed_unknowns,
                    },
                )

            # 9. Atomic Fenced DB Commit
            saved = self.store.commit_fenced(
                trip_id=trip_id,
                base_version=base_version,
                request_id=request_id,
                new_graph=candidate_graph,
            )
            br = compute_blast_radius(
                base_graph,
                saved.graph,
                logical_scope_ids=closure.dirty_stay_ids | closure.dirty_leg_ids | closure.dirty_activity_ids,
            )
            return EditOutcome(
                200,
                {
                    "status": "COMMITTED",
                    "version": saved.version,
                    "scope_tier": closure.scope_tier,
                    "recovery_mode": recovery_mode,
                    "solver_certificate": solve_res.certificate,
                    "hard_constraints_status": rep.hard_constraints_status.value,
                    "verification_certificate": rep.verification_certificate,
                    "assumptions_accepted": [
                        {"node_ids": list(a.node_ids), "code": a.code}
                        for a in rep.assumptions_accepted
                    ],
                    "blast_radius": {
                        "semantic": br.semantic_blast_radius,
                        "temporal": br.temporal_blast_radius,
                    },
                    "graph": saved.graph.model_dump(mode="json"),
                },
            )

        except LockViolationError as exc:
            self.store.abort_request(request_id)
            return EditOutcome(409, {"status": "LOCK_GUARD_VIOLATION", "detail": str(exc)})
        except SolverOverloadedError as exc:
            self.store.abort_request(request_id)
            return EditOutcome(503, {"status": "SOLVER_OVERLOADED", "detail": str(exc)})
        except SolverTimeoutError as exc:
            self.store.abort_request(request_id)
            return EditOutcome(503, {"status": "INDETERMINATE_SOLVER_TIMEOUT", "detail": str(exc)})
        except StaleVersionOrAbortedError as exc:
            return EditOutcome(409, {"status": "STALE_VERSION_OR_ABORTED", "detail": str(exc)})
        except asyncio.CancelledError:
            self.store.abort_request(request_id)
            raise

    async def _run_global_replan_job(
        self,
        job: GlobalReplanJob,
        base_graph: TripGraph,
        edit: EditCommand,
        verification_policy: VerificationPolicy,
        allow_auto_drop_optional: bool,
    ) -> None:
        """Separate 30,000 ms async execution contract for GlobalReplanMode."""
        job.status = "RUNNING"
        job.events.append({"stage": "global_replan_started", "reason": job.reason})
        try:
            places = self.provider.all_places()
            holidays = {s.city_id: self.provider.get_public_holidays(s.city_id) for s in base_graph.stays}

            mutated = apply_direct_mutation(base_graph, edit, self.tz_registry)
            # Capture SHA-256 FreezeSnapshot for user-locked nodes.
            # On ShiftStartDateEdit with explicit allow_shift_locked=True, lock hashes pin the
            # approved calendar-shifted locked nodes so CP-SAT still cannot swap/drop/reschedule them.
            lock_baseline = (
                mutated
                if (isinstance(edit, ShiftStartDateEdit) and edit.allow_shift_locked)
                else base_graph
            )
            snap = FreezeSnapshot.capture(
                lock_baseline,
                frozenset(s.id for s in lock_baseline.stays),
                frozenset(l.id for l in lock_baseline.transit_legs),
                frozenset(a.id for a in lock_baseline.activities),
            )
            all_days = frozenset((a.stay_id, a.local_date) for a in mutated.activities)

            job.events.append({"stage": "compiling_global_ir", "days": len(all_days)})
            ir = compile_dirty_ir(
                mutated,
                all_days,
                places,
                self.provider.transit_minutes,
                self.now_utc,
                self.tz_registry,
                verification_policy=verification_policy,
                public_holidays_by_city=holidays,
                allow_auto_drop_optional=allow_auto_drop_optional,
            )

            job.events.append({"stage": "solving_global_cpsat"})
            res = await self.supervisor.solve(
                ir,
                hard_deadline_sec=5.5,
                opt_timeout_sec=5.0,
                run_mus=False,
            )
            if res.certificate not in ("PROVEN_OPTIMAL_IN_SCOPE", "FEASIBLE_OPTIMALITY_UNPROVEN"):
                self.store.abort_request(job.request_id)
                job.status = "FAILED"
                job.final_payload = {"solver_certificate": res.certificate}
                job.events.append({"stage": "failed", "solver_certificate": res.certificate})
                return

            candidate = _apply_solve_assignments(mutated, res, places)

            # Mandatory pre-commit safety barrier: LockGuard + independent K3/policy verification.
            try:
                assert_lock_guard(snap, candidate)
            except LockViolationError as exc:
                self.store.abort_request(job.request_id)
                job.status = "FAILED"
                job.final_payload = {"error": "LOCK_GUARD_VIOLATION", "detail": str(exc)}
                job.events.append({"stage": "failed", "error": "LOCK_GUARD_VIOLATION", "detail": str(exc)})
                return

            rep = verify_trip_graph(
                candidate,
                places,
                self.provider.transit_minutes,
                self.now_utc,
                self.tz_registry,
                public_holidays_by_city=holidays,
            )
            blocked, violations, disallowed_unknowns = self._check_verification_commit_gate(
                rep, verification_policy
            )
            if blocked:
                self.store.abort_request(job.request_id)
                job.status = "FAILED"
                job.final_payload = {
                    "error": "INDEPENDENT_VERIFIER_REJECTED_SOLVED_GRAPH",
                    "hard_constraints_status": rep.hard_constraints_status.value,
                    "violations": violations,
                    "disallowed_assumptions": disallowed_unknowns,
                }
                job.events.append(
                    {
                        "stage": "failed",
                        "error": "INDEPENDENT_VERIFIER_REJECTED_SOLVED_GRAPH",
                        "violations": violations,
                        "disallowed_assumptions": disallowed_unknowns,
                    }
                )
                return

            saved = self.store.commit_fenced(
                trip_id=job.trip_id,
                base_version=job.base_version,
                request_id=job.request_id,
                new_graph=candidate,
            )
            job.status = "COMPLETED"
            job.final_payload = {
                "status": "COMMITTED",
                "scope_tier": "GLOBAL_REPLAN",
                "version": saved.version,
                "solver_certificate": res.certificate,
                "hard_constraints_status": rep.hard_constraints_status.value,
                "verification_certificate": rep.verification_certificate,
                "assumptions_accepted": [
                    {"node_ids": list(a.node_ids), "code": a.code}
                    for a in rep.assumptions_accepted
                ],
            }
            job.events.append(
                {
                    "stage": "completed",
                    "version": saved.version,
                    "verification_certificate": rep.verification_certificate,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.store.abort_request(job.request_id)
            job.status = "FAILED"
            job.final_payload = {"error": str(exc)}
            job.events.append({"stage": "failed", "error": str(exc)})
        finally:
            job.done_event.set()
