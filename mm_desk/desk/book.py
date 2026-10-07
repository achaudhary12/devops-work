"""Local L2 book, top-N levels, rebuilt from snapshots + deltas.

Prevents: D4 (crossed or empty book is reported, never papered over).
"""
from __future__ import annotations

from .events import BookUpdate


class Book:
    __slots__ = ("bids", "asks", "ts", "seq", "depth")

    def __init__(self, depth: int = 10) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts = 0
        self.seq = -1
        self.depth = depth

    def apply(self, u: BookUpdate) -> None:
        if u.snapshot:
            self.bids.clear()
            self.asks.clear()
        for side, levels in ((self.bids, u.bids), (self.asks, u.asks)):
            for px, sz in levels:
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ts = u.recv_ts
        self.seq = u.seq

    def top(self, side: int, n: int | None = None) -> list[tuple[float, float]]:
        """side=+1 bids (desc), -1 asks (asc)."""
        n = n or self.depth
        if side > 0:
            return sorted(self.bids.items(), reverse=True)[:n]
        return sorted(self.asks.items())[:n]

    @property
    def best_bid(self) -> tuple[float, float] | None:
        return max(self.bids.items()) if self.bids else None

    @property
    def best_ask(self) -> tuple[float, float] | None:
        return min(self.asks.items()) if self.asks else None

    @property
    def valid(self) -> bool:
        bb, ba = self.best_bid, self.best_ask
        return bb is not None and ba is not None and bb[0] < ba[0]

    def mid(self) -> float | None:
        if not self.valid:
            return None
        return (self.best_bid[0] + self.best_ask[0]) / 2

    def size_at(self, side: int, price: float) -> float:
        return (self.bids if side > 0 else self.asks).get(price, 0.0)

    def depth_size(self, side: int, n: int | None = None) -> float:
        n = n or self.depth
        d = self.bids if side > 0 else self.asks
        if len(d) <= n:
            return sum(d.values())
        return sum(sz for _, sz in self.top(side, n))
