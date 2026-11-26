"""Dual-Model CP-SAT Solver & Bounded Deletion-Filter MUS Engine (TripGraph v7).

Guarantees:
1. OptimizationModel (multi-worker + exact dynamic hierarchical weights) and
   DiagnosticFeasibilityModel (single-worker + zero objective + assumptions)
   are materialized from the EXACT SAME CompiledConstraintIR.
2. Shared scheduling resources use cloned per-constraint optional intervals
   gated by exact conjunction literals p_{k,v} <=> (y_v AND a_g).
3. Objective tier bounds (U1, U2, U3) are computed dynamically from actual
   instantiated CP-SAT domains and asserted < 2**53 before solve.
4. Empty assumption core -> INTERNAL_BASE_AXIOM_CONTRADICTION (never mislabeled
   as PROVEN_PHYSICALLY_INFEASIBLE).
5. Policy-gate literal in core -> INFEASIBLE_UNDER_VERIFICATION_POLICY.
6. MUS deletion filter strictly capped at <= 6 probes x 20 ms within 250 ms.
"""

from __future__ import annotations

from dataclasses import dataclass
import time as pytime
from typing import Any

from ortools.sat.python import cp_model

from tripgraph.solver.ir import (
    AssumptionGroupSpec,
    CompiledConstraintIR,
)
from tripgraph.solver.types import (
    CoreMinimality,
    DualSolveResult,
    ObjectiveOverflowError,
    SolvedActivityAssignment,
    SolverCertificate,
)


@dataclass
class _BuiltModel:
    model: cp_model.CpModel
    y_vars: dict[str, cp_model.IntVar]
    start_vars: dict[str, cp_model.IntVar]
    end_vars: dict[str, cp_model.IntVar]
    x_place: dict[tuple[str, str], cp_model.IntVar]
    x_adj: dict[tuple[str, str, str, str], cp_model.IntVar]  # (stay, iso_d, u, v)
    changed_vars: dict[str, cp_model.IntVar]
    disp_vars: dict[str, cp_model.IntVar]
    same_cat_vars: list[tuple[int, cp_model.IntVar]]
    assump_vars: dict[str, cp_model.IntVar]
    index_to_group_id: dict[int, str]
    w1: int = 0
    w2: int = 0
    w3: int = 1


