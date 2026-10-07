"""The morning brief. Every session starts with three lines:

    feel: how the market feels today
    max_loss_usd: the most we can lose today
    stop: the one event that makes us stop

If any of the three is missing, we do not trade today. The jev runner refuses to
start in paper or live mode without today's brief, and the max loss can't be
looser than the guard's daily limit.

Prevents: D10 (no unexamined sessions), D5 (the stop event is decided before
the cascade, not during it).

    python -m desk.brief suggest           # draft line 1 + 2 from tape and guard.toml
    python -m desk.brief write --feel "..." --max-loss 10 --stop "..."
    python -m desk.brief check
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .config import DeskConfig

LINES = ("feel", "max_loss_usd", "stop")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def path_for(cfg: DeskConfig, day: str | None = None) -> Path:
    return cfg.paths.briefs_dir / f"{day or today()}.md"


def parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(feel|max_loss_usd|stop)\s*:\s*(.*\S)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check(cfg: DeskConfig, guard_max_loss: float | None = None, day: str | None = None) -> dict[str, str]:
    """Returns the brief, or raises SystemExit('we do not trade today: ...')."""
    p = path_for(cfg, day)
    if not p.exists():
        raise SystemExit(f"we do not trade today: no brief at {p}")
    b = parse(p.read_text())
    missing = [k for k in LINES if not b.get(k)]
    if missing:
        raise SystemExit(f"we do not trade today: brief missing {', '.join(missing)}")
    try:
        loss = float(b["max_loss_usd"].lstrip("$").replace(",", ""))
    except ValueError:
        raise SystemExit("we do not trade today: max_loss_usd is not a number")
    if loss <= 0:
        raise SystemExit("we do not trade today: max_loss_usd must be > 0")
    if guard_max_loss is not None and loss > guard_max_loss:
        raise SystemExit(f"we do not trade today: brief allows ${loss:g}, the guard allows ${guard_max_loss:g}")
    return b


def write(cfg: DeskConfig, feel: str, max_loss: float, stop: str) -> Path:
    p = path_for(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"feel: {feel}\nmax_loss_usd: {max_loss:g}\nstop: {stop}\n")
    return p


def suggest(cfg: DeskConfig) -> str:
    """Draft from the last 24h of tape + guard.toml. The stop line is left for a person."""
    import time

    from . import tape
    from .config import NS
    from .guard import load_limits
    from .jev import mood
    from .replay import replay, DoNothing

    feel = "unknown (no tape)"
    try:
        now = time.time_ns()
        ev = tape.read_range(cfg.paths.tape_db, now - 86400 * NS, now)
        if ev:
            r = replay(ev, cfg, DoNothing())
            import numpy as np
            grid = np.arange(r.mid_ts[0], r.mid_ts[-1], NS)
            v = float(np.nanstd(np.diff(r.mid_at(grid))))
            feel = f"{mood(v, cfg)} — 1s vol ${v:.2f}, last 24h range ${r.mid_px.min():,.1f}-{r.mid_px.max():,.1f}"
    except Exception as e:  # suggestion only; never blocks writing the brief by hand
        feel = f"unknown ({type(e).__name__})"
    try:
        loss = f"{load_limits(cfg.paths.guard_config).max_loss_per_day_usd:g}"
    except Exception:
        loss = ""
    return f"feel: {feel}\nmax_loss_usd: {loss}\nstop: \n"


def main() -> None:
    import argparse

    from . import config

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("suggest")
    sp.add_parser("check")
    w = sp.add_parser("write")
    w.add_argument("--feel", required=True)
    w.add_argument("--max-loss", type=float, required=True)
    w.add_argument("--stop", required=True)
    a = ap.parse_args()
    cfg = config.load(a.config)
    if a.cmd == "suggest":
        print(suggest(cfg), end="")
    elif a.cmd == "write":
        print(write(cfg, a.feel, a.max_loss, a.stop))
    else:
        print("\n".join(f"{k}: {v}" for k, v in check(cfg).items()))


if __name__ == "__main__":
    main()
