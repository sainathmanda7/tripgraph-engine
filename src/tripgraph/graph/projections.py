"""Exact global hard-constraint & soft-objective projections (TripGraph v7).

Contract:
- Hard constraint projection is EXACT iff:
    evaluate_full(frozen, dirty) == evaluate_projected(summary(frozen), dirty)
- Soft objective projection is EXACT iff there exists constant C(frozen) such that:
    evaluate_full(frozen, dirty) == C(frozen) + evaluate_projected(summary(frozen), dirty)
  (verified via exact difference invariance across all candidate assignments).
- Higher-order sequence objectives (e.g. trip-wide daily variance) are marked
  NON_PROJECTABLE and rejected from incremental solving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from tripgraph.schema.models import ActivityNode, Place

ProjectionClass = Literal["EXACT", "NON_PROJECTABLE"]


class HardProjectionSpec(Protocol):
    name: str
    projection_class: ProjectionClass

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool: ...
    def extract_summary(self, frozen_acts: tuple[ActivityNode, ...]) -> Any: ...
    def evaluate_projected(self, summary: Any, dirty_acts: tuple[ActivityNode, ...]) -> bool: ...


class SoftObjectiveProjectionSpec(Protocol):
    name: str
    projection_class: ProjectionClass

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> int: ...
    def extract_summary(
        self,
        frozen_acts: tuple[ActivityNode, ...],
        dirty_days: frozenset[tuple[str, date]],
    ) -> Any: ...
    def evaluate_projected(
        self,
        summary: Any,
        dirty_day_all_acts: tuple[ActivityNode, ...],
    ) -> int: ...


# ---------------------------------------------------------------------------
# 1. EXACT Global Hard Constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TotalBudgetProjection:
    total_budget_usd: int
    name: str = "total_budget"
    projection_class: ProjectionClass = "EXACT"

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool:
        return sum(a.cost_usd for a in all_acts) <= self.total_budget_usd

    def extract_summary(self, frozen_acts: tuple[ActivityNode, ...]) -> int:
        return self.total_budget_usd - sum(a.cost_usd for a in frozen_acts)

    def evaluate_projected(self, summary: int, dirty_acts: tuple[ActivityNode, ...]) -> bool:
        return sum(a.cost_usd for a in dirty_acts) <= summary


@dataclass(frozen=True)
class AntiDuplicationProjection:
    name: str = "trip_anti_duplication"
    projection_class: ProjectionClass = "EXACT"

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool:
        pids = [a.place_id for a in all_acts]
        return len(pids) == len(set(pids))

    def extract_summary(
        self, frozen_acts: tuple[ActivityNode, ...]
    ) -> tuple[bool, frozenset[str]]:
        pids = [a.place_id for a in frozen_acts]
        frozen_ok = len(pids) == len(set(pids))
        return (frozen_ok, frozenset(pids))

    def evaluate_projected(
        self,
        summary: tuple[bool, frozenset[str]],
        dirty_acts: tuple[ActivityNode, ...],
    ) -> bool:
        frozen_ok, frozen_pids = summary
        if not frozen_ok:
            return False
        dirty_pids = [a.place_id for a in dirty_acts]
        if len(dirty_pids) != len(set(dirty_pids)):
            return False
        return frozen_pids.isdisjoint(dirty_pids)


@dataclass(frozen=True)
class MaxPerCategoryProjection:
    max_per_category: dict[str, int]
    name: str = "max_per_category"
    projection_class: ProjectionClass = "EXACT"

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool:
        counts: dict[str, int] = {}
        for a in all_acts:
            counts[a.category] = counts.get(a.category, 0) + 1
        return all(counts.get(cat, 0) <= lim for cat, lim in self.max_per_category.items())

    def extract_summary(self, frozen_acts: tuple[ActivityNode, ...]) -> dict[str, int]:
        frozen_counts: dict[str, int] = {}
        for a in frozen_acts:
            frozen_counts[a.category] = frozen_counts.get(a.category, 0) + 1
        return {
            cat: lim - frozen_counts.get(cat, 0)
            for cat, lim in self.max_per_category.items()
        }

    def evaluate_projected(
        self, summary: dict[str, int], dirty_acts: tuple[ActivityNode, ...]
    ) -> bool:
        if any(rem < 0 for rem in summary.values()):
            return False
        dirty_counts: dict[str, int] = {}
        for a in dirty_acts:
            dirty_counts[a.category] = dirty_counts.get(a.category, 0) + 1
        return all(dirty_counts.get(cat, 0) <= rem for cat, rem in summary.items())


@dataclass(frozen=True)
class OncePerTripTagProjection:
    once_tags: frozenset[str]
    name: str = "once_per_trip_tags"
    projection_class: ProjectionClass = "EXACT"

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool:
        counts: dict[str, int] = {}
        for a in all_acts:
            for t in a.tags:
                if t in self.once_tags:
                    counts[t] = counts.get(t, 0) + 1
        return all(c <= 1 for c in counts.values())

    def extract_summary(
        self, frozen_acts: tuple[ActivityNode, ...]
    ) -> tuple[bool, frozenset[str]]:
        counts: dict[str, int] = {}
        for a in frozen_acts:
            for t in a.tags:
                if t in self.once_tags:
                    counts[t] = counts.get(t, 0) + 1
        frozen_ok = all(c <= 1 for c in counts.values())
        used_in_frozen = frozenset(t for t, c in counts.items() if c >= 1)
        return (frozen_ok, used_in_frozen)

    def evaluate_projected(
        self,
        summary: tuple[bool, frozenset[str]],
        dirty_acts: tuple[ActivityNode, ...],
    ) -> bool:
        frozen_ok, used_in_frozen = summary
        if not frozen_ok:
            return False
        dirty_seen: set[str] = set()
        for a in dirty_acts:
            for t in a.tags:
                if t not in self.once_tags:
                    continue
                if t in used_in_frozen or t in dirty_seen:
                    return False
                dirty_seen.add(t)
        return True


@dataclass(frozen=True)
class GlobalDietaryProjection:
    required_dietary_tags: frozenset[str]
    place_registry: dict[str, Place]
    name: str = "global_dietary"
    projection_class: ProjectionClass = "EXACT"

    def _meal_ok(self, a: ActivityNode) -> bool:
        if not a.is_meal or not self.required_dietary_tags:
            return True
        p = self.place_registry.get(a.place_id)
        return p is not None and self.required_dietary_tags.issubset(set(p.dietary_tags))

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> bool:
        return all(self._meal_ok(a) for a in all_acts)

    def extract_summary(self, frozen_acts: tuple[ActivityNode, ...]) -> bool:
        return all(self._meal_ok(a) for a in frozen_acts)

    def evaluate_projected(self, summary: bool, dirty_acts: tuple[ActivityNode, ...]) -> bool:
        return summary and all(self._meal_ok(a) for a in dirty_acts)


# ---------------------------------------------------------------------------
# 2. EXACT Soft-Objective Projections (v6/v7 Dynamic Topology Compliant)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderInvariantCategoryPenaltyProjection:
    """Convex quadratic penalty on trip-wide category clustering: sum_k count(k)^2."""

    weight: int = 10
    name: str = "category_count_quadratic_soft"
    projection_class: ProjectionClass = "EXACT"

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> int:
        counts: dict[str, int] = {}
        for a in all_acts:
            counts[a.category] = counts.get(a.category, 0) + 1
        return self.weight * sum(c * c for c in counts.values())

    def extract_summary(
        self,
        frozen_acts: tuple[ActivityNode, ...],
        dirty_days: frozenset[tuple[str, date]],
    ) -> dict[str, int]:
        # Only nodes on non-dirty days are strictly excluded from dirty_day_all_acts
        counts: dict[str, int] = {}
        for a in frozen_acts:
            if (a.stay_id, a.local_date) not in dirty_days:
                counts[a.category] = counts.get(a.category, 0) + 1
        return counts

    def evaluate_projected(
        self,
        summary: dict[str, int],
        dirty_day_all_acts: tuple[ActivityNode, ...],
    ) -> int:
        # Exact delta: phi(N_F + N_D) - phi(N_F)
        dirty_counts: dict[str, int] = {}
        for a in dirty_day_all_acts:
            dirty_counts[a.category] = dirty_counts.get(a.category, 0) + 1
        delta = 0
        for cat, nd in dirty_counts.items():
            nf = summary.get(cat, 0)
            delta += (nf + nd) ** 2 - nf ** 2
        return self.weight * delta


@dataclass(frozen=True)
class IntraDayMarkovAdjacencyProjection:
    """Exact Order-1 intra-day Markov penalty (same-category back-to-back penalty).

    v6/v7 Exactness Rule:
    - Evaluates consecutive pairs in canonical order (start_utc, end_utc, id)
      strictly within each (stay_id, local_date) day bin.
    - Because EVERY node (mutable + locked_by_user anchors) on every dirty day
      is passed in dirty_day_all_acts, reordering around locked anchors or
      dropping intermediate nodes updates adjacency EXACTLY, while untouched
      days contribute constant C(frozen).
    """

    same_category_penalty: int = 25
    name: str = "intraday_markov_same_category"
    projection_class: ProjectionClass = "EXACT"

    def _score_day(self, day_acts: list[ActivityNode]) -> int:
        ordered = sorted(day_acts, key=lambda a: (a.start_utc, a.end_utc, a.id))
        penalty = 0
        for u, v in zip(ordered, ordered[1:]):
            if u.category == v.category:
                penalty += self.same_category_penalty
        return penalty

    def evaluate_full(self, all_acts: tuple[ActivityNode, ...]) -> int:
        by_day: dict[tuple[str, date], list[ActivityNode]] = {}
        for a in all_acts:
            by_day.setdefault((a.stay_id, a.local_date), []).append(a)
        return sum(self._score_day(acts) for acts in by_day.values())

    def extract_summary(
        self,
        frozen_acts: tuple[ActivityNode, ...],
        dirty_days: frozenset[tuple[str, date]],
    ) -> frozenset[tuple[str, date]]:
        # Untouched days' Markov costs are completely invariant over x_D -> C(frozen)
        return dirty_days

    def evaluate_projected(
        self,
        summary: frozenset[tuple[str, date]],
        dirty_day_all_acts: tuple[ActivityNode, ...],
    ) -> int:
        by_day: dict[tuple[str, date], list[ActivityNode]] = {}
        for a in dirty_day_all_acts:
            day_key = (a.stay_id, a.local_date)
            assert day_key in summary, f"Unexpected non-dirty day node {a.id} in projected eval"
            by_day.setdefault(day_key, []).append(a)
        return sum(self._score_day(acts) for acts in by_day.values())


@dataclass(frozen=True)
class TripPacingVarianceObjective:
    """Example NON_PROJECTABLE higher-order objective (escalates to GlobalReplanMode)."""

    name: str = "trip_pacing_variance"
    projection_class: ProjectionClass = "NON_PROJECTABLE"
