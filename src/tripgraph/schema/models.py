"""Core TripGraph v7 Pydantic v2 schemas and Stage-0 input axiom validators."""

from __future__ import annotations

from datetime import date, time, timedelta
from enum import Enum, IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Kleene K3 Verification & Evidence Enums
# ---------------------------------------------------------------------------


class TruthValue(str, Enum):
    """Three-valued Kleene logic for epistemic verification."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class VerificationPolicy(str, Enum):
    """Caller-selected policy governing which UNKNOWN states CP-SAT may accept."""

    STRICT = "STRICT"
    ASSUME_WEEKLY_NON_HOLIDAY = "ASSUME_WEEKLY_NON_HOLIDAY"
    ALLOW_UNVERIFIED_HOLIDAYS = "ALLOW_UNVERIFIED_HOLIDAYS"


class AuthorityTier(IntEnum):
    """Source authority precedence (lower integer = higher authority)."""

    TIER_1_OFFICIAL = 1
    TIER_2_COMMERCIAL = 2
    TIER_3_COMMUNITY = 3


class EvidenceSpecificity(IntEnum):
    """Temporal specificity of an availability record (higher = more specific)."""

    WEEKLY_RECURRING = 1
    DATE_SPECIFIC = 2


class EvidenceVerdict(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNPARSEABLE = "UNPARSEABLE"


class HolidayPolicy(str, Enum):
    OPEN_NORMAL = "OPEN_NORMAL"
    HOLIDAY_HOURS = "HOLIDAY_HOURS"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class NodeTier(str, Enum):
    """Droppability tier: only OPTIONAL may be dropped, and only with explicit consent."""

    CRITICAL = "CRITICAL"
    ESSENTIAL = "ESSENTIAL"
    OPTIONAL = "OPTIONAL"


class TransitMode(str, Enum):
    WALK = "WALK"
    DRIVE = "DRIVE"
    METRO = "METRO"
    RAIL = "RAIL"
    FLIGHT = "FLIGHT"


# ---------------------------------------------------------------------------
# Temporal & Opening-Hours Contracts
# ---------------------------------------------------------------------------


class LocalOffsetTime(BaseModel):
    """Wall-clock local time paired with an explicit calendar-day offset from an anchor date."""

    model_config = ConfigDict(frozen=True)

    day_offset: int = Field(ge=-1, le=3)
    local_time: time


class OpeningRule(BaseModel):
    """Wall-clock weekly opening rule (handles overnight spans & DST safely)."""

    model_config = ConfigDict(frozen=True)

    weekday: int = Field(ge=0, le=6, description="0=Monday .. 6=Sunday")
    open_local: time
    close_local: time
    close_day_offset: Literal[0, 1] = 0
    dst_ambiguous_open: Literal["fold_0", "fold_1"] = "fold_0"
    dst_ambiguous_close: Literal["fold_0", "fold_1"] = "fold_1"
    dst_gap_policy: Literal["shift_forward", "closed"] = "shift_forward"

    @model_validator(mode="after")
    def validate_same_day_order(self) -> OpeningRule:
        if self.close_day_offset == 0 and self.close_local <= self.open_local:
            raise ValueError(
                "close_local must be strictly after open_local when close_day_offset == 0; "
                "use close_day_offset=1 for overnight intervals."
            )
        return self


class EvidenceRecord(BaseModel):
    """Single provenance-tracked availability claim used by the Pareto witness resolver."""

    model_config = ConfigDict(frozen=True)

    record_id: str
    place_id: str
    authority_tier: AuthorityTier
    specificity: EvidenceSpecificity
    verdict: EvidenceVerdict
    source: str
    fetched_at_utc: int = Field(ge=0)
    ttl_sec: int = Field(gt=0)
    max_stale_age_sec: int = Field(default=86400, ge=0)
    applicable_date: date | None = None
    rules: tuple[OpeningRule, ...] = ()

    @model_validator(mode="after")
    def validate_date_specificity(self) -> EvidenceRecord:
        if self.specificity == EvidenceSpecificity.DATE_SPECIFIC and self.applicable_date is None:
            raise ValueError("DATE_SPECIFIC evidence must specify applicable_date.")
        return self


class Place(BaseModel):
    """Canonical grounded venue in PlaceRegistry."""

    model_config = ConfigDict(frozen=True)

    place_id: str
    name: str
    city_id: str
    tz_iana: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    category: str
    tags: tuple[str, ...] = ()
    dietary_tags: tuple[str, ...] = ()
    min_dwell_min: int = Field(default=30, ge=1)
    holiday_policy: HolidayPolicy = HolidayPolicy.UNKNOWN
    estimated_cost_usd: int = Field(default=0, ge=0)
    rating: float = Field(default=4.2, ge=0.0, le=5.0)
    evidence: tuple[EvidenceRecord, ...] = ()


# ---------------------------------------------------------------------------
# Contractual Hotel Reservation Model
# ---------------------------------------------------------------------------


class LateNotifyPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    supported: bool = False
    notify_by: LocalOffsetTime | None = None
    extended_hold_until: LocalOffsetTime | None = None

    @model_validator(mode="after")
    def validate_supported_fields(self) -> LateNotifyPolicy:
        if self.supported and (self.notify_by is None or self.extended_hold_until is None):
            raise ValueError(
                "LateNotifyPolicy with supported=True must specify notify_by and extended_hold_until."
            )
        return self


class ContactlessPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    pre_reg_deadline: LocalOffsetTime
    pre_registered: bool
    code_valid_from: LocalOffsetTime
    code_activation_expiry: LocalOffsetTime


class BaggageDropPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    checkin_day_open: time | None = None
    checkin_day_close: time | None = None
    checkout_day_until: time | None = None

    @model_validator(mode="after")
    def validate_window_pair(self) -> BaggageDropPolicy:
        if (self.checkin_day_open is None) != (self.checkin_day_close is None):
            raise ValueError("Both checkin_day_open and checkin_day_close must be set or both None.")
        if (
            self.checkin_day_open is not None
            and self.checkin_day_close is not None
            and self.checkin_day_close <= self.checkin_day_open
        ):
            raise ValueError("checkin_day_close must be after checkin_day_open.")
        return self


class HotelPolicyOverride(BaseModel):
    """Date-specific property override (e.g., holiday desk hours or special cutoff)."""

    model_config = ConfigDict(frozen=True)

    standard_checkin: LocalOffsetTime | None = None
    no_show_cutoff: LocalOffsetTime | None = None
    staffed_desk_cutoff: LocalOffsetTime | None = None


class HotelBookingContract(BaseModel):
    """Full contractual hotel booking and physical access policy (TripGraph v7)."""

    model_config = ConfigDict(frozen=True)

    hotel_id: str
    hotel_name: str
    tz_iana: str
    check_in_date: date
    check_out_date: date
    guarantee_type: Literal["PREPAID", "CARD_GUARANTEED", "UNSECURED_HOLD"]
    access_method: Literal["STAFFED_HOURS", "STAFFED_24H", "CONTACTLESS_CODE"]
    standard_checkin: LocalOffsetTime = LocalOffsetTime(day_offset=0, local_time=time(15, 0))
    standard_checkout: time = time(11, 0)
    no_show_cutoff: LocalOffsetTime = LocalOffsetTime(day_offset=1, local_time=time(6, 0))
    multi_night_preserve_on_noshow: bool = False
    late_notify_policy: LateNotifyPolicy = Field(default_factory=LateNotifyPolicy)
    guest_notified_late_at_utc: int | None = None
    staffed_desk_cutoff: LocalOffsetTime | None = None
    contactless_policy: ContactlessPolicy | None = None
    baggage_drop_policy: BaggageDropPolicy = Field(default_factory=BaggageDropPolicy)
    date_policy_overrides: dict[date, HotelPolicyOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract_axioms(self) -> HotelBookingContract:
        if self.check_out_date <= self.check_in_date:
            raise ValueError("Hotel check_out_date must be strictly after check_in_date.")
        if self.access_method == "STAFFED_HOURS" and self.staffed_desk_cutoff is None:
            raise ValueError("STAFFED_HOURS access_method requires staffed_desk_cutoff.")
        if self.access_method == "CONTACTLESS_CODE" and self.contactless_policy is None:
            raise ValueError("CONTACTLESS_CODE access_method requires contactless_policy.")
        return self

    @property
    def reserved_nights(self) -> tuple[date, ...]:
        """Exact half-open set of local calendar nights covered: [check_in_date, check_out_date)."""
        num_nights = (self.check_out_date - self.check_in_date).days
        return tuple(self.check_in_date + timedelta(days=i) for i in range(num_nights))


# ---------------------------------------------------------------------------
# TripGraph Structural Spine Nodes
# ---------------------------------------------------------------------------


class CityStay(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    city_id: str
    tz_iana: str
    hotel: HotelBookingContract
    arrival_binding: Literal["DIRECT_ROOM_CHECKIN", "PRE_CHECKIN_BAG_DROP"] = "DIRECT_ROOM_CHECKIN"
    node_tier: NodeTier = NodeTier.CRITICAL
    locked_by_user: bool = False

    @property
    def check_in_date(self) -> date:
        return self.hotel.check_in_date

    @property
    def check_out_date(self) -> date:
        return self.hotel.check_out_date


class TransitLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    from_stay_id: str
    to_stay_id: str
    origin_place_id: str
    dest_place_id: str
    mode: TransitMode
    depart_utc: int = Field(ge=0)
    arrive_utc: int = Field(ge=0)
    carrier_slot_id: str
    boarding_buffer_min: int = Field(default=20, ge=0)
    cost_usd: int = Field(default=0, ge=0)
    node_tier: NodeTier = NodeTier.CRITICAL
    locked_by_user: bool = False

    @model_validator(mode="after")
    def validate_transit_time_axiom(self) -> TransitLeg:
        if self.arrive_utc <= self.depart_utc:
            raise ValueError("TransitLeg arrive_utc must be strictly greater than depart_utc.")
        return self


class ActivityNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    stay_id: str
    city_id: str
    place_id: str
    local_date: date
    start_utc: int = Field(ge=0)
    end_utc: int = Field(ge=0)
    duration_min: int = Field(ge=0)
    is_milestone: bool = False
    is_meal: bool = False
    node_tier: NodeTier = NodeTier.OPTIONAL
    locked_by_user: bool = False
    cost_usd: int = Field(default=0, ge=0)
    category: str = "general"
    tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_stage0_time_axioms(self) -> ActivityNode:
        if not self.is_milestone and self.duration_min < 1:
            raise ValueError("Non-milestone ActivityNode must have duration_min >= 1.")
        expected_end = self.start_utc + self.duration_min * 60
        if self.end_utc != expected_end:
            raise ValueError(
                f"Stage-0 Axiom Contradiction: end_utc ({self.end_utc}) != "
                f"start_utc ({self.start_utc}) + duration_min*60 ({self.duration_min * 60})."
            )
        return self


class TzPinRecord(BaseModel):
    """Cryptographic pin linking each referenced IANA TZ to exact loaded TZif bytes."""

    model_config = ConfigDict(frozen=True)

    tz_iana: str
    source: str
    version_label: str
    tzif_sha256: str


class TripConstraints(BaseModel):
    """Trip-wide hard constraints & daily limits."""

    model_config = ConfigDict(frozen=True)

    total_budget_usd: int | None = Field(default=None, ge=0)
    daily_active_limit_min: int = Field(default=600, gt=0)
    required_dietary_tags: tuple[str, ...] = ()
    max_per_category: dict[str, int] = Field(default_factory=dict)
    once_per_trip_tags: tuple[str, ...] = ()


class TripGraph(BaseModel):
    """Canonical versioned itinerary state."""

    model_config = ConfigDict(frozen=True)

    trip_id: str
    version: int = Field(default=1, ge=1)
    stays: tuple[CityStay, ...]
    transit_legs: tuple[TransitLeg, ...] = ()
    activities: tuple[ActivityNode, ...] = ()
    constraints: TripConstraints = Field(default_factory=TripConstraints)
    tz_pins: dict[str, TzPinRecord] = Field(default_factory=dict)
    user_intent_notes: str = ""

    @model_validator(mode="after")
    def validate_graph_integrity(self) -> TripGraph:
        stay_ids = {s.id for s in self.stays}
        if len(stay_ids) != len(self.stays):
            raise ValueError("Duplicate CityStay IDs detected.")
        act_ids = {a.id for a in self.activities}
        if len(act_ids) != len(self.activities):
            raise ValueError("Duplicate ActivityNode IDs detected.")
        for act in self.activities:
            if act.stay_id not in stay_ids:
                raise ValueError(f"Activity {act.id} references unknown stay_id {act.stay_id}.")
        for leg in self.transit_legs:
            if leg.from_stay_id not in stay_ids or leg.to_stay_id not in stay_ids:
                raise ValueError(f"TransitLeg {leg.id} references unknown stay_id.")
        return self
