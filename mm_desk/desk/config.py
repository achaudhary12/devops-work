"""Every tunable number on the desk, in one place.

Bracketed values in the brief ([60%], [250ms], ...) live here with the same
names. Leash limits are NOT here: they live in guard.toml, read only by the
guard process (see guard.py).

Prevents: D7 (no magic numbers scattered across research vs live code paths).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

NS = 1_000_000_000
MS = 1_000_000


@dataclass(frozen=True)
class Market:
    symbol: str = "BTC-PERP"
    tick: float = 0.1
    lot: float = 0.001                  # minimum size step, BTC
    maker_fee: float = 0.0002           # fraction of notional; negative = rebate. SET YOUR REAL TIER:
                                        # at 2bp the fee+tick floor is ~$24 at $60k and the desk never trades (D3)
    taker_fee: float = 0.0005           # only ever paid by the guard's flatten
    funding_interval_s: int = 8 * 3600


@dataclass(frozen=True)
class Signals:
    book_levels: int = 10               # what Jev sees
    pressure_levels: int = 1            # levels summed into bid_size/ask_size for pressure
    flow_window_s: float = 2.0          # "who is hitting" window
    vol_window_s: float = 60.0          # realized vol window
    vol_sample_s: float = 1.0           # mid sampled every second for vol
    flow_norm_halflife_s: float = 600.0 # what "normal" aggressive volume means
    violent_vol: float = 15.0           # $/sqrt(s); above this the market mood is "violent"
    size_buckets: tuple[float, ...] = (0.005, 0.02)  # BTC edges: small | medium | large


@dataclass(frozen=True)
class Pricing:
    pressure_coef: float = 0.0          # $ per unit pressure; fitted on past tape (fit.py)
    pressure_fit_end_ns: int = 0        # last tape timestamp used to fit pressure_coef
    risk: float = 0.5                   # inventory aversion (gamma)
    vol_spread_k: float = 0.2           # extra full spread per $ of 1s vol
    base_size: float = 0.01             # BTC per side when flat
    max_inventory: float = 0.05         # BTC; quoting side shrinks to 0 here (guard has its own cap)
    requote_ticks: int = 1              # min price move before we cancel/replace
    requote_size_frac: float = 0.25     # min relative size change before we replace


@dataclass(frozen=True)
class Pull:
    book_drop_frac: float = 0.60        # one side loses 60% of size ...
    book_drop_window_s: float = 1.0     # ... in under 1s
    flow_mult: float = 4.0              # aggressive volume 4x normal
    flow_warmup_s: float = 120.0        # don't judge "normal" before this much tape
    liq_notional_usd: float | None = None  # [$X] — MUST be set before live; None = pull on every liquidation
    max_feed_lag_ms: float = 250.0
    funding_guard_s: float = 120.0
    calm_s: float = 20.0                # back only after 20s of normal market


@dataclass(frozen=True)
class Latency:
    feed_ms: float = 30.0               # exchange -> us; replace with measure_feed_delay(tape)
    order_ms: float = 40.0              # us -> exchange matching engine; measured by guard RTT/2


@dataclass(frozen=True)
class Gates:
    capital_usd: float = 10_000.0
    max_drawdown_frac: float = 0.05
    max_day_loss_frac: float = 0.01
    max_flat_return_s: float = 90.0
    quotes_per_unit: int = 1000
    require_ci_above_zero: bool = True  # 10s markout CI lower bound must be > 0, not just the mean
    shadow_tolerance: float = 0.30
    bootstrap_iters: int = 2000


@dataclass(frozen=True)
class Ladder:
    shadow_days: int = 7
    paper_days: int = 7
    first_live_inventory_usd: float = 100.0
    fills_to_double: int = 500


@dataclass(frozen=True)
class Paths:
    root: Path = Path(os.environ.get("DESK_HOME", "./var"))

    @property
    def tape_db(self) -> Path: return self.root / "tape.duckdb"
    @property
    def bus_dir(self) -> Path: return self.root / "bus"
    @property
    def leash_log(self) -> Path: return self.root / "leash.log"
    @property
    def widen_file(self) -> Path: return self.root / "widen.json"
    @property
    def ladder_file(self) -> Path: return self.root / "ladder.json"
    @property
    def go_live_file(self) -> Path: return self.root / "GO_LIVE"
    @property
    def briefs_dir(self) -> Path: return self.root / "briefs"
    @property
    def state_file(self) -> Path: return self.root / "state.json"
    @property
    def guard_config(self) -> Path: return self.root / "guard.toml"


@dataclass(frozen=True)
class DeskConfig:
    market: Market = field(default_factory=Market)
    signals: Signals = field(default_factory=Signals)
    pricing: Pricing = field(default_factory=Pricing)
    pull: Pull = field(default_factory=Pull)
    latency: Latency = field(default_factory=Latency)
    gates: Gates = field(default_factory=Gates)
    ladder: Ladder = field(default_factory=Ladder)
    paths: Paths = field(default_factory=Paths)

    def with_(self, section: str, **kw) -> "DeskConfig":
        return replace(self, **{section: replace(getattr(self, section), **kw)})


def load(path: str | Path | None = None) -> DeskConfig:
    """Load desk.toml (optional) over defaults. Unknown keys are an error."""
    import tomllib

    cfg = DeskConfig()
    if path is None:
        return cfg
    data = tomllib.loads(Path(path).read_text())
    for section, values in data.items():
        cur = getattr(cfg, section)
        known = {f.name for f in fields(cur)}
        bad = set(values) - known
        if bad:
            raise ValueError(f"unknown keys in [{section}]: {sorted(bad)}")
        if section == "paths":
            values = {k: Path(v) for k, v in values.items()}
        cfg = replace(cfg, **{section: replace(cur, **values)})
    return cfg
