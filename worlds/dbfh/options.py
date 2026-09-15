from __future__ import annotations

import abc
import dataclasses

from Options import PerGameCommonOptions, Choice, FreeText, Range

MAX_HOURS = 120
MAX_SLOTS = 5


class DBFHGoal(Choice):
    """When the client automatically calls !goal (and !release)

    - `final_hour_played`: When the bus has been fed and running for `hour_played_max` hours.
    - `final_hour_funded`: When the bus has been fed and funded through `hour_funded_max` hours.
    """
    display_name = "Goal"
    default = 0

    option_final_hour_played = 0
    option_final_hour_funded = 1


class DBPubSub(FreeText):
    """
    The pub-sub URL to get the current donation amount from.
    """
    display_name = "DBFH Pub-Sub URL"


class DBStartDateTime(FreeText):
    """
    The starting date and time of bus in ISO9601 format.
    """
    display_name = "DBFH Start Time"


class DBHours(Range, abc.ABC):
    range_start = 1
    range_end = MAX_HOURS
    default = 96


class DBPlayedHours(DBHours):
    """Number of hours of DBFH played that have associated unlocks/locations."""
    display_name = "DBFH Hours Played locations"


class DBFundedHours(DBHours):
    """Number of hours of DBFH funded that have associated unlocks/locations."""
    display_name = "DBFH Hours Played funded"


class ItemRange(Range):
    range_start = 0
    range_end = MAX_SLOTS


class DBFundedItem(ItemRange):
    """Number of checks completed when a new hour is funded and the driver is fed."""
    display_name = "Items per hour funded"
    default = 1


class DBFundedPriorityItem(ItemRange):
    """Of those checks, how many should be forced to be progression items."""
    display_name = "Progression items per hour funded"
    default = 0


class DBPlayedItem(ItemRange):
    """Number of checks completed when an hour is completed by DBFH and the driver is fed."""
    display_name = "Items per hour played"
    default = 2


class DBPlayedPriorityItem(ItemRange):
    """Of those checks, how many should be forced to be progression items."""
    display_name = "Progression items per hour played"
    default = 1


class DriverGoodItemsFactor(Range):
    """How many progressive fuel and refreshment items to add to the pool,
    as a percentage of the total number required to goal."""

    display_name = "Driver Goods over-supply factor"
    range_start = 100
    range_end = 400
    default = 150


@dataclasses.dataclass
class DBFHOptions(PerGameCommonOptions):
    goal: DBFHGoal

    source_pubsub_url: DBPubSub
    source_event_start: DBStartDateTime

    hour_funded_max: DBFundedHours
    hour_played_max: DBPlayedHours

    hour_funded_items: DBFundedItem
    hour_funded_force_progression_items: DBFundedPriorityItem
    hour_played_items: DBPlayedItem
    hour_played_force_progression_items: DBPlayedPriorityItem

    driver_goods_factor: DriverGoodItemsFactor
