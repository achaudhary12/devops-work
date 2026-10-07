"""The normalized tape: four event kinds, each with exchange time and our time.

All timestamps are integer nanoseconds since epoch. `recv_ts` is stamped the
instant the message leaves the socket, before parsing.

Prevents: D4 (every number has a provenance time), D7 (one schema for record,
replay and live).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Union

import orjson


@dataclass(slots=True, frozen=True)
class BookUpdate:
    exch_ts: int
    recv_ts: int
    seq: int
    bids: tuple[tuple[float, float], ...]   # (price, size); size 0 deletes the level
    asks: tuple[tuple[float, float], ...]
    snapshot: bool = False
    kind: str = "book"


@dataclass(slots=True, frozen=True)
class Trade:
    exch_ts: int
    recv_ts: int
    seq: int
    price: float
    size: float
    aggressor: int                          # +1 buyer lifted the ask, -1 seller hit the bid
    kind: str = "trade"


@dataclass(slots=True, frozen=True)
class Liquidation:
    exch_ts: int
    recv_ts: int
    seq: int
    price: float
    size: float
    side: int                               # +1 forced buy (short liquidated), -1 forced sell
    kind: str = "liq"


@dataclass(slots=True, frozen=True)
class Funding:
    exch_ts: int
    recv_ts: int
    seq: int
    rate: float                             # current/predicted rate for the next print
    next_funding_ts: int                    # ns
    mark: float
    settled: bool = False                   # True on the print itself: longs pay rate*notional
    kind: str = "funding"


Event = Union[BookUpdate, Trade, Liquidation, Funding]
_KINDS = {"book": BookUpdate, "trade": Trade, "liq": Liquidation, "funding": Funding}


def to_json(ev: Event) -> bytes:
    return orjson.dumps(asdict(ev))


def from_dict(d: dict) -> Event:
    cls = _KINDS[d["kind"]]
    if cls is BookUpdate:
        d = dict(d, bids=tuple(map(tuple, d["bids"])), asks=tuple(map(tuple, d["asks"])))
    return cls(**d)


def from_json(b: bytes | str) -> Event:
    return from_dict(orjson.loads(b))
