"""Contractual hotel reservation state machine (TripGraph v7).

Separates contractual booking hold terms (no-show cutoffs, late-arrival
notification deadlines, multi-night preservation) from physical access methods
(STAFFED_24H, STAFFED_HOURS, CONTACTLESS_CODE) and early baggage drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from tripgraph.schema.models import (
    HotelBookingContract,
    LocalOffsetTime,
    TruthValue,
)
from tripgraph.time.tz_registry import (
    PinnedTimezone,
    PinnedTzRegistry,
    resolve_local_to_utc,
    utc_to_local,
)

ReservationState = Literal[
    "ACTIVE_CHECKED_IN",
    "PRE_CHECKIN_BAGGAGE_HELD_THEN_CHECKED_IN",
    "NO_SHOW_CANCELLED",
    "MISSING_PRIOR_RESERVATION_NIGHT",
    "MISSING_FOLLOWUP_CHECKIN",
    "CHECKIN_REJECTED_DESK_CLOSED",
    "CONTACTLESS_NOT_PREREGISTERED",
    "CONTACTLESS_CODE_EXPIRED_OR_INACTIVE",
    "ARRIVAL_AFTER_STAY_CHECKOUT",
]


@dataclass(frozen=True)
class HotelCheckOutcome:
    truth: TruthValue
    state: ReservationState
    reason: str


def _resolve_offset_utc(
    anchor_date: date,
    offset_time: LocalOffsetTime,
    pinned_tz: PinnedTimezone,
    *,
    ambiguous_policy: Literal["fold_0", "fold_1"] = "fold_0",
) -> int:
    target_date = anchor_date + timedelta(days=offset_time.day_offset)
    res = resolve_local_to_utc(
        target_date,
        offset_time.local_time,
        pinned_tz,
        ambiguous_policy=ambiguous_policy,
        gap_policy="shift_forward",
    )
    assert res.utc_epoch is not None
    return res.utc_epoch


def evaluate_hotel_arrival(
    contract: HotelBookingContract,
    arrival_utc: int,
    tz_registry: PinnedTzRegistry,
    *,
    followup_checkin_utc: int | None = None,
) -> HotelCheckOutcome:
    """Evaluate whether a physical arrival (and optional post-bag-drop check-in) is valid."""
    pinned_tz = tz_registry.get(contract.tz_iana)
    d0 = contract.check_in_date

    # Apply date-specific property override on d0 if present
    override = contract.date_policy_overrides.get(d0)
    std_checkin = (
        override.standard_checkin
        if override and override.standard_checkin is not None
        else contract.standard_checkin
    )
    no_show_cutoff = (
        override.no_show_cutoff
        if override and override.no_show_cutoff is not None
        else contract.no_show_cutoff
    )
    staffed_cutoff = (
        override.staffed_desk_cutoff
        if override and override.staffed_desk_cutoff is not None
        else contract.staffed_desk_cutoff
    )

    room_ready_utc = _resolve_offset_utc(d0, std_checkin, pinned_tz, ambiguous_policy="fold_0")
    base_noshow_utc = _resolve_offset_utc(d0, no_show_cutoff, pinned_tz, ambiguous_policy="fold_1")

    # Determine effective contractual no-show deadline (late-notify extension if notified on time)
    effective_noshow_utc = base_noshow_utc
    lnp = contract.late_notify_policy
    if (
        lnp.supported
        and lnp.notify_by is not None
        and lnp.extended_hold_until is not None
        and contract.guest_notified_late_at_utc is not None
    ):
        notify_by_utc = _resolve_offset_utc(d0, lnp.notify_by, pinned_tz, ambiguous_policy="fold_1")
        if contract.guest_notified_late_at_utc <= notify_by_utc:
            effective_noshow_utc = _resolve_offset_utc(
                d0, lnp.extended_hold_until, pinned_tz, ambiguous_policy="fold_1"
            )

    checkout_res = resolve_local_to_utc(
        contract.check_out_date,
        contract.standard_checkout,
        pinned_tz,
        ambiguous_policy="fold_0",
        gap_policy="shift_forward",
    )
    assert checkout_res.utc_epoch is not None
    stay_checkout_utc = checkout_res.utc_epoch

    # Step 1: Arrival before room_ready_utc -> check baggage drop vs missing prior night
    used_bag_drop = False
    if arrival_utc < room_ready_utc:
        arr_local_date = utc_to_local(arrival_utc, pinned_tz).date()
        bdp = contract.baggage_drop_policy
        if (
            arr_local_date == d0
            and bdp.checkin_day_open is not None
            and bdp.checkin_day_close is not None
        ):
            bag_open = resolve_local_to_utc(
                d0, bdp.checkin_day_open, pinned_tz, ambiguous_policy="fold_0", gap_policy="shift_forward"
            ).utc_epoch
            bag_close = resolve_local_to_utc(
                d0, bdp.checkin_day_close, pinned_tz, ambiguous_policy="fold_1", gap_policy="shift_forward"
            ).utc_epoch
            assert bag_open is not None and bag_close is not None
            if bag_open <= arrival_utc <= bag_close:
                used_bag_drop = True
                if followup_checkin_utc is None or followup_checkin_utc < room_ready_utc:
                    return HotelCheckOutcome(
                        truth=TruthValue.FALSE,
                        state="MISSING_FOLLOWUP_CHECKIN",
                        reason="Baggage drop succeeded, but no valid check-in after room_ready_utc was scheduled.",
                    )
                t_check = followup_checkin_utc
            else:
                return HotelCheckOutcome(
                    truth=TruthValue.FALSE,
                    state="MISSING_PRIOR_RESERVATION_NIGHT",
                    reason="Arrival occurs before check-in day baggage-drop window opens; prior night required.",
                )
        else:
            return HotelCheckOutcome(
                truth=TruthValue.FALSE,
                state="MISSING_PRIOR_RESERVATION_NIGHT",
                reason="Arrival precedes room_ready_utc with no eligible check-in day baggage drop.",
            )
    else:
        t_check = followup_checkin_utc if (followup_checkin_utc is not None and followup_checkin_utc >= arrival_utc) else arrival_utc

    # Step 2: Stay checkout boundary & contractual no-show cancellation
    if t_check >= stay_checkout_utc:
        return HotelCheckOutcome(
            truth=TruthValue.FALSE,
            state="ARRIVAL_AFTER_STAY_CHECKOUT",
            reason="Check-in time occurs at or after final stay checkout.",
        )

    if t_check > effective_noshow_utc and not contract.multi_night_preserve_on_noshow:
        return HotelCheckOutcome(
            truth=TruthValue.FALSE,
            state="NO_SHOW_CANCELLED",
            reason="Check-in exceeds effective contractual no-show deadline.",
        )

    # Step 3: Physical access verification
    if contract.access_method == "STAFFED_HOURS":
        assert staffed_cutoff is not None
        desk_cutoff_utc = _resolve_offset_utc(d0, staffed_cutoff, pinned_tz, ambiguous_policy="fold_1")
        if t_check > desk_cutoff_utc:
            return HotelCheckOutcome(
                truth=TruthValue.FALSE,
                state="CHECKIN_REJECTED_DESK_CLOSED",
                reason="Physical check-in exceeds staffed front-desk closing cutoff.",
            )

    elif contract.access_method == "CONTACTLESS_CODE":
        cp = contract.contactless_policy
        assert cp is not None
        if not cp.pre_registered:
            return HotelCheckOutcome(
                truth=TruthValue.FALSE,
                state="CONTACTLESS_NOT_PREREGISTERED",
                reason="Contactless property requires guest pre-registration before arrival.",
            )
        code_from_utc = _resolve_offset_utc(d0, cp.code_valid_from, pinned_tz, ambiguous_policy="fold_0")
        code_exp_utc = _resolve_offset_utc(d0, cp.code_activation_expiry, pinned_tz, ambiguous_policy="fold_1")
        if not (code_from_utc <= t_check <= code_exp_utc):
            return HotelCheckOutcome(
                truth=TruthValue.FALSE,
                state="CONTACTLESS_CODE_EXPIRED_OR_INACTIVE",
                reason="Check-in falls outside contactless access-code validity window.",
            )

    final_state: ReservationState = (
        "PRE_CHECKIN_BAGGAGE_HELD_THEN_CHECKED_IN" if used_bag_drop else "ACTIVE_CHECKED_IN"
    )
    return HotelCheckOutcome(
        truth=TruthValue.TRUE,
        state=final_state,
        reason="Contractual hold and physical access satisfied.",
    )
