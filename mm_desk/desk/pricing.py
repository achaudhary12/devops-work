"""How Jev prices. A pure function: View + inventory -> two prices and sizes.

    fair   = weighted mid + pressure_coef * pressure       (coef fitted on past tape only)
    centre = fair - inventory * risk * vol^2
    spread = max(fees_round_trip + 1 tick, ...) + vol_spread_k * vol + bucket widening

Holding too much long moves both quotes down until someone takes it off our
hands. The side that adds inventory shrinks linearly to zero at max_inventory.

Prevents: D1 (pressure skew + bucket widening), D2 (inventory skew, size shrink),
D3 (spread floor = fees + tick), D7 (same function in research, replay, live).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import DeskConfig
from .signals import View

_EPS = 1e-9


@dataclass(slots=True, frozen=True)
class Quote:
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    centre: float
    spread: float
    as_of: int          # newest input timestamp
    decision_ts: int    # when Jev produced it (>= as_of)
    live_ts: int        # when it can rest on the exchange = decision + measured delay


def _floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + _EPS) * tick, 10)


def _ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - _EPS) * tick, 10)


def _lots(x: float, lot: float) -> float:
    return round(math.floor(x / lot + _EPS) * lot, 10)


def min_spread(cfg: DeskConfig, price: float) -> float:
    """Round-trip maker fees + one tick. The gap never goes below this."""
    return 2 * max(cfg.market.maker_fee, 0.0) * price + cfg.market.tick


def price(view: View, inventory: float, cfg: DeskConfig, widen_bps: float = 0.0
          ) -> tuple[float | None, float, float | None, float, float, float]:
    """Returns (bid, bid_size, ask, ask_size, centre, spread)."""
    p, m = cfg.pricing, cfg.market
    fair = view.wmid + p.pressure_coef * view.pressure
    centre = fair - inventory * p.risk * view.vol ** 2
    spread = (min_spread(cfg, view.wmid)
              + p.vol_spread_k * view.vol
              + max(widen_bps, 0.0) * 1e-4 * view.wmid)

    bid = _floor_tick(centre - spread / 2, m.tick)
    ask = _ceil_tick(centre + spread / 2, m.tick)
    # post-only: never cross. Clamping only ever moves a quote away from the other side.
    bid = min(bid, _floor_tick(view.best_ask - m.tick, m.tick))
    ask = max(ask, _ceil_tick(view.best_bid + m.tick, m.tick))
    if ask - bid < min_spread(cfg, view.wmid) - _EPS:  # rounding can't shrink it, but be explicit
        ask = _ceil_tick(bid + min_spread(cfg, view.wmid), m.tick)

    fill_frac = min(abs(inventory) / p.max_inventory, 1.0) if p.max_inventory > 0 else 1.0
    adding = p.base_size * (1.0 - fill_frac)
    bid_size = _lots(adding if inventory >= 0 else p.base_size, m.lot)
    ask_size = _lots(adding if inventory <= 0 else p.base_size, m.lot)
    return (bid if bid_size > 0 else None, bid_size,
            ask if ask_size > 0 else None, ask_size, centre, spread)
