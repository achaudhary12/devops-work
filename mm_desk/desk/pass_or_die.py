"""Pass or die. A version survives only if, on tape it has never seen:

  1. the 10s report card is positive after fees
  2. profit per 1000 quotes beats a dumb symmetric quoter and beats doing nothing
  3. max drawdown under 5% of capital
  4. no single day loses more than 1% of capital
  5. inventory returns to flat within 90s on average
  6. 1 and 2 hold in calm days and violent days separately

Every statistic is reported with a bootstrap confidence interval (minute blocks,
so autocorrelated fills don't fake precision). Fail any line and the version dies.
No violent days in the test set = line 6 is unproven = dies.

Prevents: D1 (markouts), D2 (flat time), D3 (after fees, beat baselines),
D5 (violent days), D7 (unseen tape only; replay raises on fit overlap).

    python -m desk.pass_or_die --tape var/tape.duckdb --from 2026-09-20 --to 2026-10-06
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from .config import NS, DeskConfig
from .events import Event
from .jev import Jev
from .replay import ReplayResult, SymmetricQuoter, replay
from .report_card import fills_frame

MINUTE = 60 * NS


@dataclass
class Line:
    name: str
    ok: bool
    value: str
    ci: str = ""


@dataclass
class Verdict:
    lines: list[Line] = field(default_factory=list)

    @property
    def survives(self) -> bool:
        return bool(self.lines) and all(l.ok for l in self.lines)

    def render(self) -> str:
        w = max(len(l.name) for l in self.lines)
        rows = [f"{'PASS' if l.ok else 'FAIL'}  {l.name:<{w}}  {l.value:<28} {l.ci}" for l in self.lines]
        rows.append("SURVIVES" if self.survives else "DIES")
        return "\n".join(rows)


@dataclass
class DayRun:
    day: str
    jev: ReplayResult
    sym: ReplayResult
    violent: bool


def _boot_mean(x: np.ndarray, blocks: np.ndarray, iters: int, rng) -> tuple[float, float, float]:
    """Block bootstrap of the mean: resample whole minutes."""
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    ub, inv = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inv, weights=x, minlength=len(ub))
    cnts = np.bincount(inv, minlength=len(ub)).astype(float)
    draws = rng.integers(0, len(ub), size=(iters, len(ub)))
    means = sums[draws].sum(1) / np.maximum(cnts[draws].sum(1), 1)
    return float(x.mean()), float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _per_1000(res: Sequence[ReplayResult], iters: int, rng, unit: int) -> tuple[float, float, float]:
    """PnL per `unit` quotes with a minute-block bootstrap CI."""
    pnl_m, q_m = [], []
    for r in res:
        if len(r.equity) == 0:
            continue
        mins = (r.equity_ts - r.start_ts) // MINUTE
        eq_end = np.array([r.equity[mins == m][-1] for m in np.unique(mins)])
        pnl_m.extend(np.diff(np.concatenate([[0.0], eq_end])))
        qm = np.bincount((np.asarray(r.place_ts, np.int64) - r.start_ts) // MINUTE,
                         minlength=len(eq_end))[:len(eq_end)]
        q_m.extend(qm)
    pnl_m, q_m = np.asarray(pnl_m), np.asarray(q_m, float)
    if q_m.sum() == 0:
        return 0.0, 0.0, 0.0
    point = pnl_m.sum() / q_m.sum() * unit
    draws = rng.integers(0, len(pnl_m), size=(iters, len(pnl_m)))
    boots = pnl_m[draws].sum(1) / np.maximum(q_m[draws].sum(1), 1) * unit
    return float(point), float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))


def _markouts(res: Sequence[ReplayResult], cfg: DeskConfig, iters: int, rng):
    xs, bl = [], []
    for i, r in enumerate(res):
        df = fills_frame(r, cfg)
        if df.is_empty():
            continue
        mo = df["mo_10s"].to_numpy()
        ok = ~np.isnan(mo)
        xs.append(mo[ok])
        bl.append(df["ts"].to_numpy()[ok] // MINUTE + i * 10**9)
    if not xs:
        return float("nan"), float("nan"), float("nan"), 0
    x, b = np.concatenate(xs), np.concatenate(bl)
    return (*_boot_mean(x, b, iters, rng), len(x))


def max_drawdown(res: Sequence[ReplayResult]) -> float:
    curve, base = [], 0.0
    for r in res:
        curve.append(base + r.equity)
        base += r.pnl
    if not curve:
        return 0.0
    c = np.concatenate([[0.0], *curve])
    return float(np.max(np.maximum.accumulate(c) - c))


def flat_return_s(res: Sequence[ReplayResult], lot: float) -> tuple[float, int]:
    """Mean seconds from leaving flat to being flat again (open episodes count to tape end)."""
    durs = []
    for r in res:
        start = None
        for t, inv in r.inv_series:
            flat = abs(inv) < lot / 2
            if start is None and not flat:
                start = t
            elif start is not None and flat:
                durs.append((t - start) / NS)
                start = None
        if start is not None:
            durs.append((r.end_ts - start) / NS)
    return (float(np.mean(durs)) if durs else 0.0), len(durs)


def day_is_violent(r: ReplayResult, cfg: DeskConfig) -> bool:
    if len(r.mid_ts) < 3:
        return False
    grid = np.arange(r.mid_ts[0], r.mid_ts[-1], NS)
    m = r.mid_at(grid)
    return float(np.nanstd(np.diff(m))) > cfg.signals.violent_vol


def run_days(days: Iterable[tuple[str, list[Event], int]], cfg: DeskConfig,
             widen: dict[str, float] | None = None) -> list[DayRun]:
    """days: (label, events, trade_from_ns) — see tape.iter_days."""
    out = []
    for day, ev, t0 in days:
        j = replay(ev, cfg, Jev(cfg, widen), trade_from_ns=t0)
        s = replay(ev, cfg, SymmetricQuoter(cfg), trade_from_ns=t0)
        out.append(DayRun(day, j, s, day_is_violent(j, cfg)))
    return out


def judge(runs: list[DayRun], cfg: DeskConfig, seed: int = 0) -> Verdict:
    g, rng, it = cfg.gates, np.random.default_rng(seed), cfg.gates.bootstrap_iters
    v = Verdict()
    if not runs:
        v.lines.append(Line("tape", False, "no unseen tape"))
        return v
    J = [r.jev for r in runs]
    S = [r.sym for r in runs]

    def edge_lines(tag: str, js, ss) -> None:
        mean, lo, hi, n = _markouts(js, cfg, it, rng)
        ok = (lo > 0) if g.require_ci_above_zero else (mean > 0)
        v.lines.append(Line(f"{tag}10s report card > 0 after fees", bool(ok and n > 0),
                            f"{mean:+.3f} bps (n={n})", f"95% CI [{lo:+.3f}, {hi:+.3f}]"))
        pj, pjl, pjh = _per_1000(js, it, rng, g.quotes_per_unit)
        ps, _, _ = _per_1000(ss, it, rng, g.quotes_per_unit)
        lo_or_pt = pjl if g.require_ci_above_zero else pj
        v.lines.append(Line(f"{tag}$/{g.quotes_per_unit} quotes > symmetric & > 0",
                            bool(lo_or_pt > max(ps, 0.0)),
                            f"jev {pj:+.3f} vs sym {ps:+.3f}", f"95% CI [{pjl:+.3f}, {pjh:+.3f}]"))

    edge_lines("", J, S)

    dd = max_drawdown(J)
    v.lines.append(Line(f"max drawdown < {g.max_drawdown_frac:.0%} capital",
                        dd < g.max_drawdown_frac * g.capital_usd, f"${dd:,.2f}",
                        f"limit ${g.max_drawdown_frac * g.capital_usd:,.0f}"))
    worst = min(r.jev.pnl for r in runs)
    worst_day = min(runs, key=lambda r: r.jev.pnl).day
    v.lines.append(Line(f"no day loses > {g.max_day_loss_frac:.0%} capital",
                        worst > -g.max_day_loss_frac * g.capital_usd,
                        f"worst {worst_day} ${worst:,.2f}",
                        f"limit -${g.max_day_loss_frac * g.capital_usd:,.0f}"))
    fr, n = flat_return_s(J, cfg.market.lot)
    v.lines.append(Line(f"back to flat < {g.max_flat_return_s:.0f}s avg",
                        fr < g.max_flat_return_s, f"{fr:.1f}s over {n} episodes"))

    for tag, flag in (("calm: ", False), ("violent: ", True)):
        sub = [r for r in runs if r.violent == flag]
        if not sub:
            v.lines.append(Line(f"{tag}days tested", False, "0 days — unproven"))
            continue
        edge_lines(tag, [r.jev for r in sub], [r.sym for r in sub])
    return v


def main() -> None:
    import argparse
    import sys

    from . import config, tape
    from .jev import load_widen

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--tape")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    a = ap.parse_args()
    cfg = config.load(a.config)
    days = tape.iter_days(a.tape or cfg.paths.tape_db, a.start, a.end)
    verdict = judge(run_days(days, cfg, load_widen(cfg.paths.widen_file)), cfg)
    print(verdict.render())
    sys.exit(0 if verdict.survives else 1)


if __name__ == "__main__":
    main()
