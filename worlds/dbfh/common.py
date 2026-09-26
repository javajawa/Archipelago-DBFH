from __future__ import annotations

import enum
from typing import TypedDict

from .options import MAX_HOURS, MAX_SLOTS

GAME = "Desert Bus For Hope"


class Checks(enum.StrEnum):
    HourFunded = "Funded"
    HourPlayed = "Played"


def location_name(check_type: Checks, hour_zero_offset: int, slot_zero_offset: int = 0) -> str:
    return f"Hour {hour_zero_offset + 1} {check_type.value} (Slot {slot_zero_offset + 1})"


def event_name(check_type: Checks, hour_zero_offset: int) -> str:
    return f"Event: Hour {hour_zero_offset + 1} {check_type.value}"


def location_id(check_type: Checks, hour_zero_offset: int, slot_zero_offset: int) -> int:
    base_id = hour_zero_offset * MAX_SLOTS + slot_zero_offset + 1

    match check_type:
        case check_type.HourFunded:
            return base_id
        case check_type.HourPlayed:
            return base_id + (MAX_SLOTS * MAX_HOURS)


class DBFHSlotData(TypedDict):
    api_url: str
    vst_url: str
    death_link: bool

    funded_hours: int
    funded_items: int
    played_hours: int
    played_items: int

    goal_type: int
    goal_hours: int


__all__ = "GAME", "Checks", "DBFHSlotData", "event_name", "location_id", "location_name"