def _build_cp_model(
    ir: CompiledConstraintIR,
    *,
    diagnostic_mode: bool,
    active_group_ids: frozenset[str] | None = None,
) -> _BuiltModel:
    """Single shared compiler materializing OptimizationModel or DiagnosticFeasibilityModel."""
    model = cp_model.CpModel()
    active_set = (
        active_group_ids if active_group_ids is not None else frozenset(ir.groups.keys())
    )

    assump_vars: dict[str, cp_model.IntVar] = {}
    index_to_group_id: dict[int, str] = {}
    if diagnostic_mode:
        for gid in sorted(ir.groups.keys()):
            lit = model.new_bool_var(f"assump::{gid}")
            assump_vars[gid] = lit
            index_to_group_id[lit.index] = gid

    def group_active(gid: str) -> bool:
        if gid not in ir.groups:
            return False
        return True if diagnostic_mode else (gid in active_set)

    def enforce_lits(gid: str, extra: list[Any] | None = None) -> list[Any]:
        lits: list[Any] = []
        if diagnostic_mode:
            lits.append(assump_vars[gid])
        if extra:
            lits.extend(extra)
        return lits

    # ------------------------------------------------------------------
    # 1. UNCONDITIONAL BASE AXIOMS (Identical in both models, never disabled)
    # ------------------------------------------------------------------
    y_vars: dict[str, cp_model.IntVar] = {}
    start_vars: dict[str, cp_model.IntVar] = {}
    end_vars: dict[str, cp_model.IntVar] = {}
    x_place: dict[tuple[str, str], cp_model.IntVar] = {}
    changed_vars: dict[str, cp_model.IntVar] = {}
    disp_vars: dict[str, cp_model.IntVar] = {}
    max_disp_bounds: list[int] = []
    rank_penalty_terms: list[tuple[int, cp_model.IntVar]] = []
    droppable_act_ids: list[str] = []

    for aid, spec in sorted(ir.activities.items()):
        y = model.new_bool_var(f"y::{aid}")
        s = model.new_int_var(spec.min_start_min, spec.max_start_min, f"s::{aid}")
        e = model.new_int_var(
            spec.min_start_min + spec.duration_min,
            spec.max_start_min + spec.duration_min,
            f"e::{aid}",
        )
        model.add(e == s + spec.duration_min)
        # Anchor dropped nodes cleanly to orig_start_min so disp=0 when dropped
        model.add(s == spec.orig_start_min).only_enforce_if(y.Not())

        y_vars[aid] = y
        start_vars[aid] = s
        end_vars[aid] = e

        # Place selection: active node picks exactly 1 candidate; dropped picks 0
        p_lits: list[cp_model.IntVar] = []
        for cand in spec.candidates:
            xp = model.new_bool_var(f"xp::{aid}::{cand.place_id}")
            x_place[(aid, cand.place_id)] = xp
            p_lits.append(xp)
            if cand.rank_penalty > 0:
                rank_penalty_terms.append((cand.rank_penalty, xp))

        if p_lits:
            model.add(sum(p_lits) == y)
        else:
            model.add(y == 0)

        # Change & displacement tracking (base axioms)
        ch = model.new_bool_var(f"ch::{aid}")
        max_disp = max(
            abs(spec.max_start_min - spec.orig_start_min),
            abs(spec.orig_start_min - spec.min_start_min),
            1,
        )
        disp = model.new_int_var(0, max_disp, f"disp::{aid}")
        model.add(disp >= s - spec.orig_start_min)
        model.add(disp >= spec.orig_start_min - s)
        model.add(s - spec.orig_start_min <= max_disp * ch)
        model.add(spec.orig_start_min - s <= max_disp * ch)
        model.add(ch >= 1 - y)
        if (aid, spec.orig_place_id) in x_place:
            model.add(ch >= 1 - x_place[(aid, spec.orig_place_id)])
        else:
            model.add(ch == 1)

        changed_vars[aid] = ch
        disp_vars[aid] = disp
        max_disp_bounds.append(max_disp)

        if f"PRESENCE_REQUIRED:{aid}" not in ir.groups:
            droppable_act_ids.append(aid)

    # Optional test hook: unconditional base-axiom contradiction
    if ir.synthetic_base_contradiction is not None:
        sc = ir.synthetic_base_contradiction
        sv = start_vars[sc.act_id]
        model.add(sv >= sc.impossible_lb)
        model.add(sv <= sc.impossible_ub)

    # Dynamic Adjacency Base Axioms (Sentinel degree equations + Subtour rank elimination)
    x_adj: dict[tuple[str, str, str, str], cp_model.IntVar] = {}
    same_cat_vars: list[tuple[int, cp_model.IntVar]] = []

    for day in ir.day_bins:
        iso_d = day.local_date.isoformat()
        nodes = list(day.act_ids)
        m = len(nodes)
        sent_s = "__START__"
        sent_e = "__END__"

        # Directed edges over ({S} U V_d) x (V_d U {E}) \ self-loops
        # Includes (S, E) for empty day
        edge_pairs: list[tuple[str, str]] = [(sent_s, sent_e)]
        for u in nodes:
            edge_pairs.append((sent_s, u))
            edge_pairs.append((u, sent_e))
            for v in nodes:
                if u != v:
                    edge_pairs.append((u, v))

        for u, v in edge_pairs:
            x_adj[(day.stay_id, iso_d, u, v)] = model.new_bool_var(
                f"adj::{day.stay_id}::{iso_d}::{u}->{v}"
            )

        # Sentinel degrees: exactly 1 outgoing from S, exactly 1 incoming to E
        model.add(
            sum(x_adj[(day.stay_id, iso_d, sent_s, v)] for v in [*nodes, sent_e]) == 1
        )
        model.add(
            sum(x_adj[(day.stay_id, iso_d, u, sent_e)] for u in [sent_s, *nodes]) == 1
        )

        # Node degrees: in-degree == y[v] AND out-degree == y[v]
        for v in nodes:
            in_edges = [x_adj[(day.stay_id, iso_d, u, v)] for u in [sent_s, *nodes] if u != v]
            out_edges = [x_adj[(day.stay_id, iso_d, v, w)] for w in [*nodes, sent_e] if w != v]
            model.add(sum(in_edges) == y_vars[v])
            model.add(sum(out_edges) == y_vars[v])

        # Subtour elimination: rank r_v in [1..m], x_{u,v}=1 => r_v >= r_u + 1
        rank_vars: dict[str, cp_model.IntVar] = {
            v: model.new_int_var(1, max(1, m), f"rank::{day.stay_id}::{iso_d}::{v}")
            for v in nodes
        }
        for u in nodes:
            for v in nodes:
                if u != v:
                    model.add(rank_vars[v] >= rank_vars[u] + 1).only_enforce_if(
                        x_adj[(day.stay_id, iso_d, u, v)]
                    )

        # Cap optional drops per day when drops are allowed
        if ir.globals_ir.allow_auto_drop_optional:
            day_droppable = [aid for aid in nodes if aid in droppable_act_ids]
            if day_droppable:
                model.add(
                    sum(1 - y_vars[aid] for aid in day_droppable)
                    <= ir.globals_ir.max_optional_drops_per_day
                )

        # Intra-day Order-1 Markov same-category indicator (used in OptimizationModel objective)
        if not diagnostic_mode and day.same_category_penalty > 0:
            for u in nodes:
                for v in nodes:
                    if u == v:
                        continue
                    same_pairs = [
                        (cu.place_id, cv.place_id)
                        for cu in ir.activities[u].candidates
                        for cv in ir.activities[v].candidates
                        if cu.category == cv.category
                    ]
                    if not same_pairs:
                        continue
                    sc_var = model.new_bool_var(f"samecat::{day.stay_id}::{iso_d}::{u}->{v}")
                    adj_uv = x_adj[(day.stay_id, iso_d, u, v)]
                    for pu, pv in same_pairs:
                        model.add(sc_var == 1).only_enforce_if(
                            [adj_uv, x_place[(u, pu)], x_place[(v, pv)]]
                        )
                    same_cat_vars.append((day.same_category_penalty, sc_var))

    # ------------------------------------------------------------------
    # 2. ASSUMPTION-GATED CONSTRAINT GROUPS (Identical semantics in both models)
    # ------------------------------------------------------------------

    # 2a. Activity presence, locks, closures, policy gates, opening windows & dietary
    for aid, spec in sorted(ir.activities.items()):
        p_gid = f"PRESENCE_REQUIRED:{aid}"
        if group_active(p_gid):
            ct = model.add(y_vars[aid] == 1)
            lits = enforce_lits(p_gid)
            if lits:
                ct.only_enforce_if(lits)

        l_gid = f"LOCK_PIN:{aid}"
        if group_active(l_gid):
            lits = enforce_lits(l_gid)
            ct1 = model.add(y_vars[aid] == 1)
            ct2 = model.add(start_vars[aid] == spec.orig_start_min)
            if lits:
                ct1.only_enforce_if(lits)
                ct2.only_enforce_if(lits)
            if (aid, spec.orig_place_id) in x_place:
                ct3 = model.add(x_place[(aid, spec.orig_place_id)] == 1)
                if lits:
                    ct3.only_enforce_if(lits)

        for cand in spec.candidates:
            xp = x_place[(aid, cand.place_id)]

            pol_gid = f"POLICY_GATE:{aid}:{cand.place_id}"
            if group_active(pol_gid):
                ct = model.add(xp == 0)
                lits = enforce_lits(pol_gid)
                if lits:
                    ct.only_enforce_if(lits)

            cl_gid = f"VENUE_CLOSED:{aid}:{cand.place_id}"
            if group_active(cl_gid):
                ct = model.add(xp == 0)
                lits = enforce_lits(cl_gid)
                if lits:
                    ct.only_enforce_if(lits)

            ow_gid = f"OPENING_WINDOW:{aid}:{cand.place_id}"
            if group_active(ow_gid) and cand.windows_min:
                w_lits: list[cp_model.IntVar] = []
                for idx, (o_min, c_min) in enumerate(cand.windows_min):
                    wl = model.new_bool_var(f"win::{aid}::{cand.place_id}::{idx}")
                    model.add(start_vars[aid] >= o_min).only_enforce_if(wl)
                    model.add(end_vars[aid] <= c_min).only_enforce_if(wl)
                    w_lits.append(wl)
                ct = model.add(sum(w_lits) >= xp)
                lits = enforce_lits(ow_gid)
                if lits:
                    ct.only_enforce_if(lits)

            d_gid = f"DIETARY:{aid}:{cand.place_id}"
            if group_active(d_gid):
                ct = model.add(xp == 0)
                lits = enforce_lits(d_gid)
                if lits:
                    ct.only_enforce_if(lits)

    # 2b. Intra-day transit physics, zero-duration tie-breaker & daily active budget
    for day in ir.day_bins:
        iso_d = day.local_date.isoformat()
        nodes = list(day.act_ids)
        if not nodes:
            continue

        t_gid = f"DAY_TRANSIT_ORDER:{day.stay_id}:{day.local_date}"
        if group_active(t_gid):
            for u in nodes:
                su = ir.activities[u]
                for v in nodes:
                    if u == v:
                        continue
                    sv = ir.activities[v]
                    adj_uv = x_adj[(day.stay_id, iso_d, u, v)]
                    for cu in su.candidates:
                        for cv in sv.candidates:
                            t_min = day.transit_matrix_min.get((cu.place_id, cv.place_id), 0)
                            lits = enforce_lits(
                                t_gid,
                                [adj_uv, x_place[(u, cu.place_id)], x_place[(v, cv.place_id)]],
                            )
                            model.add(end_vars[u] + t_min <= start_vars[v]).only_enforce_if(lits)

                    # Zero-duration canonical tie-breaker (v7 unique-order proof)
                    if su.duration_min == 0 and sv.duration_min == 0 and su.id_rank > sv.id_rank:
                        lits = enforce_lits(t_gid, [adj_uv])
                        model.add(start_vars[v] >= start_vars[u] + 1).only_enforce_if(lits)

        b_gid = f"DAILY_BUDGET:{day.stay_id}:{day.local_date}"
        if group_active(b_gid):
            ct = model.add(
                sum(ir.activities[aid].duration_min * y_vars[aid] for aid in nodes)
                <= day.daily_active_limit_min
            )
            lits = enforce_lits(b_gid)
            if lits:
                ct.only_enforce_if(lits)

    # 2c. Cloned Per-Constraint Optional Intervals for ResourceNoOverlap (v6/v7 contract)
    for rspec in ir.resource_no_overlaps:
        if not group_active(rspec.group_id):
            continue
        cloned_intervals: list[cp_model.IntervalVar] = []
        for aid in rspec.act_ids:
            if aid not in ir.activities:
                continue
            spec = ir.activities[aid]
            if diagnostic_mode:
                # Exact Boolean conjunction: p_{k,v} <=> (y_v AND a_g)
                a_g = assump_vars[rspec.group_id]
                y_v = y_vars[aid]
                p_kv = model.new_bool_var(f"p_clone::{rspec.group_id}::{aid}")
                model.add_bool_and([y_v, a_g]).only_enforce_if(p_kv)
                model.add_bool_or([y_v.Not(), a_g.Not(), p_kv])
                iv = model.new_optional_interval_var(
                    start_vars[aid],
                    spec.duration_min,
                    end_vars[aid],
                    p_kv,
                    f"iv_diag::{rspec.group_id}::{aid}",
                )
            else:
                iv = model.new_optional_interval_var(
                    start_vars[aid],
                    spec.duration_min,
                    end_vars[aid],
                    y_vars[aid],
                    f"iv_opt::{rspec.group_id}::{aid}",
                )
            cloned_intervals.append(iv)
        if cloned_intervals:
            model.add_no_overlap(cloned_intervals)

    # 2d. Projected Global Constraints (100% linear/reifiable — NO AddAllDifferent)
    if ir.globals_ir.remaining_budget_usd is not None and group_active("GLOBAL_BUDGET"):
        cost_terms = [
            cand.cost_usd * x_place[(aid, cand.place_id)]
            for aid, spec in ir.activities.items()
            for cand in spec.candidates
        ]
        ct = model.add(sum(cost_terms) <= ir.globals_ir.remaining_budget_usd)
        lits = enforce_lits("GLOBAL_BUDGET")
        if lits:
            ct.only_enforce_if(lits)

    if group_active("GLOBAL_ANTI_DUP"):
        lits = enforce_lits("GLOBAL_ANTI_DUP")
        by_pid: dict[str, list[cp_model.IntVar]] = {}
        for (aid, pid), xp in x_place.items():
            if pid in ir.globals_ir.frozen_place_ids:
                ct = model.add(xp == 0)
                if lits:
                    ct.only_enforce_if(lits)
            else:
                by_pid.setdefault(pid, []).append(xp)
        for pid, vars_for_pid in by_pid.items():
            if len(vars_for_pid) > 1:
                ct = model.add(sum(vars_for_pid) <= 1)
                if lits:
                    ct.only_enforce_if(lits)

    for cat, rem_q in ir.globals_ir.remaining_cat_quotas.items():
        gid = f"GLOBAL_CAT_QUOTA:{cat}"
        if not group_active(gid):
            continue
        cat_vars = [
            x_place[(aid, cand.place_id)]
            for aid, spec in ir.activities.items()
            for cand in spec.candidates
            if cand.category == cat
        ]
        ct = model.add(sum(cat_vars) <= rem_q)
        lits = enforce_lits(gid)
        if lits:
            ct.only_enforce_if(lits)

    # ------------------------------------------------------------------
    # 3. EXACT DYNAMIC HIERARCHICAL OBJECTIVE (OptimizationModel ONLY)
    # ------------------------------------------------------------------
    w1, w2, w3 = 0, 0, 1
    if not diagnostic_mode:
        u1 = len(droppable_act_ids)
        u2 = len(ir.activities)
        u3 = (
            sum(max_disp_bounds)
            + sum(pen for pen, _ in same_cat_vars)
            + sum(pen for pen, _ in rank_penalty_terms)
        )

        w3 = 1
        w2 = u3 + 1
        w1 = w2 * (u2 + 1)

        max_obj = w1 * u1 + w2 * u2 + w3 * u3
        if max_obj >= (1 << 53):
            raise ObjectiveOverflowError(
                f"Dynamic hierarchical objective max ({max_obj}) exceeds 2**53 safety cap."
            )

        j1_expr = sum(1 - y_vars[aid] for aid in droppable_act_ids)
        j2_expr = sum(changed_vars.values())
        j3_expr = (
            sum(disp_vars.values())
            + sum(pen * var for pen, var in same_cat_vars)
            + sum(pen * var for pen, var in rank_penalty_terms)
        )
        model.minimize(w1 * j1_expr + w2 * j2_expr + w3 * j3_expr)

    return _BuiltModel(
        model=model,
        y_vars=y_vars,
        start_vars=start_vars,
        end_vars=end_vars,
        x_place=x_place,
        x_adj=x_adj,
        changed_vars=changed_vars,
        disp_vars=disp_vars,
        same_cat_vars=same_cat_vars,
        assump_vars=assump_vars,
        index_to_group_id=index_to_group_id,
        w1=w1,
        w2=w2,
        w3=w3,
    )


