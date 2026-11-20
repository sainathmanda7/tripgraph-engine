"""Full K3 TripGraph Verifier & Byte-Level TZDB Drift Audit (TripGraph v7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from tripgraph.schema.models import (
    Place,
    TripGraph,
    TruthValue,
)
from tripgraph.time.tz_registry import PinnedTzRegistry
from tripgraph.verifier.evidence import resolve_place_availability
from tripgraph.verifier.hotel import evaluate_hotel_arrival

VerificationCertificate = Literal[
    "VERIFIED_TRUE",
    "CONDITIONAL_ON_ASSUMPTIONS",
    "VIOLATED_FALSE",
]


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    node_ids: tuple[str, ...]
    truth: TruthValue
    code: str
    detail: str
    witness_record_id: str | None = None


@dataclass(frozen=True)
class VerificationReport:
    hard_constraints_status: TruthValue
    verification_certificate: VerificationCertificate
    checks: tuple[CheckResult, ...]
    assumptions_accepted: tuple[CheckResult, ...]
    violations: tuple[CheckResult, ...]
    drifted_timezones: tuple[str, ...]


def audit_tzdb_drift(graph: TripGraph, tz_registry: PinnedTzRegistry) -> tuple[str, ...]:
    """Compare persisted tzif_sha256 pins against live runtime TZif bytes."""
    drifted: list[str] = []
    for tz_iana, pin in graph.tz_pins.items():
        current = tz_registry.get(tz_iana)
        if current.tzif_sha256 != pin.tzif_sha256:
            drifted.append(tz_iana)
    return tuple(sorted(drifted))


def kleene_and(values: list[TruthValue]) -> TruthValue:
    """Kleene K3 conjunction: any FALSE -> FALSE; all TRUE -> TRUE; else UNKNOWN."""
    if any(v == TruthValue.FALSE for v in values):
        return TruthValue.FALSE
    if all(v == TruthValue.TRUE for v in values):
        return TruthValue.TRUE
    return TruthValue.UNKNOWN


def verify_trip_graph(
    graph: TripGraph,
    place_registry: dict[str, Place],
    transit_minutes_fn,
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    *,
    public_holidays_by_city: dict[str, frozenset[date]] | None = None,
) -> VerificationReport:
    """Run full deterministic K3 verification over a TripGraph."""
    holidays_map = public_holidays_by_city or {}
    checks: list[CheckResult] = []
    drifted = audit_tzdb_drift(graph, tz_registry)
    if drifted:
        checks.append(
            CheckResult(
                check_id="tzdb_drift",
                node_ids=drifted,
                truth=TruthValue.FALSE,
                code="TZDB_BYTES_DRIFTED",
                detail=f"Loaded TZif bytes changed for: {', '.join(drifted)}",
            )
        )

    stays_by_id = {s.id: s for s in graph.stays}

    # 1. Entity grounding, city membership, dietary tags & K3 venue availability
    seen_places: dict[str, str] = {}
    cat_counts: dict[str, int] = {}
    once_tags_seen: dict[str, str] = {}

    for act in graph.activities:
        place = place_registry.get(act.place_id)
        if place is None:
            checks.append(
                CheckResult(
                    check_id=f"grounding:{act.id}",
                    node_ids=(act.id,),
                    truth=TruthValue.FALSE,
                    code="UNKNOWN_PLACE_ID",
                    detail=f"Place {act.place_id!r} not in PlaceRegistry.",
                )
            )
            continue

        if place.city_id != act.city_id:
            checks.append(
                CheckResult(
                    check_id=f"city_match:{act.id}",
                    node_ids=(act.id,),
                    truth=TruthValue.FALSE,
                    code="WRONG_CITY_ENTITY",
                    detail=f"Place {place.place_id} belongs to {place.city_id}, not {act.city_id}.",
                )
            )

        # Trip-wide anti-duplication
        if place.place_id in seen_places:
            checks.append(
                CheckResult(
                    check_id=f"antidup:{act.id}",
                    node_ids=(seen_places[place.place_id], act.id),
                    truth=TruthValue.FALSE,
                    code="DUPLICATE_PLACE_ID",
                    detail=f"Place {place.place_id} visited more than once.",
                )
            )
        else:
            seen_places[place.place_id] = act.id

        # Category quota tracking
        cat_counts[place.category] = cat_counts.get(place.category, 0) + 1

        # Once-per-trip experience tags
        for tag in place.tags:
            if tag in graph.constraints.once_per_trip_tags:
                if tag in once_tags_seen:
                    checks.append(
                        CheckResult(
                            check_id=f"once_tag:{act.id}:{tag}",
                            node_ids=(once_tags_seen[tag], act.id),
                            truth=TruthValue.FALSE,
                            code="ONCE_PER_TRIP_TAG_DUPLICATED",
                            detail=f"Tag {tag!r} exceeded once-per-trip limit.",
                        )
                    )
                else:
                    once_tags_seen[tag] = act.id

        # Dietary tags on meals
        if act.is_meal and graph.constraints.required_dietary_tags:
            missing = set(graph.constraints.required_dietary_tags) - set(place.dietary_tags)
            if missing:
                checks.append(
                    CheckResult(
                        check_id=f"dietary:{act.id}",
                        node_ids=(act.id,),
                        truth=TruthValue.FALSE,
                        code="DIETARY_REQUIREMENT_UNMET",
                        detail=f"Meal {act.id} missing dietary tags: {sorted(missing)}",
                    )
                )

        # K3 opening-hours availability
        avail = resolve_place_availability(
            place,
            act.start_utc,
            act.end_utc,
            now_utc,
            tz_registry,
            public_holidays=holidays_map.get(act.city_id, frozenset()),
        )
        checks.append(
            CheckResult(
                check_id=f"availability:{act.id}",
                node_ids=(act.id,),
                truth=avail.truth,
                code=avail.epistemic_code,
                detail=f"Availability for {place.name}: {avail.epistemic_code}",
                witness_record_id=avail.witness_record_id,
            )
        )

    # 2. Category max quotas & total budget
    for cat, limit in graph.constraints.max_per_category.items():
        if cat_counts.get(cat, 0) > limit:
            checks.append(
                CheckResult(
                    check_id=f"cat_quota:{cat}",
                    node_ids=(),
                    truth=TruthValue.FALSE,
                    code="CATEGORY_QUOTA_EXCEEDED",
                    detail=f"Category {cat!r} count {cat_counts[cat]} exceeds max {limit}.",
                )
            )

    if graph.constraints.total_budget_usd is not None:
        total_cost = sum(a.cost_usd for a in graph.activities) + sum(
            leg.cost_usd for leg in graph.transit_legs
        )
        if total_cost > graph.constraints.total_budget_usd:
            checks.append(
                CheckResult(
                    check_id="total_budget",
                    node_ids=(),
                    truth=TruthValue.FALSE,
                    code="TOTAL_BUDGET_EXCEEDED",
                    detail=f"Cost {total_cost} exceeds budget {graph.constraints.total_budget_usd}.",
                )
            )

    # 3. Intra-day transit physics & daily active time budget
    by_day: dict[tuple[str, date], list] = {}
    for act in graph.activities:
        by_day.setdefault((act.stay_id, act.local_date), []).append(act)

    for (stay_id, d), day_acts in by_day.items():
        day_acts.sort(key=lambda a: (a.start_utc, a.end_utc, a.id))
        active_min = sum(a.duration_min for a in day_acts)
        if active_min > graph.constraints.daily_active_limit_min:
            checks.append(
                CheckResult(
                    check_id=f"daily_budget:{stay_id}:{d}",
                    node_ids=tuple(a.id for a in day_acts),
                    truth=TruthValue.FALSE,
                    code="DAILY_BUDGET_EXCEEDED",
                    detail=f"Scheduled {active_min}m exceeds daily limit {graph.constraints.daily_active_limit_min}m.",
                )
            )
        for u, v in zip(day_acts, day_acts[1:]):
            t_min = transit_minutes_fn(u.place_id, v.place_id)
            gap_sec = v.start_utc - u.end_utc
            if gap_sec < t_min * 60:
                checks.append(
                    CheckResult(
                        check_id=f"transit:{u.id}->{v.id}",
                        node_ids=(u.id, v.id),
                        truth=TruthValue.FALSE,
                        code="TRANSIT_PHYSICS_VIOLATED",
                        detail=f"Gap {gap_sec // 60}m < required transit {t_min}m.",
                    )
                )

    # 4. Inter-city transit leg -> destination hotel contractual arrival
    for leg in graph.transit_legs:
        dest_stay = stays_by_id[leg.to_stay_id]
        transfer_min = transit_minutes_fn(leg.dest_place_id, dest_stay.hotel.hotel_id)
        hotel_arrival_utc = leg.arrive_utc + transfer_min * 60
        h_res = evaluate_hotel_arrival(dest_stay.hotel, hotel_arrival_utc, tz_registry)
        checks.append(
            CheckResult(
                check_id=f"hotel_arrival:{leg.id}->{dest_stay.id}",
                node_ids=(leg.id, dest_stay.id),
                truth=h_res.truth,
                code=h_res.state,
                detail=h_res.reason,
            )
        )

    status = kleene_and([c.truth for c in checks]) if checks else TruthValue.TRUE
    if status == TruthValue.FALSE:
        cert: VerificationCertificate = "VIOLATED_FALSE"
    elif status == TruthValue.UNKNOWN:
        cert = "CONDITIONAL_ON_ASSUMPTIONS"
    else:
        cert = "VERIFIED_TRUE"

    return VerificationReport(
        hard_constraints_status=status,
        verification_certificate=cert,
        checks=tuple(checks),
        assumptions_accepted=tuple(c for c in checks if c.truth == TruthValue.UNKNOWN),
        violations=tuple(c for c in checks if c.truth == TruthValue.FALSE),
        drifted_timezones=drifted,
    )
