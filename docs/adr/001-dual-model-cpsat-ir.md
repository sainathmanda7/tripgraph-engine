# ADR-001: Dual-Model CP-SAT Compilation from a Single Shared IR

- **Status:** Accepted
- **Date:** 2026-11
- **Files:** `src/tripgraph/solver/ir.py`, `src/tripgraph/solver/cpsat_engine.py`

## Context
TripGraph requires two solver behaviors that OR-Tools CP-SAT cannot legally combine in one `CpModel`:
1. Fast multi-worker hierarchical optimization (`num_search_workers=2`, minimizing drops >> churn >> displacement + Markov penalties).
2. Exact infeasibility explanation (`AddAssumptions` + `sufficient_assumptions_for_infeasibility()`), which OR-Tools requires to run single-worker (`num_search_workers=1`) with **zero objective**—and where shared `OptionalIntervalVar` objects leak across assumption groups if reused.

## Decision
Compile every dirty subgraph into one immutable `CompiledConstraintIR` (keeping policy-blocked venues inside the variable domain via explicit `VERIFICATION_POLICY_GATE` assumption groups—zero silent pruning). Materialize:
1. `OptimizationModel` first (multi-worker, dynamically proven weights `W1*U1 + W2*U2 + W3*U3 < 2^53`).
2. `DiagnosticFeasibilityModel` **only when `OptimizationModel` returns `INFEASIBLE`**, using cloned per-constraint optional intervals (`p_{k,v} <=> y_v AND a_g`) and a bounded 6-probe (`<= 250 ms`) deletion-filter MUS pass.

## Alternatives Considered
- **Always run single-worker feasibility model first:** Doubles latency on feasible edits (the common path).
- **Silent domain pruning of policy-blocked venues:** Collapses policy rejections (`HOLIDAY_UNVERIFIED`) into physical infeasibility, misleading travelers into believing an open venue is closed.

## Consequences
- **Positive:** Zero diagnostic overhead on feasible edits; exact separation of `PROVEN_PHYSICALLY_INFEASIBLE`, `INFEASIBLE_UNDER_VERIFICATION_POLICY`, and empty-core `INTERNAL_BASE_AXIOM_CONTRADICTION`; verified across 60 randomized fuzz tests in CI.
- **Negative:** Requires strict semantic parity inside `_build_cp_model()` backed by a runtime equivalence guard (`INTERNAL_MODEL_EQUIVALENCE_ERROR`).
