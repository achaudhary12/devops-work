"""Synthetic BTC-PERP tape for tests, demos and red-team baselines.

NOT a substitute for recorded tape — pass_or_die only counts real tape. It exists
so every module can be exercised before the recorder has a week of data.

The generator hides a slowly mean-reverting drift that leaks into L1 imbalance
and trade aggressor side, so there is something real for the pressure fit to
find, and informed flow for the report card to catch.
"""
from __future__ import annotations

import math

import numpy as np

from .config import MS, NS
from .events import BookUpdate, Event, Funding, Liquidation, Trade

T0 = 1_767_225_600 * NS  # 2026-01-01 00:00:00 UTC


def synth_tape(seconds: float = 3600, seed: int = 0, start_ns: int = T0, mid0: float = 60_000.0,
               tick: float = 0.1, vol: float = 5.0, step_s: float = 0.1, trade_rate: float = 6.0,
               informed: float = 0.25, lag_ms: tuple[float, float] = (15.0, 45.0),
               lag_spike_p: float = 2e-4, liq_per_hour: float = 1.0, funding_rate: float = 1e-4,
               funding_every_s: int = 8 * 3600, levels: int = 10) -> list[Event]:
    rng = np.random.default_rng(seed)
    steps = int(seconds / step_s)
    sq = math.sqrt(step_s)
    out: list[Event] = []
    seq = 0
    last_recv = 0
    spike_left = 0

    def stamp(exch: int) -> int:
        nonlocal last_recv, spike_left
        lag = rng.uniform(*lag_ms)
        if spike_left > 0:
            spike_left -= 1
            lag += 300
        elif rng.random() < lag_spike_p:
            spike_left = 20
        last_recv = max(last_recv + 1, exch + int(lag * MS))
        return last_recv

    def emit(make) -> None:
        nonlocal seq
        seq += 1
        out.append(make(seq))

    p = mid0
    drift = 0.0
    prev_bid: float | None = None
    for i in range(steps):
        t = start_ns + int(i * step_s * NS)
        drift = 0.97 * drift + 0.03 * rng.normal(0, vol * 0.6)   # $/s, mean-reverting
        jump = 0.0
        if rng.random() < liq_per_hour * step_s / 3600:
            side = -1 if rng.random() < 0.5 else 1
            size = float(rng.uniform(2, 20))
            jump = side * size * 4 * tick * 5
            ts = t + 1
            emit(lambda s: Liquidation(ts, stamp(ts), s, p, size, side))
        dp = drift * step_s + vol * sq * rng.normal() + jump
        p_next = p + dp

        # trades during this step: informed toward where the price is going
        n = rng.poisson(trade_rate * step_s)
        for k in range(n):
            ts = t + int((k + 1) / (n + 1) * step_s * NS)
            agg = (1 if dp > 0 else -1) if rng.random() < 0.5 + informed else (1 if rng.random() < 0.5 else -1)
            bid = math.floor(p / tick) * tick
            px = round(bid + tick if agg > 0 else bid, 10)
            sz = round(float(rng.exponential(0.05)) + 0.001, 3)
            emit(lambda s, ts=ts, px=px, sz=sz, agg=agg: Trade(ts, stamp(ts), s, px, sz, agg))

        # the price moved: anything between old and new touch was swept
        bid_next = math.floor(p_next / tick) * tick
        if prev_bid is not None and abs(bid_next - prev_bid) >= tick * 1.5:
            d = 1 if bid_next > prev_bid else -1
            ts = t + int(step_s * NS) - 2
            first = prev_bid if d < 0 else prev_bid + tick
            last = bid_next + tick if d < 0 else bid_next
            n_lv = int(round(abs(last - first) / tick)) + 1
            # a sweep prints at the old touch, somewhere in between, and the last level taken
            for k in sorted({0, n_lv // 2, n_lv - 1}):
                px, sz = round(first + d * k * tick, 10), round(float(rng.exponential(0.4)) + 0.01, 3)
                emit(lambda s, px=px, sz=sz: Trade(ts, stamp(ts), s, px, sz, d))

        # book snapshot at the end of the step; L1 imbalance leaks the drift
        p = p_next
        ts = t + int(step_s * NS) - 1
        bb = round(bid_next, 10)
        tilt = math.tanh(drift / (vol + 1e-9))
        bids, asks = [], []
        for k in range(levels):
            base = float(rng.exponential(0.6)) + 0.05 + 0.2 * k
            bsz = base * (1 + 0.8 * tilt) if k == 0 else base
            asz = float(rng.exponential(0.6)) + 0.05 + 0.2 * k
            asz = asz * (1 - 0.8 * tilt) if k == 0 else asz
            bids.append((round(bb - k * tick, 10), round(max(bsz, 0.01), 3)))
            asks.append((round(bb + (k + 1) * tick, 10), round(max(asz, 0.01), 3)))
        emit(lambda s: BookUpdate(ts, stamp(ts), s, tuple(bids), tuple(asks), snapshot=True))
        prev_bid = bb

        # funding: a predicted-rate print each minute, a settlement at the boundary
        on_boundary = t % (funding_every_s * NS) == 0
        if on_boundary or i % int(60 / step_s) == 0:
            nxt = (t // (funding_every_s * NS) + 1) * funding_every_s * NS
            emit(lambda s: Funding(t, stamp(t), s, funding_rate, nxt, p, on_boundary))

    out.sort(key=lambda e: e.recv_ts)
    return out