def solve_optimization_only(
    ir: CompiledConstraintIR,
    *,
    active_group_ids: frozenset[str] | None = None,
    timeout_sec: float = 0.75,
    num_workers: int = 2,
) -> tuple[int, _BuiltModel, cp_model.CpSolver]:
    """Run OptimizationModel (used both directly and by dual-model equivalence tests)."""
    built = _build_cp_model(ir, diagnostic_mode=False, active_group_ids=active_group_ids)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = timeout_sec
    solver.parameters.num_search_workers = num_workers
    solver.parameters.random_seed = 42
    status = solver.solve(built.model)
    return status, built, solver


def _extract_solution(
    ir: CompiledConstraintIR, built: _BuiltModel, solver: cp_model.CpSolver
) -> tuple[dict[str, SolvedActivityAssignment], dict[tuple[str, str], tuple[str, ...]], int]:
    assignments: dict[str, SolvedActivityAssignment] = {}
    for aid, spec in ir.activities.items():
        present = bool(solver.value(built.y_vars[aid]))
        if not present:
            assignments[aid] = SolvedActivityAssignment(
                act_id=aid,
                present=False,
                place_id=None,
                start_utc=None,
                end_utc=None,
                changed=True,
            )
            continue
        chosen_pid = next(
            cand.place_id
            for cand in spec.candidates
            if solver.value(built.x_place[(aid, cand.place_id)]) == 1
        )
        s_min = int(solver.value(built.start_vars[aid]))
        e_min = int(solver.value(built.end_vars[aid]))
        assignments[aid] = SolvedActivityAssignment(
            act_id=aid,
            present=True,
            place_id=chosen_pid,
            start_utc=ir.epoch_origin_utc + s_min * 60,
            end_utc=ir.epoch_origin_utc + e_min * 60,
            changed=bool(solver.value(built.changed_vars[aid])),
        )

    # Reconstruct exact Hamiltonian path order on each dirty day
    day_orders: dict[tuple[str, str], tuple[str, ...]] = {}
    for day in ir.day_bins:
        iso_d = day.local_date.isoformat()
        nodes = list(day.act_ids)
        cur = "__START__"
        order: list[str] = []
        visited: set[str] = set()
        while True:
            nxt = None
            for cand_v in [*nodes, "__END__"]:
                if cand_v == cur:
                    continue
                if solver.value(built.x_adj[(day.stay_id, iso_d, cur, cand_v)]) == 1:
                    nxt = cand_v
                    break
            assert nxt is not None, f"Broken adjacency path from {cur} on {iso_d}"
            if nxt == "__END__":
                break
            assert nxt not in visited, f"Subtour detected at {nxt} on {iso_d}"
            visited.add(nxt)
            order.append(nxt)
            cur = nxt
        day_orders[(day.stay_id, iso_d)] = tuple(order)

    markov_pen = sum(pen * int(solver.value(var)) for pen, var in built.same_cat_vars)
    return assignments, day_orders, markov_pen


