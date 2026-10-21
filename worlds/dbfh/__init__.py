from __future__ import annotations

import logging
from typing import ClassVar

from BaseClasses import (
    CollectionRule,
    CollectionState,
    Item,
    ItemClassification,
    Location,
    Region,
)
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import Component, Type, components, launch_subprocess

from .common import GAME, Checks, DBFHSlotData, event_name, location_id, location_name
from .options import MAX_HOURS, MAX_SLOTS, DBFHOptions
from .prizes import DBFHItems


def run_client(*args: str) -> None:
    from .client import launch  # lazy import

    launch_subprocess(launch, name="DBFHClient")


components.append(
    Component(
        "Desert Bus For Hope Client",
        func=run_client,
        component_type=Type.CLIENT,
    )
)


class DBFHWebWorld(WebWorld):
    theme = "dirt"


class DBFHItem(Item):
    game = GAME


class DBFHRegion(Region):
    game = GAME


class DBFHLocation(Location):
    game = GAME

    force_progress: bool = False

    def __init__(self, player: int, name: str, address: int | None, parent: Region,
                 force_progression_item: bool) -> None:
        super().__init__(player, name, address, parent)
        self.force_progress = force_progression_item

    def can_fill(self, state: CollectionState, item: Item, check_access: bool = True) -> bool:
        if self.force_progress and not item.advancement:
            return False

        return super().can_fill(state, item, check_access=check_access)


