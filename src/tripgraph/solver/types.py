"""Pure-Python CP-SAT solver result DTOs (zero ortools imports).

Imported by runtime, orchestrator, supervisor, and repair modules so the API
process never transitively loads native OR-Tools libraries into sys.modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tripgraph.solver.ir import AssumptionGroupSpec

SolverCertificate = Literal[
    "PROVEN_OPTIMAL_IN_SCOPE",
    "FEASIBLE_OPTIMALITY_UNPROVEN",
    "PROVEN_PHYSICALLY_INFEASIBLE",
    "INFEASIBLE_UNDER_VERIFICATION_POLICY",
    "INFEASIBLE_CAUSE_UNCLASSIFIED",
    "INDETERMINATE_SOLVER_TIMEOUT",
    "INTERNAL_BASE_AXIOM_CONTRADICTION",
    "INTERNAL_MODEL_EQUIVALENCE_ERROR",
    "INTERNAL_SOLVER_MODEL_ERROR",
    "NOT_INVOKED_VERIFY_ONLY",
]

CoreMinimality = Literal[
    "NOT_APPLICABLE",
    "PROVEN_MUS",
    "SUFFICIENT_NOT_PROVEN_MINIMAL",
    "EXPLANATION_TIMEOUT",
    "EMPTY_BASE_AXIOM_CORE",
]


class ObjectiveOverflowError(RuntimeError):
    """Raised when dynamically computed hierarchical objective exceeds 2**53."""


@dataclass(frozen=True)
class SolvedActivityAssignment:
    act_id: str
    present: bool
    place_id: str | None
    start_utc: int | None
    end_utc: int | None
    changed: bool


@dataclass(frozen=True)
class DualSolveResult:
    certificate: SolverCertificate
    assignments: dict[str, SolvedActivityAssignment]
    day_orders: dict[tuple[str, str], tuple[str, ...]]  # (stay_id, iso_date) -> ordered active act_ids
    core_groups: tuple[AssumptionGroupSpec, ...] = ()
    core_minimality: CoreMinimality = "NOT_APPLICABLE"
    incumbent_objective: int | None = None
    best_bound: int | None = None
    tier1_proven_optimal: bool | None = None
    markov_penalty_value: int | None = None
    opt_wall_ms: float = 0.0
    diag_wall_ms: float = 0.0
    probes_executed: int = 0