def solve_ir(
    ir: CompiledConstraintIR,
    *,
    opt_timeout_sec: float = 0.75,
    diag_total_budget_ms: float = 250.0,
    diag_initial_sec: float = 0.070,
    mus_per_probe_sec: float = 0.020,
    mus_max_probes: int = 6,
    run_mus: bool = True,
    opt_workers: int = 2,
) -> DualSolveResult:
    """Execute OptimizationModel -> (if INFEASIBLE) DiagnosticFeasibilityModel + Bounded MUS."""
    t0 = pytime.perf_counter()
    status, opt_built, opt_solver = solve_optimization_only(
        ir, timeout_sec=opt_timeout_sec, num_workers=opt_workers
    )
    opt_ms = (pytime.perf_counter() - t0) * 1000.0

    if status == cp_model.MODEL_INVALID:
        return DualSolveResult(
            certificate="INTERNAL_SOLVER_MODEL_ERROR",
            assignments={},
            day_orders={},
            opt_wall_ms=opt_ms,
        )

    if status == cp_model.UNKNOWN:
        return DualSolveResult(
            certificate="INDETERMINATE_SOLVER_TIMEOUT",
            assignments={},
            day_orders={},
            opt_wall_ms=opt_ms,
        )

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments, day_orders, markov_pen = _extract_solution(ir, opt_built, opt_solver)
        inc_obj = int(round(opt_solver.objective_value))
        best_bnd = int(round(opt_solver.best_objective_bound))
        tier1_opt = (
            (inc_obj // opt_built.w1 == best_bnd // opt_built.w1)
            if opt_built.w1 > 0
            else True
        )
        cert: SolverCertificate = (
            "PROVEN_OPTIMAL_IN_SCOPE"
            if status == cp_model.OPTIMAL
            else "FEASIBLE_OPTIMALITY_UNPROVEN"
        )
        return DualSolveResult(
            certificate=cert,
            assignments=assignments,
            day_orders=day_orders,
            incumbent_objective=inc_obj,
            best_bound=best_bnd,
            tier1_proven_optimal=tier1_opt,
            markov_penalty_value=markov_pen,
            opt_wall_ms=opt_ms,
        )

    # ------------------------------------------------------------------
    # OptimizationModel proved INFEASIBLE -> Build DiagnosticFeasibilityModel
    # (Single worker, ZERO objective, strict assumptions + bounded MUS)
    # ------------------------------------------------------------------
    d_start = pytime.perf_counter()
    diag_built = _build_cp_model(ir, diagnostic_mode=True)
    all_group_ids = sorted(ir.groups.keys())
    diag_built.model.add_assumptions([diag_built.assump_vars[g] for g in all_group_ids])

    diag_solver = cp_model.CpSolver()
    diag_solver.parameters.num_search_workers = 1  # Mandatory for OR-Tools assumptions
    diag_solver.parameters.max_time_in_seconds = diag_initial_sec
    diag_solver.parameters.random_seed = 42
    d_status = diag_solver.solve(diag_built.model)

    if d_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Runtime Equivalence Guard: diagnostic model MUST NOT disagree with opt model!
        return DualSolveResult(
            certificate="INTERNAL_MODEL_EQUIVALENCE_ERROR",
            assignments={},
            day_orders={},
            opt_wall_ms=opt_ms,
            diag_wall_ms=(pytime.perf_counter() - d_start) * 1000.0,
        )

    if d_status != cp_model.INFEASIBLE:
        return DualSolveResult(
            certificate="INFEASIBLE_CAUSE_UNCLASSIFIED",
            assignments={},
            day_orders={},
            core_minimality="EXPLANATION_TIMEOUT",
            opt_wall_ms=opt_ms,
            diag_wall_ms=(pytime.perf_counter() - d_start) * 1000.0,
        )

    raw_core_indices = list(diag_solver.sufficient_assumptions_for_infeasibility())
    core_ids = [
        diag_built.index_to_group_id[idx]
        for idx in raw_core_indices
        if idx in diag_built.index_to_group_id
    ]

    # v7 Empty-Core Guard: base axioms alone are contradictory -> NEVER physical travel conflict
    if not core_ids:
        return DualSolveResult(
            certificate="INTERNAL_BASE_AXIOM_CONTRADICTION",
            assignments={},
            day_orders={},
            core_groups=(),
            core_minimality="EMPTY_BASE_AXIOM_CORE",
            opt_wall_ms=opt_ms,
            diag_wall_ms=(pytime.perf_counter() - d_start) * 1000.0,
        )

    # Bounded Deletion-Filter MUS Pass (<= 6 probes x 20ms within 250ms total budget)
    minimality: CoreMinimality = "SUFFICIENT_NOT_PROVEN_MINIMAL"
    probes_run = 0
    if run_mus:
        shrunk = list(core_ids)
        all_tested_necessary = len(shrunk) <= mus_max_probes
        idx = 0
        while idx < len(shrunk) and probes_run < mus_max_probes:
            elapsed_ms = (pytime.perf_counter() - d_start) * 1000.0
            if elapsed_ms + (mus_per_probe_sec * 1000.0) > diag_total_budget_ms:
                all_tested_necessary = False
                break

            candidate_drop = shrunk[idx]
            probe_on = set(shrunk) - {candidate_drop}
            diag_built.model.clear_assumptions()
            diag_built.model.add_assumptions(
                [diag_built.assump_vars[g] for g in all_group_ids if g in probe_on]
                + [diag_built.assump_vars[g].Not() for g in all_group_ids if g not in probe_on]
            )
            probe_solver = cp_model.CpSolver()
            probe_solver.parameters.num_search_workers = 1
            probe_solver.parameters.max_time_in_seconds = mus_per_probe_sec
            probe_solver.parameters.random_seed = 42
            p_status = probe_solver.solve(diag_built.model)
            probes_run += 1

            if p_status == cp_model.INFEASIBLE:
                # Redundant assumption: permanently remove it
                shrunk.pop(idx)
            elif p_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # Necessary assumption: keep it and advance
                idx += 1
            else:
                all_tested_necessary = False
                idx += 1

        core_ids = shrunk
        if all_tested_necessary and idx == len(core_ids) and len(core_ids) <= mus_max_probes:
            minimality = "PROVEN_MUS"
        else:
            minimality = "SUFFICIENT_NOT_PROVEN_MINIMAL"

    core_specs = tuple(ir.groups[g] for g in core_ids)
    has_policy_gate = any(g.origin == "VERIFICATION_POLICY_GATE" for g in core_specs)
    final_cert: SolverCertificate = (
        "INFEASIBLE_UNDER_VERIFICATION_POLICY"
        if has_policy_gate
        else "PROVEN_PHYSICALLY_INFEASIBLE"
    )

    return DualSolveResult(
        certificate=final_cert,
        assignments={},
        day_orders={},
        core_groups=core_specs,
        core_minimality=minimality,
        opt_wall_ms=opt_ms,
        diag_wall_ms=(pytime.perf_counter() - d_start) * 1000.0,
        probes_executed=probes_run,
    )
