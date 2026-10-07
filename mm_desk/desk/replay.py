"""Replay the tape event by event with an honest fill model.

  * Every action (place / cancel) lands at decision + order latency + feed latency
    on our receive timeline. Until a cancel lands, the order can still be filled.
  * Fills come from matching.QueueSim: back of the visible queue, fill only after
    everything ahead at our price has traded (or a print goes through us).
  * Real maker fees on every fill; funding charged on inventory at every print.
  * Refuses to run on tape that overlaps the pressure-coefficient fit window.

Prevents: D3 (fees, funding), D4 (latency on every action), D7 (queue model,
fit/test overlap), D8 (funding).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from itertools import count
from typing import Iterable, Protocol

import numpy as np

from .book import Book
from .config import MS, NS, DeskConfig
from .events import BookUpdate, Event, Funding, Trade
from .jev import Decision, Jev
from .matching import QueueSim, Resting
from .orders import Cancel, Order, Place, plan_actions
from .pricing import Quote, min_spread
from .pull import reason_keys


class PeekError(AssertionError):
    """Research touched data it could not have had."""


class Trader(Protocol):
    inventory: float
    def on_event(self, ev: Event) -> Decision: ...
    def on_fill(self, side: int, size: float) -> None: ...


@dataclass(slots=True)
class Fill:
    ts: int
    side: int
    price: float
    size: float
    fee: float
    oid: int
    order_size: float
    vol: float
    queue_wait_ns: int


@dataclass
class ReplayResult:
    fills: list[Fill] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)   # every quote Jev produced (if kept)
    places: int = 0
    place_ts: list[int] = field(default_factory=list)
    cancels: int = 0
    rejects: int = 0
    pulls: dict[str, int] = field(default_factory=dict)
    fees: float = 0.0
    funding: float = 0.0
    cash: float = 0.0
    inventory: float = 0.0
    mid_ts: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    mid_px: np.ndarray = field(default_factory=lambda: np.empty(0))
    equity_ts: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    equity: np.ndarray = field(default_factory=lambda: np.empty(0))
    inv_series: list[tuple[int, float]] = field(default_factory=list)
    start_ts: int = 0
    end_ts: int = 0

    @property
    def pnl(self) -> float:
        return float(self.equity[-1]) if len(self.equity) else 0.0

    def mid_at(self, ts: np.ndarray | int) -> np.ndarray:
        """Last mid printed at or before ts (NaN before the first)."""
        i = np.searchsorted(self.mid_ts, ts, side="right") - 1
        out = np.where(i >= 0, self.mid_px[np.clip(i, 0, None)], np.nan)
        return out


class SymmetricQuoter:
    """The dumb baseline: mid +/- (fees + tick)/2, fixed size, no skew, never pulls."""

    def __init__(self, cfg: DeskConfig) -> None:
        self.cfg, self.book, self.inventory = cfg, Book(), 0.0
        self.delay_ns = int(cfg.latency.order_ms * MS)

    def on_fill(self, side: int, size: float) -> None:
        self.inventory = round(self.inventory + side * size, 10)

    def on_event(self, ev: Event) -> Decision:
        if isinstance(ev, BookUpdate):
            self.book.apply(ev)
        mid = self.book.mid()
        if mid is None:
            return Decision(None, "book_invalid", None)
        tick, half = self.cfg.market.tick, min_spread(self.cfg, mid) / 2
        bid = round(int((mid - half) / tick) * tick, 10)
        ask = round(-int(-(mid + half) / tick) * tick, 10)
        bid = min(bid, self.book.best_ask[0] - tick)
        ask = max(ask, self.book.best_bid[0] + tick)
        cap, s = self.cfg.pricing.max_inventory, self.cfg.pricing.base_size
        q = Quote(bid=bid if self.inventory < cap else None, bid_size=s,
                  ask=ask if self.inventory > -cap else None, ask_size=s,
                  centre=mid, spread=ask - bid, as_of=ev.recv_ts, decision_ts=ev.recv_ts,
                  live_ts=ev.recv_ts + self.delay_ns)
        return Decision(q, None, None)


class DoNothing:
    inventory = 0.0
    def on_fill(self, side: int, size: float) -> None: ...
    def on_event(self, ev: Event) -> Decision: return Decision(None, "do_nothing", None)


def replay(tape: Iterable[Event], cfg: DeskConfig, trader: Trader | None = None,
           keep_quotes: bool = False, equity_every_s: float = 1.0,
           trade_from_ns: int = 0) -> ReplayResult:
    """Events before trade_from_ns only warm Jev up (book, signals); no orders, no PnL."""
    trader = trader if trader is not None else Jev(cfg)
    m = cfg.market
    land_delay = int((cfg.latency.order_ms + cfg.latency.feed_ms) * MS)
    sim = QueueSim(cfg.signals.book_levels)
    book = sim.book
    res = ReplayResult()
    working: dict[int, Order] = {}
    pending: list[tuple[int, int, object]] = []
    seq = count()
    mid_ts: list[int] = []
    mid_px: list[float] = []
    eq_ts: list[int] = []
    eq: list[float] = []
    eq_step = int(equity_every_s * NS)
    next_eq = 0
    last_vol = 0.0
    prev_keys: tuple[str, ...] = ()
    first = first_seen = True

    def fill(r: Resting, qty: float, t: int) -> None:
        o, px = r.order, r.order.price
        fee = px * qty * m.maker_fee
        res.cash += -o.side * px * qty - fee
        res.fees += fee
        res.inventory = round(res.inventory + o.side * qty, 10)
        res.inv_series.append((t, res.inventory))
        res.fills.append(Fill(t, o.side, px, qty, fee, o.oid, o.size, last_vol, t - r.landed))
        trader.on_fill(o.side, qty)
        if o.remaining <= 0:
            if working.get(o.side) is o:
                del working[o.side]

    for ev in tape:
        t = ev.recv_ts
        if first_seen:
            first_seen = False
            fit_end = cfg.pricing.pressure_fit_end_ns
            if fit_end and fit_end >= t:
                raise PeekError(f"pressure_coef was fit on tape up to {fit_end}, replay starts at {t}")
        if t < trade_from_ns:
            if isinstance(ev, BookUpdate):
                book.apply(ev)
            trader.on_event(ev)
            continue
        if first:
            first = False
            res.start_ts = t
            next_eq = t
        res.end_ts = t

        # 1. actions whose time has come reach the exchange
        while pending and pending[0][0] <= t:
            _, _, act = heapq.heappop(pending)
            if isinstance(act, Place):
                o = act.order
                if not sim.land_place(o, t):
                    res.rejects += 1
                    if working.get(o.side) is o:
                        del working[o.side]
            else:
                sim.land_cancel(act.oid)

        # 2. the market moves; our resting orders may trade
        if isinstance(ev, Trade):
            for r, qty in sim.on_trade(ev):
                fill(r, qty, t)
        elif isinstance(ev, BookUpdate):
            book.apply(ev)
            mid = book.mid()
            if mid is not None and (not mid_px or mid != mid_px[-1]):
                mid_ts.append(t)
                mid_px.append(mid)
        elif isinstance(ev, Funding) and ev.settled:
            pay = res.inventory * ev.mark * ev.rate
            res.cash -= pay
            res.funding += pay

        # 3. Jev looks at the event and decides
        d = trader.on_event(ev)
        if d.view is not None:
            last_vol = d.view.vol
        keys = reason_keys(d.reason)
        if keys != prev_keys:                         # count pull episodes, not events
            for k in set(keys) - set(prev_keys):
                res.pulls[k] = res.pulls.get(k, 0) + 1
        prev_keys = keys
        if d.quote is not None and keep_quotes:
            res.quotes.append(d.quote)
        for act in plan_actions(d.quote, working, cfg):
            if isinstance(act, Place):
                working[act.order.side] = act.order
                res.places += 1
                res.place_ts.append(t)
            else:
                for s, o in list(working.items()):
                    if o.oid == act.oid:
                        del working[s]
                res.cancels += 1
            heapq.heappush(pending, (t + land_delay, next(seq), act))

        # 4. mark to market
        while next_eq <= t and mid_px:
            eq_ts.append(next_eq)
            eq.append(res.cash + res.inventory * mid_px[-1])
            next_eq += eq_step

    res.mid_ts = np.asarray(mid_ts, np.int64)
    res.mid_px = np.asarray(mid_px, float)
    res.equity_ts = np.asarray(eq_ts, np.int64)
    res.equity = np.asarray(eq, float)
    return res
