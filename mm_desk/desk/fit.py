"""Fit the book-pressure coefficient on past tape only.

    future weighted-mid change over `horizon_s`  ~  beta * pressure_now

The fit records the last timestamp it touched (including the horizon it looked
ahead). replay() refuses to run on tape starting at or before that timestamp.

Prevents: D1 (centre leans where the book is leaning), D7 (fit/test overlap).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .config import NS, DeskConfig
from .events import Event
from .signals import SignalState


@dataclass(frozen=True)
class PressureFit:
    coef: float          # $ per unit pressure
    t_stat: float
    r2: float
    n: int
    fit_end_ns: int      # last tape timestamp the fit used (inclusive of look-ahead)

    def apply(self, cfg: DeskConfig) -> DeskConfig:
        return cfg.with_("pricing", pressure_coef=self.coef, pressure_fit_end_ns=self.fit_end_ns)


def fit_pressure(tape: Iterable[Event], cfg: DeskConfig, horizon_s: float = 10.0,
                 sample_s: float = 1.0) -> PressureFit:
    st = SignalState(cfg.signals)
    ts, mids, prs = [], [], []
    next_t = None
    last_ts = 0
    for ev in tape:
        if next_t is None:
            next_t = ev.recv_ts
        while ev.recv_ts >= next_t:
            v = st.view()
            if v is not None:
                ts.append(next_t)
                mids.append(v.wmid)
                prs.append(v.pressure)
            next_t += int(sample_s * NS)
        st.update(ev)
        last_ts = ev.recv_ts
    t = np.asarray(ts, np.int64)
    m = np.asarray(mids)
    x = np.asarray(prs)
    j = np.searchsorted(t, t + int(horizon_s * NS))
    ok = j < len(t)
    y = m[j[ok]] - m[ok]
    x = x[ok]
    if len(x) < 30 or not np.any(x):
        return PressureFit(0.0, 0.0, 0.0, int(len(x)), last_ts)
    beta = float(x @ y / (x @ x))
    resid = y - beta * x
    se = float(np.sqrt(resid @ resid / (len(x) - 1) / (x @ x)))
    r2 = 1 - float(resid @ resid) / float((y - y.mean()) @ (y - y.mean()) or 1.0)
    return PressureFit(beta, beta / se if se > 0 else 0.0, r2, int(len(x)), last_ts)


def main() -> None:
    import argparse

    from . import config, tape

    ap = argparse.ArgumentParser(description="Fit pressure_coef on [from, to]; test only on later days.")
    ap.add_argument("--config")
    ap.add_argument("--tape")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--horizon-s", type=float, default=10.0)
    a = ap.parse_args()
    cfg = config.load(a.config)
    lo, _ = tape._day_bounds(a.start)
    _, hi = tape._day_bounds(a.end)
    f = fit_pressure(tape.read_range(a.tape or cfg.paths.tape_db, lo, hi, warmup_s=0), cfg, a.horizon_s)
    print(f"# n={f.n} t={f.t_stat:.2f} r2={f.r2:.4f} — paste into desk.toml; test only on days after {a.end}")
    print(f"[pricing]\npressure_coef = {f.coef:.6f}\npressure_fit_end_ns = {f.fit_end_ns}")


if __name__ == "__main__":
    main()
