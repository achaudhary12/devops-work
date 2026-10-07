"""The four numbers Jev keeps live, each stamped with the time it was true.

    weighted mid  = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
    book pressure = (bid_size - ask_size) / (bid_size + ask_size)
    who is hitting = net aggressive volume over the last 2s (buys - sells)
    how fast it moves = realized vol of the mid over the last 1 min ($ per sqrt(s))

Signals only advance when an event is fed in; they never look at a clock and
never see an event before `update()` is called with it.

Prevents: D1 (pressure + flow skew the centre), D2/D5 (vol widens and shrinks
risk), D4/D7 (every value carries `as_of`, checked by the no-peeking test).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .book import Book
from .config import NS, Signals as SignalsCfg
from .events import BookUpdate, Event, Funding, Liquidation, Trade


@dataclass(slots=True, frozen=True)
class View:
    """Everything Jev may use to price. `as_of` = newest input's recv_ts."""
    as_of: int
    best_bid: float
    best_ask: float
    wmid: float
    pressure: float
    net_flow: float           # BTC, last flow_window_s
    flow_normal: float        # EWMA of |net_flow| per window; 0 during warmup
    vol: float                # $ per sqrt(second)
    bid_depth: float          # top-N size, for the book-collapse pull
    ask_depth: float
    feed_lag_ns: int          # recv_ts - exch_ts of the newest event
    warm: bool


class SignalState:
    def __init__(self, cfg: SignalsCfg) -> None:
        self.cfg = cfg
        self.book = Book(cfg.book_levels)
        self._flow: deque[tuple[int, float]] = deque()
        self._flow_sum = 0.0
        self._flow_ewma = 0.0
        self._flow_bucket_end = 0
        self._flow_bucket_sum = 0.0
        self._mids: deque[float] = deque(maxlen=int(cfg.vol_window_s / cfg.vol_sample_s) + 1)
        self._next_sample = 0
        self._vol: float | None = None      # cached until the next mid sample
        self._first_ts = 0
        self.last_recv = 0
        self.last_lag = 0
        self.last_liq: Liquidation | None = None
        self.funding: Funding | None = None

    # -- feed -------------------------------------------------------------
    def update(self, ev: Event) -> None:
        t = ev.recv_ts
        if self._first_ts == 0:
            self._first_ts = t
            self._next_sample = t
            self._flow_bucket_end = t + int(self.cfg.flow_window_s * NS)
        # sample the mid as it stood *before* this event, at each boundary it crossed
        self._sample_until(t)
        self._roll_flow_buckets(t)

        if isinstance(ev, BookUpdate):
            self.book.apply(ev)
        elif isinstance(ev, Trade):
            signed = ev.size * ev.aggressor
            self._flow.append((t, signed))
            self._flow_sum += signed
            self._flow_bucket_sum += signed
        elif isinstance(ev, Liquidation):
            self.last_liq = ev
        elif isinstance(ev, Funding):
            self.funding = ev
        self._expire_flow(t)
        self.last_recv = t
        self.last_lag = ev.recv_ts - ev.exch_ts

    def _sample_until(self, t: int) -> None:
        step = int(self.cfg.vol_sample_s * NS)
        mid = self.book.mid()
        while self._next_sample <= t:
            if mid is not None:
                self._mids.append(mid)
                self._vol = None
            self._next_sample += step

    def _roll_flow_buckets(self, t: int) -> None:
        step = int(self.cfg.flow_window_s * NS)
        windows_per_halflife = self.cfg.flow_norm_halflife_s / self.cfg.flow_window_s
        alpha = 1 - 0.5 ** (1 / windows_per_halflife)
        while self._flow_bucket_end <= t:
            x = abs(self._flow_bucket_sum)
            self._flow_ewma = x if self._flow_ewma == 0 else (1 - alpha) * self._flow_ewma + alpha * x
            self._flow_bucket_sum = 0.0
            self._flow_bucket_end += step

    def _expire_flow(self, t: int) -> None:
        cutoff = t - int(self.cfg.flow_window_s * NS)
        while self._flow and self._flow[0][0] <= cutoff:
            self._flow_sum -= self._flow.popleft()[1]

    # -- read -------------------------------------------------------------
    def vol(self) -> float:
        if self._vol is None:
            m = list(self._mids)
            if len(m) < 3:
                self._vol = 0.0
            else:
                diffs = [b - a for a, b in zip(m, m[1:])]
                mu = sum(diffs) / len(diffs)
                var = sum((d - mu) ** 2 for d in diffs) / (len(diffs) - 1)
                self._vol = math.sqrt(var / self.cfg.vol_sample_s)
        return self._vol

    def view(self, warmup_s: float = 0.0) -> View | None:
        b = self.book
        if not b.valid:
            return None
        (bp, bs), (ap, as_) = b.best_bid, b.best_ask
        wmid = (bp * as_ + ap * bs) / (bs + as_)
        n = self.cfg.pressure_levels
        pb, pa = b.depth_size(+1, n), b.depth_size(-1, n)
        pressure = (pb - pa) / (pb + pa) if pb + pa > 0 else 0.0
        warm = (self.last_recv - self._first_ts) >= warmup_s * NS
        return View(
            as_of=self.last_recv,
            best_bid=bp, best_ask=ap, wmid=wmid, pressure=pressure,
            net_flow=self._flow_sum,
            flow_normal=self._flow_ewma if warm else 0.0,
            vol=self.vol(),
            bid_depth=b.depth_size(+1), ask_depth=b.depth_size(-1),
            feed_lag_ns=self.last_lag, warm=warm,
        )
