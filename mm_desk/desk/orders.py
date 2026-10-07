"""Diff the quotes Jev wants against the orders we have. Shared by replay and live.

Requote only when price moves >= requote_ticks or size changes materially, so a
twitchy mid can't turn into hundreds of cancels per second.

Prevents: D9 (runaway order loop), D7 (replay and live send the same actions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

from .config import DeskConfig
from .pricing import Quote

_ids = count(1)


@dataclass(slots=True)
class Order:
    side: int            # +1 bid, -1 ask
    price: float
    size: float
    oid: int = field(default_factory=lambda: next(_ids))
    filled: float = 0.0

    @property
    def remaining(self) -> float:
        return round(self.size - self.filled, 10)


@dataclass(slots=True, frozen=True)
class Place:
    order: Order


@dataclass(slots=True, frozen=True)
class Cancel:
    oid: int


Action = Place | Cancel


def plan_actions(want: Quote | None, working: dict[int, Order], cfg: DeskConfig) -> list[Action]:
    """working: side -> live order (at most one per side)."""
    acts: list[Action] = []
    tick = cfg.market.tick
    for side in (+1, -1):
        cur = working.get(side)
        if want is None:
            px, sz = None, 0.0
        else:
            px, sz = (want.bid, want.bid_size) if side > 0 else (want.ask, want.ask_size)
        if px is None or sz <= 0:
            if cur is not None:
                acts.append(Cancel(cur.oid))
            continue
        if cur is not None:
            moved = abs(cur.price - px) >= cfg.pricing.requote_ticks * tick - 1e-9
            resized = abs(cur.remaining - sz) > cfg.pricing.requote_size_frac * sz
            if not moved and not resized:
                continue
            acts.append(Cancel(cur.oid))
        acts.append(Place(Order(side, px, sz)))
    return acts
