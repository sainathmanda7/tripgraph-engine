"""SHA-256 node freeze locks, LockGuard enforcement, and Blast-Radius diffing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel

from tripgraph.schema.models import ActivityNode, CityStay, TransitLeg, TripGraph


class LockViolationError(RuntimeError):
    """Raised when a candidate graph mutates a frozen or locked_by_user node."""


def canonical_node_hash(node: BaseModel) -> str:
    """Compute a deterministic SHA-256 digest of a Pydantic node's canonical JSON."""
    payload = node.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FreezeSnapshot:
    """Immutable SHA-256 fingerprints of all frozen nodes and user-locked nodes."""

    frozen_stay_hashes: dict[str, str]
    frozen_leg_hashes: dict[str, str]
    frozen_activity_hashes: dict[str, str]
    user_locked_activity_hashes: dict[str, str]
    user_locked_stay_hashes: dict[str, str]
    user_locked_leg_hashes: dict[str, str]

    @classmethod
    def capture(
        cls,
        graph: TripGraph,
        dirty_stay_ids: frozenset[str],
        dirty_leg_ids: frozenset[str],
        dirty_activity_ids: frozenset[str],
    ) -> FreezeSnapshot:
        return cls(
            frozen_stay_hashes={
                s.id: canonical_node_hash(s)
                for s in graph.stays
                if s.id not in dirty_stay_ids
            },
            frozen_leg_hashes={
                l.id: canonical_node_hash(l)
                for l in graph.transit_legs
                if l.id not in dirty_leg_ids
            },
            frozen_activity_hashes={
                a.id: canonical_node_hash(a)
                for a in graph.activities
                if a.id not in dirty_activity_ids
            },
            user_locked_stay_hashes={
                s.id: canonical_node_hash(s) for s in graph.stays if s.locked_by_user
            },
            user_locked_leg_hashes={
                l.id: canonical_node_hash(l) for l in graph.transit_legs if l.locked_by_user
            },
            user_locked_activity_hashes={
                a.id: canonical_node_hash(a) for a in graph.activities if a.locked_by_user
            },
        )


def verify_lock_guard(snapshot: FreezeSnapshot, candidate: TripGraph) -> list[str]:
    """Return a list of freeze/user-lock violations; empty list means 100% preserved."""
    violations: list[str] = []

    cand_stays: dict[str, CityStay] = {s.id: s for s in candidate.stays}
    cand_legs: dict[str, TransitLeg] = {l.id: l for l in candidate.transit_legs}
    cand_acts: dict[str, ActivityNode] = {a.id: a for a in candidate.activities}

    def _check_map(
        expected: dict[str, str], actual_map: dict[str, BaseModel], label: str
    ) -> None:
        for node_id, exp_hash in expected.items():
            node = actual_map.get(node_id)
            if node is None:
                violations.append(f"{label}_DROPPED:{node_id}")
            elif canonical_node_hash(node) != exp_hash:
                violations.append(f"{label}_MUTATED:{node_id}")

    _check_map(snapshot.frozen_stay_hashes, cand_stays, "FROZEN_STAY")
    _check_map(snapshot.frozen_leg_hashes, cand_legs, "FROZEN_LEG")
    _check_map(snapshot.frozen_activity_hashes, cand_acts, "FROZEN_ACTIVITY")
    _check_map(snapshot.user_locked_stay_hashes, cand_stays, "USER_LOCKED_STAY")
    _check_map(snapshot.user_locked_leg_hashes, cand_legs, "USER_LOCKED_LEG")
    _check_map(snapshot.user_locked_activity_hashes, cand_acts, "USER_LOCKED_ACTIVITY")

    return violations


def assert_lock_guard(snapshot: FreezeSnapshot, candidate: TripGraph) -> None:
    """Hard commit gate: raises LockViolationError if any frozen or locked node changed."""
    violations = verify_lock_guard(snapshot, candidate)
    if violations:
        raise LockViolationError(f"LockGuard blocked commit: {violations}")


@dataclass(frozen=True)
class BlastRadiusReport:
    """Separates semantic churn (IDs/venues changed) from temporal churn (>5m shift)."""

    unaffected_total: int
    semantic_changed_count: int
    temporal_changed_count: int
    semantic_blast_radius: float
    temporal_blast_radius: float
    changed_node_ids: tuple[str, ...]


def compute_blast_radius(
    before: TripGraph,
    after: TripGraph,
    logical_scope_ids: frozenset[str],
    *,
    temporal_tolerance_sec: int = 300,
) -> BlastRadiusReport:
    """Measure unintended churn strictly over nodes OUTSIDE logical_scope_ids."""
    after_stays = {s.id: s for s in after.stays}
    after_legs = {l.id: l for l in after.transit_legs}
    after_acts = {a.id: a for a in after.activities}

    unaffected_total = 0
    semantic_changed = 0
    temporal_changed = 0
    changed_ids: list[str] = []

    for s in before.stays:
        if s.id in logical_scope_ids:
            continue
        unaffected_total += 1
        ns = after_stays.get(s.id)
        if ns is None or ns.hotel.hotel_id != s.hotel.hotel_id:
            semantic_changed += 1
            changed_ids.append(s.id)
        elif ns.check_in_date != s.check_in_date or ns.check_out_date != s.check_out_date:
            temporal_changed += 1
            changed_ids.append(s.id)

    for leg in before.transit_legs:
        if leg.id in logical_scope_ids:
            continue
        unaffected_total += 1
        nl = after_legs.get(leg.id)
        if nl is None or nl.carrier_slot_id != leg.carrier_slot_id:
            semantic_changed += 1
            changed_ids.append(leg.id)
        elif abs(nl.depart_utc - leg.depart_utc) > temporal_tolerance_sec:
            temporal_changed += 1
            changed_ids.append(leg.id)

    for act in before.activities:
        if act.id in logical_scope_ids:
            continue
        unaffected_total += 1
        na = after_acts.get(act.id)
        if na is None or na.place_id != act.place_id:
            semantic_changed += 1
            changed_ids.append(act.id)
        elif (
            na.local_date != act.local_date
            or abs(na.start_utc - act.start_utc) > temporal_tolerance_sec
        ):
            temporal_changed += 1
            changed_ids.append(act.id)

    denom = max(1, unaffected_total)
    return BlastRadiusReport(
        unaffected_total=unaffected_total,
        semantic_changed_count=semantic_changed,
        temporal_changed_count=temporal_changed,
        semantic_blast_radius=semantic_changed / denom if unaffected_total else 0.0,
        temporal_blast_radius=temporal_changed / denom if unaffected_total else 0.0,
        changed_node_ids=tuple(sorted(changed_ids)),
    )
