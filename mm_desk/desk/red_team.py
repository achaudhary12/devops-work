"""Red team. Every Sunday, Opus plays the predator.

Each attack rewrites a base tape (recorded or synthetic) with order flow designed
to pick off our quotes, then Jev is replayed on the clean and attacked tapes.
If the attack makes money — Jev's PnL over the attack window is worse on the
attacked tape by more than `min_loss_usd` — it becomes a new rule in deaths.md.

Attacks:
  spoof_wall  fake size stacked on one side (pressure says "up"), pulled, then a
              sweep the other way and the price stays there
  sweep       sudden one-way aggressive sweep through N levels, permanent impact
  slow_bleed  a patient seller: small sells every few seconds, a tick lower each
              time, for minutes

New attacks: write a function (events, t0, rng) -> events and add it to ATTACKS.

Prevents: D1 (adverse selection we haven't imagined yet), D5 (cascades).

    python -m desk.red_team --synth-seed 7          # weekly run on synthetic base tape
    python -m desk.red_team --tape var/tape.duckdb --day 2026-10-04
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from .config import NS, DeskConfig
from .events import BookUpdate, Event, Funding, Liquidation, Trade
from .jev import Jev
from .replay import replay

Attack = Callable[[list[Event], int, DeskConfig, np.random.Generator], tuple[list[Event], int, int, dict]]


def _shift(e: Event, delta: float) -> Event:
    if delta == 0:
        return e
    if isinstance(e, BookUpdate):
        return replace(e, bids=tuple((round(p + delta, 10), s) for p, s in e.bids),
                       asks=tuple((round(p + delta, 10), s) for p, s in e.asks))
    if isinstance(e, (Trade, Liquidation)):
        return replace(e, price=round(e.price + delta, 10))
    if isinstance(e, Funding):
        return replace(e, mark=e.mark + delta)
    return e


def _shift_after(events: list[Event], t: int, delta: float) -> list[Event]:
    """Permanent impact: every price after t moves by delta (the predator's profit)."""
    return [e if e.recv_ts <= t else _shift(e, delta) for e in events]


def _touch_at(events: list[Event], t: int) -> tuple[float, float]:
    bb = ba = None
    for e in events:
        if e.recv_ts > t:
            break
        if isinstance(e, BookUpdate) and e.bids and e.asks:
            bb, ba = max(p for p, _ in e.bids), min(p for p, _ in e.asks)
    return bb, ba


def _sweep(t: int, start_px: float, levels: int, side: int, tick: float, size: float) -> list[Trade]:
    """side=-1: sells hitting bids downward. recv==exch: the attacker is co-located, we are not."""
    return [Trade(t + i, t + i, -1, round(start_px + side * i * tick, 10), size, side)
            for i in range(levels)]


def spoof_wall(ev, t0, cfg, rng, wall_btc=25.0, hold_s=8.0, levels=12):
    tick, t1 = cfg.market.tick, t0 + int(hold_s * NS)
    side = 1 if rng.random() < 0.5 else -1          # fake wall on `side`, real flow the other way
    out = []
    for e in ev:
        if isinstance(e, BookUpdate) and t0 <= e.recv_ts < t1:
            lv = list(e.bids if side > 0 else e.asks)
            lv = [(p, s + (wall_btc if k < 3 else 0)) for k, (p, s) in enumerate(lv)]
            e = replace(e, bids=tuple(lv)) if side > 0 else replace(e, asks=tuple(lv))
        out.append(e)
    bb, ba = _touch_at(out, t1)
    start = bb if side > 0 else ba                  # fake bids, then sell into the real ones (and vice versa)
    out += _sweep(t1, start, levels, -side, tick, 0.5)
    out.sort(key=lambda e: e.recv_ts)
    return _shift_after(out, t1, -side * levels * tick), t0, t1 + 60 * NS, {"side": side, "wall_btc": wall_btc}


def sweep(ev, t0, cfg, rng, levels=30):
    tick = cfg.market.tick
    side = 1 if rng.random() < 0.5 else -1
    bb, ba = _touch_at(ev, t0)
    out = sorted(ev + _sweep(t0, ba if side > 0 else bb, levels, side, tick, 1.0), key=lambda e: e.recv_ts)
    return _shift_after(out, t0, side * levels * tick), t0 - 5 * NS, t0 + 60 * NS, {"side": side, "levels": levels}


def slow_bleed(ev, t0, cfg, rng, minutes=10, every_s=3.0):
    tick = cfg.market.tick
    side = 1 if rng.random() < 0.5 else -1
    times = [t0 + int(k * every_s * NS) for k in range(int(minutes * 60 / every_s))]
    # each little trade leaves the price one tick further along, for good
    out = [_shift(e, side * tick * bisect_left(times, e.recv_ts)) for e in ev]
    trades, k, touch = [], 0, None
    for e in out:
        while k < len(times) and e.recv_ts > times[k]:
            if touch:
                trades.append(Trade(times[k], times[k], -1, touch[1] if side > 0 else touch[0], 0.05, side))
            k += 1
        if isinstance(e, BookUpdate) and e.bids and e.asks:
            touch = (max(p for p, _ in e.bids), min(p for p, _ in e.asks))
    out = sorted(out + trades, key=lambda e: e.recv_ts)
    return out, t0, t0 + int(minutes * 60 * NS) + 60 * NS, {"side": side, "minutes": minutes}


ATTACKS: dict[str, Attack] = {"spoof_wall": spoof_wall, "sweep": sweep, "slow_bleed": slow_bleed}


@dataclass
class AttackResult:
    name: str
    params: dict
    clean_pnl: float
    attacked_pnl: float
    window: tuple[int, int]

    @property
    def attacker_profit(self) -> float:
        return self.clean_pnl - self.attacked_pnl


def _window_pnl(res, a: int, b: int) -> float:
    if len(res.equity) == 0:
        return 0.0
    i = np.searchsorted(res.equity_ts, [a, b], side="right") - 1
    i = np.clip(i, 0, len(res.equity) - 1)
    return float(res.equity[i[1]] - res.equity[i[0]])


def run(base: list[Event], cfg: DeskConfig, seed: int = 0, attacks: dict[str, Attack] | None = None,
        widen: dict | None = None) -> list[AttackResult]:
    rng = np.random.default_rng(seed)
    t_lo, t_hi = base[0].recv_ts, base[-1].recv_ts
    clean = replay(base, cfg, Jev(cfg, widen))
    out = []
    for name, atk in (attacks or ATTACKS).items():
        # attack somewhere after warm-up, leaving room for the markout window
        t0 = int(rng.uniform(t_lo + (cfg.pull.flow_warmup_s + 60) * NS, t_hi - 15 * 60 * NS))
        ev, a, b, params = atk(list(base), t0, cfg, rng)
        hit = replay(ev, cfg, Jev(cfg, widen))
        out.append(AttackResult(name, params, _window_pnl(clean, a, b), _window_pnl(hit, a, b), (a, b)))
    return out


def append_rules(deaths: Path, results: list[AttackResult], min_loss_usd: float) -> list[str]:
    day = datetime.now(timezone.utc).date().isoformat()
    lines = []
    for r in results:
        if r.attacker_profit > min_loss_usd:
            lines.append(
                f"- **RT-{day}-{r.name}** — attack made ${r.attacker_profit:,.2f} off Jev in replay "
                f"(params {r.params}, window {r.window[0]}..{r.window[1]}). "
                f"Rule: _todo — name the signal Jev missed, add a pull or pricing change, keep this "
                f"attack as a regression tape until it no longer pays._")
    if lines:
        with deaths.open("a") as f:
            f.write("\n".join(lines) + "\n")
    return lines


def main() -> None:
    import argparse

    from . import config, tape
    from .jev import load_widen
    from .synth import synth_tape

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--tape")
    ap.add_argument("--day")
    ap.add_argument("--synth-seed", type=int, default=0)
    ap.add_argument("--min-loss", type=float, default=1.0, help="$ an attack must take to count")
    ap.add_argument("--deaths", default=str(Path(__file__).resolve().parent.parent / "deaths.md"))
    a = ap.parse_args()
    cfg = config.load(a.config)
    base = tape.read_day(a.tape or cfg.paths.tape_db, a.day) if a.day else synth_tape(3600, seed=a.synth_seed)
    res = run(base, cfg, seed=a.synth_seed, widen=load_widen(cfg.paths.widen_file))
    for r in res:
        print(f"{r.name:<12} clean {r.clean_pnl:+9.2f}  attacked {r.attacked_pnl:+9.2f}  "
              f"attacker {r.attacker_profit:+9.2f}  {r.params}")
    new = append_rules(Path(a.deaths), res, a.min_loss)
    print(f"{len(new)} new rule(s) appended to {a.deaths}" if new else "no attack paid — deaths.md unchanged")


if __name__ == "__main__":
    main()