class DBFHWorld(World):
    game = GAME
    options_dataclass = DBFHOptions

    web = DBFHWebWorld()

    options: DBFHOptions
    topology_present = False

    item_names: ClassVar[set[str]] = {x.name for x in DBFHItems}
    item_id_to_name: ClassVar[dict[int, str]] = {x.id: x.name for x in DBFHItems}
    item_name_to_id: ClassVar[dict[str, int]] = {x.name: x.id for x in DBFHItems}

    location_id_to_name: ClassVar[dict[int, str]] = {
        location_id(check, hour, slot): location_name(check, hour, slot)
        for hour in range(MAX_HOURS) for slot in range(MAX_SLOTS) for check in Checks
    }
    location_names: ClassVar[set[str]] = set(location_id_to_name.values())
    location_name_to_id: ClassVar[dict[str, int]] = {name: id for id, name in location_id_to_name.items()}

    def create_regions(self) -> None:
        hours = max(
            self.options.hour_played_max.value,
            self.options.hour_funded_max.value
        )
        days = hours // 24 + (0 if hours % 24 else 1)

        last_region = DBFHRegion("Menu", self.player, self.multiworld)
        self.multiworld.regions.append(last_region)

        for day in range(days):
            region = DBFHRegion(
                f"Desert Bus Day {day + 1}",
                self.player,
                self.multiworld
            )
            self.multiworld.regions.append(region)

            last_hour_previous_day = day * 24 - 1
            if last_hour_previous_day > 0:
                last_region.add_exits(
                    exits={region.name: f"Dawn of Day {day + 1}"},
                    rules={region.name: _gen_rule_event(
                        self.player, Checks.HourFunded, last_hour_previous_day
                    )},
                )
            else:
                last_region.add_exits(exits={region.name: f"Dawn of Day {day + 1}"})

            last_region = region

    def create_items(self) -> None:
        hour_funded = self.options.hour_funded_max.value
        hour_funded_items = self.options.hour_funded_items.value
        hour_funded_priority = self.options.hour_funded_force_progression_items.value

        hour_played = self.options.hour_played_max.value
        hour_played_items = self.options.hour_played_items.value
        hour_played_priority = self.options.hour_played_force_progression_items.value

        hours = max(hour_funded, hour_played)

        progression_items = int(hours * self.options.driver_goods_factor.value / 100)
        total_locations = hour_funded * hour_funded_items + hour_played * hour_played_items

        for _ in range(progression_items):
            self.multiworld.itempool.extend((
                DBFHItem(
                    DBFHItems.BusFuel.name,
                    ItemClassification.progression,
                    DBFHItems.BusFuel.id,
                    self.player,
                ),
                DBFHItem(
                    DBFHItems.DriverRefreshment.name,
                    ItemClassification.progression,
                    DBFHItems.DriverRefreshment.id,
                    self.player,
                ),
            ))

        unused_filler: set[DBFHItems] = {item for item in DBFHItems if item.id > 1000}
        filler = total_locations - 2 * progression_items

        logging.info("DBFH: Creating %d filler items", filler)
        for _ in range(filler):
            item = self.random.choice(list(unused_filler)) if unused_filler else DBFHItems.FartMachine
            unused_filler.discard(item)
            self.multiworld.itempool.append(
                DBFHItem(item.name, ItemClassification.filler, item.id, self.player),
            )

        for hour in range(hours):
            day = hour // 24
            region = self.get_region(f"Desert Bus Day {day + 1}")

            if hour < hour_funded:
                self.create_event(region, Checks.HourFunded, hour)
            if hour < hour_played:
                self.create_event(region, Checks.HourPlayed, hour)

            for slot in range(hour_funded_items if hour < hour_funded else 0):
                region.locations.append(DBFHLocation(
                    player=self.player,
                    name=location_name(Checks.HourFunded, hour, slot),
                    address=self.location_name_to_id[location_name(Checks.HourFunded, hour, slot)],
                    parent=region,
                    force_progression_item=slot < hour_funded_priority,
                ))

            for slot in range(hour_played_items if hour < hour_played else 0):
                region.locations.append(DBFHLocation(
                    player=self.player,
                    name=location_name(Checks.HourPlayed, hour, slot),
                    address=self.location_name_to_id[location_name(Checks.HourPlayed, hour, slot)],
                    parent=region,
                    force_progression_item=slot < hour_played_priority,
                ))

    def get_filler_item_name(self) -> str:
        return DBFHItems.FartMachine.name

    def create_event(self, region: Region, check_type: Checks, hour_zero_offset: int) -> None:
        event = event_name(check_type, hour_zero_offset)

        event_location = DBFHLocation(
            self.player,
            event,
            None,
            region,
            False,
        )

        event_item = DBFHItem(
            event,
            ItemClassification.progression_skip_balancing,
            None,
            self.player,
        )

        event_location.show_in_spoiler = False
        event_location.place_locked_item(event_item)
        region.locations.append(event_location)

    def create_filler(self) -> DBFHItem:
        return DBFHItem(
            DBFHItems.FartMachine.name,
            ItemClassification.filler,
            DBFHItems.FartMachine.id,
            self.player
        )

    def _loc(self, check_type: Checks, hour_zero_offset: int,
             slot_zero_offset: int = 0) -> Location:
        return self.multiworld.get_location(
            location_name(check_type, hour_zero_offset, slot_zero_offset),
            self.player
        )

    def set_rules(self) -> None:
        rules: list[CollectionRule]

        # Generate the expected rules for Hour Funded
        #
        # Logically requires:
        #   - (N-1) increments of progressive `Bus Fuel`
        #   - (N-1) increments of progressive `Driver Refreshments`
        #   - Event `Hour Funded [N-1]` (assuming previous hour)
        #   - Event `Hour Played [...]` -- based on the last 5 Desert Buses, the earliest this hour
        #     has been purchased in a run (rounded down). This is primarily a hint for ordering
        #     between funded and played
        #
        # This change uses (N-1) hours to allow a world to be self-fulfilling.
        # This represents the initial donation made by DB/LRR at opening.
        for hour in range(self.options.hour_funded_max.value):
            event = event_name(Checks.HourFunded, hour)

            rules = [_gen_rule_resources(self.player, hour - 1)]

            if hour:
                rules.append(_gen_rule_event(self.player, Checks.HourFunded, hour - 1))

            if funded_at := _estimated_played_hour_for_funded_hour(hour):
                rules.append(_gen_rule_event(self.player, Checks.HourPlayed, funded_at))

            event_location = self.multiworld.get_location(event, self.player)
            set_rules(event_location, rules)

            slot_rule = (_gen_rule_event(self.player, Checks.HourFunded, hour),)
            for slot in range(self.options.hour_funded_items.value):
                location = self._loc(Checks.HourFunded, hour, slot)
                set_rules(location, slot_rule)

        # Generate the expected rules for Hour Played
        #
        # Logically requires:
        #  - N increments of progressive `Bus Fuel`
        #  - N increments of progressive `Driver Refreshments`
        #  - Event `Hour Funded [N]`
        #  - Event `Hour Played [N-1]` (assuming N>1)
        for hour in range(self.options.hour_played_max.value):
            event = event_name(Checks.HourPlayed, hour)
            event_location = self.multiworld.get_location(event, self.player)

            rules = [
                _gen_rule_resources(self.player, hour),
                _gen_rule_event(self.player, Checks.HourFunded, hour),
            ]

            if hour:
                rules.append(_gen_rule_event(self.player, Checks.HourPlayed, hour - 1))

            set_rules(event_location, rules)

            slot_rule = (_gen_rule_event(self.player, Checks.HourPlayed, hour),)
            for slot in range(self.options.hour_played_items.value):
                location = self._loc(Checks.HourPlayed, hour, slot)
                set_rules(location, slot_rule)

        # Set up the Goal conditions
        match self.options.goal.value:
            case self.options.goal.option_final_hour_funded:
                goal_type = Checks.HourFunded
                goal_hour = self.options.hour_funded_max.value - 1
            case self.options.goal.option_final_hour_played:
                goal_type = Checks.HourPlayed
                goal_hour = self.options.hour_played_max.value - 1
            case _:
                return

        goal = _gen_rule_event(self.player, goal_type, goal_hour)
        self.multiworld.completion_condition[self.player] = goal

    def fill_slot_data(self) -> DBFHSlotData:
        return DBFHSlotData(
            api_url=self.options.api_base_url.value,
            vst_url=self.options.vst_base_url.value,
            death_link=bool(self.options.death_link.value),
            funded_hours=self.options.hour_funded_max.value,
            funded_items=self.options.hour_funded_items.value,
            played_hours=self.options.hour_played_max.value,
            played_items=self.options.hour_played_items.value,
            goal_type=self.options.goal.value,
            goal_hours=(
                self.options.hour_funded_max.value
                if self.options.goal.value == self.options.goal.option_final_hour_funded
                else self.options.hour_played_max.value
            )
        )


