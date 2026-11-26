"""Typed user edit commands supported by TripGraph v7."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from tripgraph.schema.models import CityStay, HotelBookingContract


class ExtendStayEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["extend_stay"] = "extend_stay"
    stay_id: str
    delta_nights: int = Field(description="Positive to extend, negative to shorten.")


class ShiftStartDateEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["shift_start_date"] = "shift_start_date"
    delta_days: int
    allow_shift_locked: bool = Field(
        default=False,
        description="Default False refuses shifting when any stay, leg, or activity is locked_by_user.",
    )


class SwapHotelEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["swap_hotel"] = "swap_hotel"
    stay_id: str
    new_hotel: HotelBookingContract


class MoveActivityEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["move_activity"] = "move_activity"
    activity_id: str
    target_stay_id: str
    target_local_date: date
    target_start_utc: int | None = None


class DropActivityEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["drop_activity"] = "drop_activity"
    activity_id: str


class DropCityEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["drop_city"] = "drop_city"
    stay_id: str


class AddCityEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["add_city"] = "add_city"
    new_stay: CityStay
    insert_after_stay_id: str | None = None


class LateNightArrivalEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["late_night_arrival"] = "late_night_arrival"
    leg_id: str
    new_depart_utc: int = Field(ge=0)
    new_arrive_utc: int = Field(ge=0)
    new_carrier_slot_id: str


class UpdateDietaryEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit_type: Literal["update_dietary"] = "update_dietary"
    required_dietary_tags: tuple[str, ...]


EditCommand = Annotated[
    Union[
        ExtendStayEdit,
        ShiftStartDateEdit,
        SwapHotelEdit,
        MoveActivityEdit,
        DropActivityEdit,
        DropCityEdit,
        AddCityEdit,
        LateNightArrivalEdit,
        UpdateDietaryEdit,
    ],
    Field(discriminator="edit_type"),
]
