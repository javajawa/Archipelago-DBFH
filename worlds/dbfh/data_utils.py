from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any, TypedDict

import aiohttp

from worlds.dbfh.common import hours_unlocked_by_amount
from worlds.dbfh.options import MAX_HOURS


class SeriesData(TypedDict):
    id: str
    name: str


class SeriesList(TypedDict):
    series: list[SeriesData]


class EventData(TypedDict):
    id: str
    name: str
    url: str
    primary: bool
    starts_at: str
    logo: ImageData


class EventsList(TypedDict):
    events: list[EventData]


class ImageData(TypedDict):
    url: str
    alt: str | None
    width: int
    height: int


class CurrencyAmount(TypedDict):
    currency: str
    amount: str


class PrizeData(TypedDict):
    id: str
    type: str
    state: str
    name: str
    description: Any
    description_text: str
    starts_at: str
    ends_at: str
    images: list[ImageData]
    crafter: Any | None


class PrizesList(TypedDict):
    prizes: list[PrizeData]


async def load_prize_data() -> None:
    session = aiohttp.ClientSession(base_url="https://desertbus.org/api/")

    async with session.get("events") as req:
        events: EventsList = await req.json()

    prizes: dict[int, tuple[str, str]] = {}

    for event in events["events"]:
        async with session.get(f"events/{event['id']}/prizes") as req:
            data: PrizesList = await req.json()

        if not data.get("prizes"):
            sys.stdout.write(f"Error getting prizes for {event['name']}\n")
            continue

        for prize in data["prizes"]:
            if not prize["crafter"]:
                continue

            int_id = 1000 + int.from_bytes(prize["id"][-2:].encode("ascii"))
            if int_id in prizes:
                continue

            prizes[int_id] = prize["id"], f"{prize['name']} by {prize['crafter']['name']}"

        if len(prizes) > 500:
            break

    root = pathlib.Path(__file__).parent
    (root / "data").mkdir(exist_ok=True)
    with (root / "prizes.py").open("w", encoding="utf-8") as fh:
        fh.write('"""WARNING: This is a generated file by `data_utils.py` using API data."""\n')
        fh.write("""
# ruff: noqa: E501

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class ItemSpec(NamedTuple):
    name: str
    id: int


class DBFHItems(ItemSpec, Enum):
    BusFuel = ItemSpec("Bus Fuel Can", 1)
    DriverRefreshment = ItemSpec("Driver Refreshments", 2)
    FartMachine = ItemSpec("Fart Machine", 100)

""")

        for enum_value, (ulid, name) in sorted(prizes.items()):
            fh.write("    Prize_")
            fh.write(ulid)
            fh.write(" = ItemSpec(")
            json.dump(name, fh)
            fh.write(", ")
            fh.write(str(enum_value))
            fh.write(")\n")

    await session.close()


async def build_hours_map() -> None:
    source = pathlib.Path(__file__).parent / "data" / "graphs.json"
    with source.open("r") as stream:
        data = json.load(stream)

    latest_unlock = [0] * MAX_HOURS

    for series in data:
        last = 0
        for hour, amount in enumerate(v["y"] for v in series["values"][::4]):
            unlocked = hours_unlocked_by_amount(amount)
            for h in range(last, min(unlocked, MAX_HOURS)):
                latest_unlock[h] = max(latest_unlock[h], hour)
            last = min(unlocked, MAX_HOURS)

    mapping = {k: v for k,v in dict(enumerate(latest_unlock)).items() if v}
    sys.stdout.write(str(mapping))
    sys.stdout.write("\n")


if __name__ == "__main__":
    asyncio.run(load_prize_data())
