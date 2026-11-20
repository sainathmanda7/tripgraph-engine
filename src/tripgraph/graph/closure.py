"""Two-horizon factor-graph dirty closure & global escalation routing (TripGraph v7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from tripgraph.schema.edits import (
    AddCityEdit,
    DropActivityEdit,
    DropCityEdit,
    EditCommand,
    ExtendStayEdit,
    LateNightArrivalEdit,
    MoveActivityEdit,
    ShiftStartDateEdit,
    SwapHotelEdit,
    UpdateDietaryEdit,
)
from tripgraph.schema.models import Place, TripGraph

ScopeTier = Literal["DAY_BIN", "STAY_WIDENED", "ALL_LOCKED_VERIFY_ONLY", "GLOBAL_REPLAN"]


@dataclass(frozen=True)
class DirtyClosureResult:
    scope_tier: ScopeTier
    dirty_stay_ids: frozenset[str]
    dirty_leg_ids: frozenset[str]
    dirty_activity_ids: frozenset[str]
    dirty_days: frozenset[tuple[str, date]]
    pinned_day_activity_ids: frozenset[str]  # locked_by_user nodes sitting inside dirty_days
    unlocked_total: int
    dirty_unlocked_count: int
    dirty_ratio: float | None                # None when unlocked_total == 0 (no div-by-zero)
    escalation_reason: str | None = None


def compute_dirty_closure(
    graph: TripGraph,
    edit: EditCommand,
    place_registry: dict[str, Place],
    *,
    max_dirty_ratio: float = 0.50,
    has_non_projectable_global: bool = False,
    has_intercity_markov_objective: bool = False,
) -> DirtyClosureResult:
    """Compute Horizon-0 dirty closure with exact v7 boundary guards."""
    stays_by_id = {s.id: s for s in graph.stays}
    stays_ordered = list(graph.stays)
    stay_index = {s.id: i for i, s in enumerate(stays_ordered)}
    legs_by_from = {l.from_stay_id: l for l in graph.transit_legs}
    legs_by_to = {l.to_stay_id: l for l in graph.transit_legs}
    acts_by_id = {a.id: a for a in graph.activities}

    unlocked_stays = {s.id for s in graph.stays if not s.locked_by_user}
    unlocked_legs = {l.id for l in graph.transit_legs if not l.locked_by_user}
    unlocked_acts = {a.id for a in graph.activities if not a.locked_by_user}
    unlocked_total = len(unlocked_stays) + len(unlocked_legs) + len(unlocked_acts)

    # Guard 1: 100% locked trip -> never divide by zero, never escalate to global replan
    if unlocked_total == 0:
        return DirtyClosureResult(
            scope_tier="ALL_LOCKED_VERIFY_ONLY",
            dirty_stay_ids=frozenset(),
            dirty_leg_ids=frozenset(),
            dirty_activity_ids=frozenset(),
            dirty_days=frozenset(),
            pinned_day_activity_ids=frozenset(a.id for a in graph.activities),
            unlocked_total=0,
            dirty_unlocked_count=0,
            dirty_ratio=None,
            escalation_reason=None,
        )

    # Guard 2: Non-projectable active global constraints or inter-city topology mutations
    if has_non_projectable_global:
        return _global_escalation(
            graph, unlocked_total, "NON_PROJECTABLE_GLOBAL_CONSTRAINT"
        )
    if has_intercity_markov_objective and isinstance(edit, (DropCityEdit, AddCityEdit)):
        return _global_escalation(
            graph, unlocked_total, "NON_PROJECTABLE_INTERCITY_TOPOLOGY_MUTATION"
        )

    dirty_stays: set[str] = set()
    dirty_legs: set[str] = set()
    dirty_acts: set[str] = set()
    dirty_days: set[tuple[str, date]] = set()

    # Step 1: Seed directly mutated nodes & interface-gated cascade
    if isinstance(edit, ExtendStayEdit):
        stay = stays_by_id[edit.stay_id]
        if not stay.locked_by_user:
            dirty_stays.add(stay.id)
        new_checkout = stay.check_out_date + timedelta(days=edit.delta_nights)
        # Mark the added/removed edge day(s) in this stay
        if edit.delta_nights > 0:
            for d_off in range(edit.delta_nights):
                dirty_days.add((stay.id, stay.check_out_date + timedelta(days=d_off)))
        else:
            for d_off in range(abs(edit.delta_nights)):
                dirty_days.add((stay.id, new_checkout + timedelta(days=d_off)))

        # Interface-gated downstream cascade across subsequent stays
        idx = stay_index[stay.id]
        prev_new_checkout = new_checkout
        for k in range(idx, len(stays_ordered) - 1):
            cur_stay = stays_ordered[k]
            nxt_stay = stays_ordered[k + 1]
            out_leg = legs_by_from.get(cur_stay.id)
            # Cascade only if boundary interface is invalidated
            if prev_new_checkout > nxt_stay.check_in_date or edit.delta_nights != 0:
                if out_leg is not None and not out_leg.locked_by_user:
                    dirty_legs.add(out_leg.id)
                if not nxt_stay.locked_by_user:
                    dirty_stays.add(nxt_stay.id)
                # Arrival day of next stay enters dirty_days (and shifted days)
                for n_day in nxt_stay.hotel.reserved_nights:
                    dirty_days.add((nxt_stay.id, n_day))
                nxt_len = (nxt_stay.check_out_date - nxt_stay.check_in_date).days
                prev_new_checkout = prev_new_checkout + timedelta(days=nxt_len)
            else:
                break

    elif isinstance(edit, ShiftStartDateEdit):
        # Shifting entire trip start date touches all stays/legs/days -> likely global
        for s in stays_ordered:
            if not s.locked_by_user:
                dirty_stays.add(s.id)
            for n_day in s.hotel.reserved_nights:
                dirty_days.add((s.id, n_day))
        for l in graph.transit_legs:
            if not l.locked_by_user:
                dirty_legs.add(l.id)

    elif isinstance(edit, SwapHotelEdit):
        stay = stays_by_id[edit.stay_id]
        if not stay.locked_by_user:
            dirty_stays.add(stay.id)
        in_leg = legs_by_to.get(stay.id)
        if in_leg is not None and not in_leg.locked_by_user:
            dirty_legs.add(in_leg.id)
        # First day (check-in / baggage drop) and last day (checkout) touch hotel transit
        dirty_days.add((stay.id, stay.check_in_date))
        dirty_days.add((stay.id, stay.check_out_date - timedelta(days=1)))

    elif isinstance(edit, MoveActivityEdit):
        act = acts_by_id[edit.activity_id]
        if not act.locked_by_user:
            dirty_acts.add(act.id)
        # BOTH source day and target day enter dirty_days (v6 dynamic adjacency rule)
        dirty_days.add((act.stay_id, act.local_date))
        dirty_days.add((edit.target_stay_id, edit.target_local_date))

    elif isinstance(edit, DropActivityEdit):
        act = acts_by_id[edit.activity_id]
        if not act.locked_by_user:
            dirty_acts.add(act.id)
        dirty_days.add((act.stay_id, act.local_date))

    elif isinstance(edit, LateNightArrivalEdit):
        leg = next(l for l in graph.transit_legs if l.id == edit.leg_id)
        if not leg.locked_by_user:
            dirty_legs.add(leg.id)
        dest_stay = stays_by_id[leg.to_stay_id]
        if not dest_stay.locked_by_user:
            dirty_stays.add(dest_stay.id)
        dirty_days.add((dest_stay.id, dest_stay.check_in_date))

    elif isinstance(edit, UpdateDietaryEdit):
        req = set(edit.required_dietary_tags)
        for act in graph.activities:
            if not act.is_meal:
                continue
            place = place_registry.get(act.place_id)
            if place is None or not req.issubset(set(place.dietary_tags)):
                if not act.locked_by_user:
                    dirty_acts.add(act.id)
                dirty_days.add((act.stay_id, act.local_date))

    elif isinstance(edit, DropCityEdit):
        stay = stays_by_id[edit.stay_id]
        if not stay.locked_by_user:
            dirty_stays.add(stay.id)
        for n_day in stay.hotel.reserved_nights:
            dirty_days.add((stay.id, n_day))
        for leg in graph.transit_legs:
            if leg.from_stay_id == stay.id or leg.to_stay_id == stay.id:
                if not leg.locked_by_user:
                    dirty_legs.add(leg.id)

    elif isinstance(edit, AddCityEdit):
        # Adding a city affects surrounding transit legs & adjacent stay boundaries
        if edit.insert_after_stay_id is not None:
            out_leg = legs_by_from.get(edit.insert_after_stay_id)
            if out_leg is not None and not out_leg.locked_by_user:
                dirty_legs.add(out_leg.id)
            dirty_stays.add(edit.insert_after_stay_id)

    # Step 2: Fixed-point Day-Bin Saturation
    # Pull ALL unlocked activities on every day in dirty_days (even if currently satisfied!)
    changed = True
    while changed:
        changed = False
        for act in graph.activities:
            day_key = (act.stay_id, act.local_date)
            if act.id in dirty_acts and day_key not in dirty_days:
                dirty_days.add(day_key)
                changed = True
            if day_key in dirty_days and not act.locked_by_user and act.id not in dirty_acts:
                dirty_acts.add(act.id)
                changed = True

    pinned_in_dirty_days = frozenset(
        a.id
        for a in graph.activities
        if a.locked_by_user and (a.stay_id, a.local_date) in dirty_days
    )

    dirty_unlocked_count = len(dirty_stays) + len(dirty_legs) + len(dirty_acts)
    dirty_ratio = dirty_unlocked_count / unlocked_total

    if dirty_ratio > max_dirty_ratio:
        return DirtyClosureResult(
            scope_tier="GLOBAL_REPLAN",
            dirty_stay_ids=frozenset(dirty_stays),
            dirty_leg_ids=frozenset(dirty_legs),
            dirty_activity_ids=frozenset(dirty_acts),
            dirty_days=frozenset(dirty_days),
            pinned_day_activity_ids=pinned_in_dirty_days,
            unlocked_total=unlocked_total,
            dirty_unlocked_count=dirty_unlocked_count,
            dirty_ratio=dirty_ratio,
            escalation_reason=f"DIRTY_RATIO_EXCEEDED:{dirty_ratio:.2f}>{max_dirty_ratio:.2f}",
        )

    return DirtyClosureResult(
        scope_tier="DAY_BIN",
        dirty_stay_ids=frozenset(dirty_stays),
        dirty_leg_ids=frozenset(dirty_legs),
        dirty_activity_ids=frozenset(dirty_acts),
        dirty_days=frozenset(dirty_days),
        pinned_day_activity_ids=pinned_in_dirty_days,
        unlocked_total=unlocked_total,
        dirty_unlocked_count=dirty_unlocked_count,
        dirty_ratio=dirty_ratio,
        escalation_reason=None,
    )


def widen_to_horizon1(
    graph: TripGraph,
    horizon0: DirtyClosureResult,
    boundary_stay_ids: frozenset[str],
    *,
    max_dirty_ratio: float = 0.50,
    max_widened_cities: int = 2,
) -> DirtyClosureResult:
    """Horizon-1 widening: pull all unlocked days of touched boundary stays."""
    if len(boundary_stay_ids) > max_widened_cities:
        return _global_escalation(
            graph,
            horizon0.unlocked_total,
            f"HORIZON1_EXCEEDED_MAX_CITIES:{len(boundary_stay_ids)}>{max_widened_cities}",
        )

    stays_by_id = {s.id: s for s in graph.stays}
    dirty_stays = set(horizon0.dirty_stay_ids)
    dirty_days = set(horizon0.dirty_days)
    dirty_acts = set(horizon0.dirty_activity_ids)

    for sid in boundary_stay_ids:
        stay = stays_by_id[sid]
        if not stay.locked_by_user:
            dirty_stays.add(sid)
        for n_day in stay.hotel.reserved_nights:
            dirty_days.add((sid, n_day))

    for act in graph.activities:
        if (act.stay_id, act.local_date) in dirty_days and not act.locked_by_user:
            dirty_acts.add(act.id)

    pinned_in_dirty_days = frozenset(
        a.id
        for a in graph.activities
        if a.locked_by_user and (a.stay_id, a.local_date) in dirty_days
    )
    dirty_unlocked_count = len(dirty_stays) + len(horizon0.dirty_leg_ids) + len(dirty_acts)
    dirty_ratio = dirty_unlocked_count / horizon0.unlocked_total

    if dirty_ratio > max_dirty_ratio:
        return DirtyClosureResult(
            scope_tier="GLOBAL_REPLAN",
            dirty_stay_ids=frozenset(dirty_stays),
            dirty_leg_ids=horizon0.dirty_leg_ids,
            dirty_activity_ids=frozenset(dirty_acts),
            dirty_days=frozenset(dirty_days),
            pinned_day_activity_ids=pinned_in_dirty_days,
            unlocked_total=horizon0.unlocked_total,
            dirty_unlocked_count=dirty_unlocked_count,
            dirty_ratio=dirty_ratio,
            escalation_reason=f"HORIZON1_DIRTY_RATIO_EXCEEDED:{dirty_ratio:.2f}>{max_dirty_ratio:.2f}",
        )

    return DirtyClosureResult(
        scope_tier="STAY_WIDENED",
        dirty_stay_ids=frozenset(dirty_stays),
        dirty_leg_ids=horizon0.dirty_leg_ids,
        dirty_activity_ids=frozenset(dirty_acts),
        dirty_days=frozenset(dirty_days),
        pinned_day_activity_ids=pinned_in_dirty_days,
        unlocked_total=horizon0.unlocked_total,
        dirty_unlocked_count=dirty_unlocked_count,
        dirty_ratio=dirty_ratio,
        escalation_reason=None,
    )


def _global_escalation(
    graph: TripGraph, unlocked_total: int, reason: str
) -> DirtyClosureResult:
    return DirtyClosureResult(
        scope_tier="GLOBAL_REPLAN",
        dirty_stay_ids=frozenset(s.id for s in graph.stays if not s.locked_by_user),
        dirty_leg_ids=frozenset(l.id for l in graph.transit_legs if not l.locked_by_user),
        dirty_activity_ids=frozenset(a.id for a in graph.activities if not a.locked_by_user),
        dirty_days=frozenset((a.stay_id, a.local_date) for a in graph.activities),
        pinned_day_activity_ids=frozenset(a.id for a in graph.activities if a.locked_by_user),
        unlocked_total=unlocked_total,
        dirty_unlocked_count=unlocked_total,
        dirty_ratio=1.0,
        escalation_reason=reason,
    )
