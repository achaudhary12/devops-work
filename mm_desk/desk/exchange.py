"""Exchange access. Only the guard process ever holds an ExchangeClient.

Order types offered: post-only limit, cancel, cancel-all, and a reduce-only
market `flatten` that only the guard itself may call. There is no generic
"market order" method to misuse.

The exchange is still [exchange] in the brief. To go beyond paper, implement
`FeedAdapter` (recorder.py) and `ExchangeClient` below for it, with:
  * API key with trade permission only, withdrawals disabled, IP-whitelisted
  * keys read from .env by the guard process only
  * the venue's dead-man switch / cancel-on-disconnect enabled if it has one

Prevents: D6 (cancel-all on disconnect), D9 (no market orders), D10 (keys only here).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import AsyncIterator, Protocol

from .config import MS, DeskConfig
from .events import BookUpdate, Event, Trade
from .matching import QueueSim
from .orders import Order


class ExchangeClient(Protocol):
    connected: bool

    async def place_post_only(self, oid: int, side: int, price: float, size: float) -> None: ...
    async def cancel(self, oid: int) -> None: ...
    async def cancel_all(self) -> None: ...
    async def flatten(self, qty_signed: float) -> None:
        """Reduce-only market order. Guard-only. qty_signed > 0 buys."""
    def updates(self) -> AsyncIterator[dict]:
        """{"type": "fill"|"accepted"|"rejected"|"cancelled"|"disconnected", ...}"""


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE lines). Values never get logged."""
    out = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return {**out, **{k: v for k, v in os.environ.items() if k.startswith("EXCHANGE_")}}


def feed_adapter(cfg: DeskConfig):
    raise NotImplementedError(
        "No exchange adapter yet: the brief leaves [exchange] open. Implement recorder.FeedAdapter "
        "for your venue, or run `python -m desk.recorder --synth` for a dry run.")


def exchange_client(cfg: DeskConfig, mode: str) -> ExchangeClient:
    if mode == "paper":
        return PaperExchange(cfg)
    raise NotImplementedError(
        "Real-money client not implemented: [exchange] is unspecified. Paper first; the ladder "
        "won't allow live without a human-signed GO_LIVE file anyway.")


class PaperExchange:
    """Simulated venue driven by the live feed, using the same QueueSim as replay."""

    def __init__(self, cfg: DeskConfig) -> None:
        self.cfg = cfg
        self.sim = QueueSim(cfg.signals.book_levels)
        self.connected = True
        self._q: asyncio.Queue[dict] = asyncio.Queue()
        self._delay = cfg.latency.order_ms * MS / 1e9

    def on_market(self, ev: Event) -> None:
        if isinstance(ev, BookUpdate):
            self.sim.on_book(ev)
        elif isinstance(ev, Trade):
            for r, qty in self.sim.on_trade(ev):
                o = r.order
                self._q.put_nowait({"type": "fill", "oid": o.oid, "side": o.side, "price": o.price,
                                    "size": qty, "fee": o.price * qty * self.cfg.market.maker_fee,
                                    "remaining": o.remaining, "ts": ev.recv_ts})

    async def place_post_only(self, oid: int, side: int, price: float, size: float) -> None:
        await asyncio.sleep(self._delay)
        ok = self.sim.land_place(Order(side, price, size, oid=oid), time.time_ns())
        self._q.put_nowait({"type": "accepted" if ok else "rejected", "oid": oid,
                            "reason": None if ok else "post_only_would_cross"})

    async def cancel(self, oid: int) -> None:
        await asyncio.sleep(self._delay)
        if self.sim.land_cancel(oid) is not None:
            self._q.put_nowait({"type": "cancelled", "oid": oid})

    async def cancel_all(self) -> None:
        for oid in list(self.sim.resting):
            self.sim.land_cancel(oid)
            self._q.put_nowait({"type": "cancelled", "oid": oid})

    async def flatten(self, qty_signed: float) -> None:
        bb, ba = self.sim.book.best_bid, self.sim.book.best_ask
        if qty_signed == 0 or bb is None or ba is None:
            return
        px = ba[0] if qty_signed > 0 else bb[0]
        side = 1 if qty_signed > 0 else -1
        size = abs(qty_signed)
        self._q.put_nowait({"type": "fill", "oid": 0, "side": side, "price": px, "size": size,
                            "fee": px * size * self.cfg.market.taker_fee, "remaining": 0.0,
                            "ts": time.time_ns(), "flatten": True})

    async def updates(self) -> AsyncIterator[dict]:
        while True:
            yield await self._q.get()
