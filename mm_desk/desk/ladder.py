"""The ladder. Shadow 7 days. Paper 7 days. Real money at $100 max inventory.
Double only after 500 fills with a positive report card after fees. Any losing
week drops us one rung.

Only a human says go live. The ladder will not step onto a real-money rung unless
var/GO_LIVE exists, and the code never writes that file. It looks like:

    approved_by = "your name"
    approved_at = "2026-10-07"
    max_inventory_usd = 400      # highest rung the ladder may double up to

Prevents: D10 (no self-promotion to real money), D1-D3 (size grows only on
proven positive report cards; a losing week shrinks it).

    python -m desk.ladder status
    python -m desk.ladder week --pnl -12.5 --scored-fills 640 --markout-bps 0.4
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import DeskConfig


def rung_name(i: int, first_usd: float) -> str:
    return ("shadow", "paper")[i] if i < 2 else f"live_{first_usd * 2 ** (i - 2):g}usd"


def rung_inventory_usd(i: int, first_usd: float) -> float:
    return 0.0 if i < 2 else first_usd * 2 ** (i - 2)


@dataclass
class LadderState:
    rung: int = 0
    since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    good_fills: int = 0                    # positive-report-card fills since last rung change
    history: list[dict] = field(default_factory=list)


class Ladder:
    def __init__(self, cfg: DeskConfig) -> None:
        self.cfg = cfg
        self.path = cfg.paths.ladder_file
        self.state = self._load()

    def _load(self) -> LadderState:
        try:
            return LadderState(**json.loads(self.path.read_text()))
        except FileNotFoundError:
            return LadderState()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.state), indent=1))

    @property
    def name(self) -> str:
        return rung_name(self.state.rung, self.cfg.ladder.first_live_inventory_usd)

    @property
    def inventory_usd(self) -> float:
        return rung_inventory_usd(self.state.rung, self.cfg.ladder.first_live_inventory_usd)

    def go_live(self) -> dict | None:
        p = self.cfg.paths.go_live_file
        if not p.exists():
            return None
        d = tomllib.loads(p.read_text())
        if not d.get("approved_by") or not d.get("approved_at") or "max_inventory_usd" not in d:
            raise ValueError(f"{p} must have approved_by, approved_at, max_inventory_usd")
        return d

    def days_on_rung(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - datetime.fromisoformat(self.state.since)).total_seconds() / 86400

    def _move(self, to: int, why: str) -> None:
        self.state.history.append({"at": datetime.now(timezone.utc).isoformat(),
                                   "from": self.name, "to": rung_name(to, self.cfg.ladder.first_live_inventory_usd),
                                   "why": why})
        self.state.rung = to
        self.state.since = datetime.now(timezone.utc).isoformat()
        self.state.good_fills = 0
        self.save()

    def week(self, pnl: float, scored_fills: int, markout_bps: float,
             shadow_ok: bool = True, now: datetime | None = None) -> str:
        """Feed one week's result. Returns what happened."""
        L, s = self.cfg.ladder, self.state
        if pnl < 0 and s.rung > 0:
            self._move(s.rung - 1, f"losing week ({pnl:+.2f})")
            return f"dropped to {self.name}"
        if markout_bps > 0:
            s.good_fills += scored_fills
        days = self.days_on_rung(now)
        if s.rung == 0 and days >= L.shadow_days and shadow_ok:
            self._move(1, "7 days shadow, shadow within tolerance of replay")
        elif s.rung == 1 and days >= L.paper_days:
            gl = self.go_live()
            if gl is None:
                self.save()
                return "paper complete — waiting for a human to write GO_LIVE"
            self._move(2, f"GO_LIVE by {gl['approved_by']}")
        elif s.rung >= 2 and s.good_fills >= L.fills_to_double:
            gl = self.go_live()
            nxt = rung_inventory_usd(s.rung + 1, L.first_live_inventory_usd)
            if gl is None or nxt > float(gl["max_inventory_usd"]):
                self.save()
                return f"earned a double to ${nxt:g} but GO_LIVE caps at ${gl and gl['max_inventory_usd']}"
            self._move(s.rung + 1, f"{s.good_fills} positive fills")
        else:
            self.save()
        return f"on {self.name}"

    def require_live(self, limits=None, mark: float | None = None) -> None:
        """Called by the guard before it will start with real money. Raises if not allowed."""
        if self.state.rung < 2:
            raise SystemExit(f"ladder is on '{self.name}': no real money")
        gl = self.go_live()
        if gl is None:
            raise SystemExit("no GO_LIVE file: only a human can say go live")
        if self.inventory_usd > float(gl["max_inventory_usd"]):
            raise SystemExit("ladder rung exceeds GO_LIVE max_inventory_usd")
        if limits is not None and mark:
            cap_usd = limits.max_inventory_btc * mark
            if cap_usd > self.inventory_usd * 1.05:
                raise SystemExit(f"guard.toml max_inventory (${cap_usd:,.0f}) exceeds rung (${self.inventory_usd:,.0f})")


def main() -> None:
    import argparse

    from . import config

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    w = sp.add_parser("week")
    w.add_argument("--pnl", type=float, required=True)
    w.add_argument("--scored-fills", type=int, required=True)
    w.add_argument("--markout-bps", type=float, required=True)
    w.add_argument("--shadow-ok", action=argparse.BooleanOptionalAction, default=True)
    a = ap.parse_args()
    lad = Ladder(config.load(a.config))
    if a.cmd == "status":
        print(f"rung: {lad.name}  (${lad.inventory_usd:g} max inventory)  "
              f"days: {lad.days_on_rung():.1f}  good fills: {lad.state.good_fills}")
    else:
        print(lad.week(a.pnl, a.scored_fills, a.markout_bps, a.shadow_ok))


if __name__ == "__main__":
    main()
