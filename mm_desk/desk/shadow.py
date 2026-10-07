"""Shadow vs replay. Shadow results must land within 30% of replay or replay is lying.

Take a shadow session's summary (live feed, wall-clock decisions) and replay the
recorded tape for the same window with the same config. Compare fill rate and
PnL per 1000 quotes. A gap bigger than the tolerance means the replay's fill
model, latency or tape is wrong — and every pass_or_die verdict built on it is void.

Prevents: D7 (a backtest that flatters us).

    python -m desk.shadow var/shadow/2026-10-07.summary.json
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import DeskConfig
from .replay import ReplayResult


@dataclass
class ShadowCheck:
    metric: str
    shadow: float
    replay: float
    ok: bool


def _close(a: float, b: float, tol: float, floor: float) -> bool:
    return abs(a - b) <= tol * max(abs(b), floor)


def compare(shadow: dict, rep: ReplayResult, cfg: DeskConfig) -> list[ShadowCheck]:
    tol, unit = cfg.gates.shadow_tolerance, cfg.gates.quotes_per_unit
    sq, rq = max(shadow["places"], 1), max(rep.places, 1)
    pairs = [
        ("quotes", shadow["places"], rep.places, 10.0),
        (f"fills per {unit} quotes", shadow["fills"] / sq * unit, len(rep.fills) / rq * unit, 1.0),
        (f"$ per {unit} quotes", shadow["pnl"] / sq * unit, rep.pnl / rq * unit, 0.5),
    ]
    return [ShadowCheck(n, s, r, _close(s, r, tol, fl)) for n, s, r, fl in pairs]


def verdict(checks: list[ShadowCheck]) -> str:
    rows = [f"{'ok ' if c.ok else 'BAD'}  {c.metric:<24} shadow {c.shadow:>12.3f}  replay {c.replay:>12.3f}"
            for c in checks]
    rows.append("replay agrees with shadow" if all(c.ok for c in checks) else "REPLAY IS LYING")
    return "\n".join(rows)


def main() -> None:
    import argparse
    import json
    import sys

    from . import config, tape
    from .jev import Jev, load_widen
    from .replay import replay

    ap = argparse.ArgumentParser()
    ap.add_argument("summary")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = config.load(a.config)
    s = json.loads(open(a.summary).read())
    ev = tape.read_range(cfg.paths.tape_db, s["start_ns"], s["end_ns"])
    rep = replay(ev, cfg, Jev(cfg, load_widen(cfg.paths.widen_file)), trade_from_ns=s["start_ns"])
    checks = compare(s, rep, cfg)
    print(verdict(checks))
    sys.exit(0 if all(c.ok for c in checks) else 1)


if __name__ == "__main__":
    main()
