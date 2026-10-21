from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging
import sys
import uuid
from argparse import Namespace
from collections.abc import AsyncIterator, Coroutine
from typing import Any, NamedTuple

import aiohttp
import colorama

import Utils
from CommonClient import (
    ClientCommandProcessor,
    CommonContext,
    get_base_parser,
    logger,
    server_loop,
)
from NetUtils import ClientStatus

from .common import GAME, Checks, DBFHSlotData, hours_unlocked_by_amount, location_id
from .prizes import DBFHItems


class Crash(NamedTuple):
    minute: int
    fingerprint: tuple[str, int, str]
    description: str


class DBFHCommandProcessor(ClientCommandProcessor):
    ctx: DBFHContext


class EventDetails(NamedTuple):
    api_event_id: str
    vst_event_id: str
    event_start: datetime.datetime


class DBFHContext(CommonContext):
    game = GAME
    items_handling = 0b111  # receive all items for /received
    want_slot_data = True
    command_processor = DBFHCommandProcessor

    event: EventDetails | None
    slot_data: DBFHSlotData | None

    _connect: asyncio.Event
    _tasks: set[asyncio.Task[None]]
    _session: aiohttp.ClientSession | None
    _vst_url: str | None

    processed_unlocks_funded: int
    processed_unlocks_played: int
    seen_crashes: set[tuple[str, int, str]]

    def __init__(self, server_address: str | None = None, password: str | None = None) -> None:
        super().__init__(server_address, password)
        self.slot_data = None
        self.event = None

        self._connected = asyncio.Event()
        self._tasks = set()
        self._session = None
        self._vst_url = None

        # State we have successfully processed and synced to the multiworld.
        self.processed_unlocks_funded = 0
        self.processed_unlocks_played = 0
        self.seen_crashes = set()

    def task(self, coro: Coroutine[Any, Any, None], name: str | None = None) -> None:
        """
        Helper method for creating a task, and ensuring its ref is not lost.
        """

        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_end)
        self._tasks.add(task)

    def _task_end(self, task: asyncio.Task[None]) -> None:
        """
        Handle the end of a task, logging any exception.
        """
        self._tasks.discard(task)

        if task.cancelled():
            return

        exc = task.exception()

        if not isinstance(exc, (asyncio.TimeoutError, asyncio.CancelledError)):
            logger.exception(exc)
            pass

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()  # typing: ignore[no-untyped-call]
        await self.send_connect()

    def on_package(self, cmd: str, args: dict[str, Any]) -> None:
        if cmd != "Connected":
            return

        self.slot_data = args.get("slot_data")

        assert self.slot_data is not None, "missing slot data"

        self.task(self.update_death_link(bool(self.slot_data["death_link"])))

        # TODO: review self.checked_locations to give initial values for
        #       self.processed_unlocks_funded and self.processed_unlocks_played
        #       based on the locations already checked.
        #       (this is optional -- the code will auto reconcile on the first
        #       pass, but it would be neater)

        logger.info("Marking as connected to Archipelago")
        self._connected.set()

    async def client_loop(self) -> None:
        logger.info("Waiting for connection event")
        await self._connected.wait()

        assert self.slot_data, "no slot data loaded?"

        self._session = aiohttp.ClientSession(base_url=self.slot_data["api_url"])
        self._vst_url = self.slot_data["vst_url"].strip("/")

        logger.info("Loading Desert Bus For Hope event details")
        while not self.exit_event.is_set():
            try:
                self.event = await self._get_event_info()
                break

            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.warning("Unable to retrieve Desert Bus event information: %s", exc)
                try:
                    await asyncio.wait_for(self.exit_event.wait(), timeout=30)
                except TimeoutError:
                    pass

        if self.event is None:
            logger.error("Unable to find Desert Bus for hope event")
            await self._session.close()
            self._session = None
            return

        logger.info("Found event %s", self.event)
        logger.info("Launching Bus watcher tasks")
        self.task(self._watch_donation_total(), name="DBFH donations")
        self.task(self._watch_elapsed_time(), name="DBFH elapsed time")

        if bool(self.slot_data["death_link"]):
            logger.info("Launching Crash (death link) watcher")
            self.task(self._watch_crashes(), name="DBFH crashes")

        try:
            await self.exit_event.wait()
        finally:
            for task in self._tasks:
                task.cancel()

            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._session.close()
            self._session = None

    async def shutdown(self) -> None:
        logger.info("Shutdown called")
        self.exit_event.set()
        self._connected.set()  # 'cancel' connection
        await asyncio.gather(*self._tasks)
        await super().shutdown()  # typing: ignore[no-untyped-call]

    async def _get_event_info(self) -> EventDetails:
        assert self._session is not None, "missing http client"

        async with self._session.get("events", verify_ssl=False) as response:
            response.raise_for_status()
            events = await response.json()

        # Find the currently active Desert Bus Event
        event = next((event for event in events["events"] if event["primary"]), None)
        assert event, "no primary events found"

        # Determine the start date
        start = datetime.datetime.fromisoformat(event["starts_at"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.UTC)

        # Determine the equivalent VST event ID
        event_url = str(event["url"])
        if event_url.startswith("express"):
            vst_id = "DBEX" + str(int(event_url.removeprefix("express")) - 2024)
        else:
            vst_id = "DB" + str(int(event_url) - 2006)

        return EventDetails(event["id"], vst_id, start)

    async def _watch_donation_total(self) -> None:
        assert self._session is not None, "missing http client"
        assert self.event is not None, "missing event details"

        topic = f"total:{self.event.api_event_id}"

        socket = PhoenixClient(self._session, "socket/websocket?vsn=2.0.0", topic)

        async for received_topic, payload in socket:
            if received_topic != topic:
                continue

            amount = float(payload.get("response", {}).get("amount", 0))
            unlocked = hours_unlocked_by_amount(amount)
            logger.info("Amount is now %.2f, for %d hours funded", amount, unlocked)

            if unlocked > self.processed_unlocks_funded:
                logger.debug("Attempting to mark %d hours funded", unlocked)
                await self._submit_locations(funded=unlocked)

    async def _watch_elapsed_time(self) -> None:
        assert self.event is not None

        while not self.exit_event.is_set():
            logger.debug("Current bus time (minutes): %d", self._current_bus_minute())
            hours = self._current_bus_minute() // 60

            if hours > self.processed_unlocks_played:
                logger.debug("Attempting to mark play time up to %d hours", hours)
                await self._submit_locations(played=hours)

            try:
                await asyncio.wait_for(self.exit_event.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _watch_crashes(self) -> None:
        assert self.event is not None, "event data missing"

        previous = max(0, self._current_bus_minute() - 1)

        while not self.exit_event.is_set():
            current = self._current_bus_minute()
            if "DeathLink" in self.tags and current >= previous:
                logger.info("Checking for crashes from minute %d to minute %d", previous, current)
                async for crash in self._check_for_crashes_since(previous, current, self.event.vst_event_id):
                    if crash.fingerprint in self.seen_crashes:
                        continue

                    self.seen_crashes.add(crash.fingerprint)
                    await self.send_death(f"Desert Bus crashed: {crash.description}")

                previous = current

            try:
                await asyncio.wait_for(self.exit_event.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _check_for_crashes_since(
        self, bus_time: int, current_bus_time: int, vst_id: str
    ) -> AsyncIterator[Crash]:
        assert self._session is not None, "missing http client"

        first_day = bus_time // (24 * 60) + 1
        last_day = current_bus_time // (24 * 60) + 1

        for day in range(first_day, last_day + 1):
            try:
                logger.debug("Reading %s", f"{self._vst_url}/{vst_id}/synced_data/day{day}.csv")
                async with self._session.get(
                    f"{self._vst_url}/{vst_id}/synced_data/day{day}.csv",
                    verify_ssl=False,
                ) as response:
                    if response.status == 404:
                        continue
                    response.raise_for_status()
                    text = await response.text()
            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.warning("Unable to read VST day %d: %s", day, exc)
                continue

            for row in csv.reader(io.StringIO(text)):
                if len(row) < 4 or row[2] != "Crash":
                    continue
                minute = self._parse_bus_time(row[0])
                if minute is None or not bus_time < minute <= current_bus_time:
                    continue
                description = row[3]
                yield Crash(minute, (vst_id, minute, description), description.removeprefix("^ "))

    @staticmethod
    def _parse_bus_time(value: str) -> int | None:
        if not (value := value.strip()):
            return None

        try:
            hours, minutes = (int(part) for part in value.split(":", 1))
        except (ValueError, IndexError):
            return None

        # note: hours _can_ be negative (events that occurred before official start time)
        if minutes < 0 or minutes >= 60:
            return None

        return hours * 60 + minutes

    def _current_bus_minute(self) -> int:
        assert self.event is not None
        now = datetime.datetime.now(datetime.UTC)
        return max(0, int((now - self.event.event_start).total_seconds() // 60))

    async def _submit_locations(self, funded: int = 0, played: int = 0) -> None:
        assert self.slot_data is not None, "missing slot data"

        fuel_count = self._resource_count(DBFHItems.BusFuel)
        snack_count = self._resource_count(DBFHItems.DriverRefreshment)
        resourced_hours = min(fuel_count, snack_count)
        logger.debug("We have received %d fuel cans and %d snacks", fuel_count, snack_count)

        funded = min(funded, resourced_hours, self.slot_data["funded_hours"])
        played = min(played, resourced_hours, self.slot_data["played_hours"])

        logger.debug("Submitting checks for %d hours funded and %d hours played", funded, played)

        locations: set[int] = set()

        if funded >= self.processed_unlocks_funded:
            locations |= self._locations_for_hour_range(Checks.HourFunded, self.processed_unlocks_funded, funded)

        if played >= self.processed_unlocks_played:
            locations |= self._locations_for_hour_range(Checks.HourPlayed, self.processed_unlocks_played, played)

        await self.check_locations(locations)

        self.processed_unlocks_funded = max(funded, self.processed_unlocks_funded)
        self.processed_unlocks_played = max(played, self.processed_unlocks_played)

        goal_type = self.slot_data["goal_type"]
        goal_hours = int(self.slot_data["goal_hours"])
        goal_reached = (
            funded >= goal_hours if goal_type == 1 else played >= goal_hours
        )

        if goal_reached and not self.finished_game:
            self.finished_game = True
            await self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])

    def _resource_count(self, item: DBFHItems) -> int:
        return sum(received.item == item.id for received in self.items_received)

    def _locations_for_hour_range(self, check_type: Checks, from_hours: int, to_hours: int) -> set[int]:
        assert self.slot_data is not None, "missing slot data"

        match check_type:
            case Checks.HourFunded:
                slots = int(self.slot_data["funded_items"])
            case Checks.HourPlayed:
                slots = int(self.slot_data["played_items"])
            case _:
                raise RuntimeError

        return {
            location_id(check_type, hour, slot)
            for hour in range(from_hours, to_hours + 1)
            for slot in range(slots)
        }


class PhoenixMessage(NamedTuple):
    join_ref: str | None
    msg_ref: str
    topic: str
    event: str
    payload: Any


class PhoenixClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, *topics: str) -> None:
        self.logger = logging.getLogger(__name__)
        self.session = session
        self.url = url
        self.topics = set(topics)
        self.socket: aiohttp.ClientWebSocketResponse | None = None
        self.stop_event = asyncio.Event()
        self.join_ref = str(uuid.uuid4())

    async def _heartbeat(self, interval: float = 10) -> None:
        while True:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

            if not self.socket or self.socket.closed:
                continue

            try:
                await self.socket.send_json(PhoenixMessage(None, self.join_ref, "phoenix", "heartbeat", {}))
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                self.logger.warning("Phoenix heartbeat failed: %s", exc)

    async def __aiter__(self) -> AsyncIterator[tuple[str, Any]]:
        heartbeat = asyncio.create_task(self._heartbeat(), name="Phoenix heartbeat")
        try:
            while not self.stop_event.is_set():
                try:
                    self.socket = await self.session.ws_connect(self.url)
                    for topic in self.topics:
                        await self.socket.send_json(
                            PhoenixMessage(self.join_ref, self.join_ref, topic, "phx_join", {})
                        )
                    while not self.stop_event.is_set() and not self.socket.closed:
                        try:
                            message = await self.socket.receive_json(timeout=2)
                        except TimeoutError:
                            continue
                        parsed = PhoenixMessage(*message)
                        if parsed.topic in self.topics:
                            yield parsed.topic, parsed.payload
                except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                    self.logger.warning("Phoenix connection failed: %s", exc)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=5)
                    except TimeoutError:
                        pass
                finally:
                    if self.socket:
                        await self.socket.close()
                        self.socket = None

                if not self.stop_event.is_set():
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=5)
                    except TimeoutError:
                        pass

        finally:
            self.stop_event.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def main(args: Namespace) -> None:
    logger.info("Args: %s", args)
    ctx = DBFHContext(args.connect, args.password)
    if "name" in args:
        ctx.auth = args.name

    ctx.task(server_loop(ctx), name="DBFH server loop")
    ctx.task(ctx.client_loop(), name="DBFH client loop")

    if not sys.stdout or "--nogui" not in sys.argv:
        ctx.run_gui()  # typing: ignore[no-untyped-call]
    ctx.run_cli()  # typing: ignore[no-untyped-call]

    await ctx.exit_event.wait()
    await ctx.shutdown()


def launch(*launch_args: str) -> None:
    parser = get_base_parser("Desert Bus For Hope client")
    args = parser.parse_args(launch_args)

    Utils.init_logging("DBFHClient")

    colorama.just_fix_windows_console()
    asyncio.run(main(args))
    colorama.deinit()


if __name__ == "__main__":
    launch()