def set_rules(spot: Location, rules: list[CollectionRule] | tuple[CollectionRule]) -> None:
    if spot.access_rule is not Location.access_rule:
        raise RuntimeError("Rule already set? " + spot.name + " " + str(spot.access_rule))

    spot.access_rule = lambda state: all(rule(state) for rule in rules)


def _estimated_played_hour_for_funded_hour(hour_zero_offset: int) -> int:
    """Stub for future historical timing data.

    Returns the played hour assumed to line up with a funded hour.
    """
    return {
        109: 1, 110: 1, 111: 2, 112: 2, 113: 2, 114: 3, 115: 4, 116: 5, 117: 5,
        118: 6, 119: 7, 120: 7, 121: 8, 122: 10, 123: 14, 124: 15, 125: 17,
        126: 18, 127: 18, 128: 19, 129: 21, 130: 23, 131: 25, 132: 26,
        133: 28, 134: 28, 135: 29, 136: 32, 137: 32, 138: 36, 139: 42,
        140: 44, 141: 45, 142: 49, 143: 50
    }.get(hour_zero_offset, 0)


def _gen_rule_resources(player: int, hour_zero_offset: int) -> CollectionRule:
    return lambda state: (
            state.has(DBFHItems.BusFuel.name, player, hour_zero_offset + 1)
            and state.has(DBFHItems.DriverRefreshment.name, player, hour_zero_offset + 1)
    )


def _gen_rule_event(
        player: int, check_type: Checks, hour_zero_offset: int
) -> CollectionRule:
    event = event_name(check_type, hour_zero_offset)
    return lambda state: state.has(event, player)
