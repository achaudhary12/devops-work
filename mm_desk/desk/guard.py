"""The guard: the only process with exchange keys, and the leash nobody can loosen.

Limits are read once at start from guard.toml (not desk.toml, not the bus, not a
prompt) and frozen:
    max inventory, max loss per day, max orders per second, max order size,
    max price deviation from mark, Jev heartbeat timeout, feed staleness.

The guard:
  * accepts only post-only limit orders and cancels from Jev; never market orders
  * sends a reduce-only market order ONLY to flatten, on its own decision
  * cancels everything when Jev's heartbeat stops, Jev disconnects, the feed goes
    stale, or the exchange connection drops
  * on daily loss limit: cancel all, flatten, refuse new orders until the next UTC day
  * if anyone (Opus, Jev, any prompt) asks to change a limit over the bus, it
    writes the request to leash.log and says no

Prevents: D2, D5 (inventory + loss caps), D4, D6 (cancel on stale/disconnect),
D9 (order rate), D10 (the leash).

    python -m desk.guard --mode paper
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import bus
from .config import MS, DeskConfig
from .events import BookUpdate, from_dict

log = logging.getLogger("guard")


@dataclass(frozen=True)
class Limits:
    max_inventory_btc: float
    max_loss_per_day_usd: float
    max_orders_per_sec: float
    max_order_size_btc: float
    max_price_dev_frac: float
    jev_heartbeat_ms: float
    feed_silence_ms: float
    feed_lag_ms: float


def load_limits(path: Path) -> Limits:
    """Every limit must be written down explicitly; there are no defaults for the leash."""
    data = tomllib.loads(Path(path).read_text())
    names = {f.name for f in fields(Limits)}
    missing, extra = names - set(data), set(data) - names
    if missing or extra:
        raise ValueError(f"guard.toml: missing={sorted(missing)} unknown={sorted(extra)}")
    lim = Limits(**{k: float(v) for k, v in data.items()})
    if any(getattr(lim, n) <= 0 for n in names):
        raise ValueError("guard.toml: every limit must be > 0")
    mode = Path(path).stat().st_mode
    if mode & 0o022:
        log.warning("guard.toml is writable by group/others (mode %o) — chmod 600 it", mode & 0o777)
    return lim


class Leash:
    """Pure risk state + decisions. No I/O except leash.log; driven by the guard loop."""

    def __init__(self, limits: Limits, leash_log: Path, clock: Callable[[], int] = time.time_ns) -> None:
        self.limits = limits
        self.leash_log = Path(leash_log)
        self.clock = clock
        self.position = 0.0
        self.cash = 0.0
        self.open: dict[int, tuple[int, float, float]] = {}     # oid -> (side, price, remaining)
        self.halted: str | None = None
        self.day = self._today()
        self.day_start_equity: float | None = None
        self._tokens = limits.max_orders_per_sec
        self._last_refill = clock()

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock() / 1e9, tz=timezone.utc).date().isoformat()

    # -- accounting -------------------------------------------------------
    def equity(self, mark: float) -> float:
        return self.cash + self.position * mark

    def day_pnl(self, mark: float) -> float:
        today = self._today()
        if today != self.day or self.day_start_equity is None:
            if today != self.day and self.halted == "daily_loss":
                self.halted = None
            self.day = today
            self.day_start_equity = self.equity(mark)
        return self.equity(mark) - self.day_start_equity

    def on_fill(self, oid: int, side: int, price: float, size: float, fee: float) -> None:
        self.position = round(self.position + side * size, 10)
        self.cash -= side * price * size + fee
        if oid in self.open:
            s, p, rem = self.open[oid]
            rem = round(rem - size, 10)
            if rem <= 0:
                del self.open[oid]
            else:
                self.open[oid] = (s, p, rem)

    # -- decisions --------------------------------------------------------
    def _take_token(self) -> bool:
        now = self.clock()
        rate = self.limits.max_orders_per_sec
        self._tokens = min(rate, self._tokens + (now - self._last_refill) / 1e9 * rate)
        self._last_refill = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def check_place(self, oid: int, side: int, price: float, size: float, mark: float | None) -> str | None:
        """None = allowed. Otherwise the reason it's refused."""
        L = self.limits
        if self.halted:
            return f"halted:{self.halted}"
        if side not in (1, -1) or size <= 0 or price <= 0:
            return "malformed"
        if size > L.max_order_size_btc + 1e-12:
            return "order_size"
        if mark is None:
            return "no_mark"
        if abs(price / mark - 1) > L.max_price_dev_frac:
            return "price_far_from_mark"
        # worst case: every open order on this side fills, plus this one
        same_side = sum(rem for s, _, rem in self.open.values() if s == side)
        worst = self.position + side * (same_side + size)
        if abs(worst) > L.max_inventory_btc + 1e-12 and abs(worst) > abs(self.position):
            return "max_inventory"
        if not self._take_token():
            return "rate_limit"
        self.open[oid] = (side, price, size)
        return None

    def breach(self, mark: float | None) -> str | None:
        if mark is None:
            return None
        if self.day_pnl(mark) <= -self.limits.max_loss_per_day_usd:
            return "daily_loss"
        if abs(self.position) > self.limits.max_inventory_btc + 1e-12:
            return "inventory_over_cap"
        return None

    def refuse(self, who: str, msg: dict) -> None:
        """Somebody asked to loosen the leash. Write it down. Say no."""
        self.leash_log.parent.mkdir(parents=True, exist_ok=True)
        with self.leash_log.open("a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "from": who,
                                "request": msg, "answer": "no"}) + "\n")
        log.warning("leash change requested by %s — refused and logged", who)


ALLOWED_FROM_JEV = {"hb", "place", "cancel", "cancel_all"}


class Guard:
    def __init__(self, cfg: DeskConfig, limits: Limits, exchange) -> None:
        self.cfg, self.ex = cfg, exchange
        self.leash = Leash(limits, cfg.paths.leash_log)
        self.L = limits
        self.hub = bus.Hub(cfg.paths.bus_dir / "guard.sock", self.on_jev, self.on_jev_gone)
        self.reporter: bus.Peer | None = None
        self.mark: float | None = None
        self.last_hb = 0
        self.last_feed_wall = 0
        self.last_feed_lag = 0
        self.stale: str | None = None
        self._flatten_wait_until = 0

    # -- outbound -------------------------------------------------------------
    def alert(self, kind: str, **kw) -> None:
        msg = {"type": "alert", "kind": kind, "src": "guard", "ts": time.time_ns(), **kw}
        self.hub.publish(msg)
        if self.reporter:
            try:
                self.reporter.send_nowait(msg)
            except Exception:
                self.reporter = None

    async def cancel_everything(self, why: str) -> None:
        log.warning("CANCEL ALL: %s", why)
        await self.ex.cancel_all()
        self.leash.open.clear()
        self.alert("cancel_all", reason=why)

    async def flatten(self, why: str, qty: float | None = None) -> None:
        """Reduce-only market order: the one market order the desk may send. Default: all of it."""
        await self.cancel_everything(why)
        qty = -self.leash.position if qty is None else qty
        if qty and time.time_ns() >= self._flatten_wait_until:
            self._flatten_wait_until = time.time_ns() + 2_000 * MS   # let the fill come back
            log.error("FLATTEN %.6f: %s", qty, why)
            await self.ex.flatten(qty)
            self.alert("flatten", reason=why, qty=qty)

    # -- from jev ---------------------------------------------------------------
    async def on_jev(self, msg: dict, peer: bus.Peer) -> None:
        t = msg.get("type")
        if t not in ALLOWED_FROM_JEV:
            # anything else — set_limit, raise_cap, "just this once" — is a leash change. No.
            self.leash.refuse("jev-bus", msg)
            await peer.send({"type": "refused", "request": t, "answer": "no"})
            self.alert("leash_refused", request=t)
            return
        if t == "hb":
            self.last_hb = time.time_ns()
        elif t == "place":
            if self.stale:
                why = f"stale:{self.stale}"
            else:
                why = self.leash.check_place(msg["oid"], msg["side"], msg["price"], msg["size"], self.mark)
            if why:
                await peer.send({"type": "rejected", "oid": msg["oid"], "reason": why})
                if why.startswith(("max_inventory", "rate_limit", "halted")):
                    self.alert("leash_hit", reason=why)
                return
            asyncio.create_task(self.ex.place_post_only(msg["oid"], msg["side"], msg["price"], msg["size"]))
        elif t == "cancel":
            asyncio.create_task(self.ex.cancel(msg["oid"]))
        elif t == "cancel_all":
            await self.cancel_everything("jev_requested")

    async def on_jev_gone(self, peer: bus.Peer) -> None:
        await self.cancel_everything("jev_disconnected")

    # -- loops ------------------------------------------------------------------
    async def feed_loop(self) -> None:
        while True:
            peer = await bus.connect(self.cfg.paths.bus_dir / "feed.sock")
            async for m in bus.messages(peer):
                if m.get("type") != "event":
                    continue
                ev = from_dict(m["event"])
                self.last_feed_wall = time.time_ns()
                self.last_feed_lag = ev.recv_ts - ev.exch_ts
                if hasattr(self.ex, "on_market"):
                    self.ex.on_market(ev)
                if isinstance(ev, BookUpdate) and hasattr(self.ex, "sim"):
                    self.mark = self.ex.sim.book.mid() or self.mark
                elif isinstance(ev, BookUpdate):
                    self._book_mark(ev)
            self.alert("feed_disconnected")

    def _book_mark(self, ev: BookUpdate) -> None:
        if ev.bids and ev.asks:
            self.mark = (max(p for p, s in ev.bids if s > 0) + min(p for p, s in ev.asks if s > 0)) / 2

    async def exchange_loop(self) -> None:
        async for u in self.ex.updates():
            if u["type"] == "fill":
                self.leash.on_fill(u["oid"], u["side"], u["price"], u["size"], u["fee"])
                u["position"] = self.leash.position
            elif u["type"] in ("rejected", "cancelled"):
                self.leash.open.pop(u["oid"], None)
            elif u["type"] == "disconnected":
                self.alert("exchange_disconnected")
            self.hub.publish(u)
            if self.reporter and u["type"] == "fill":
                try:
                    self.reporter.send_nowait(u)
                except Exception:
                    self.reporter = None

    async def watchdog(self, period_s: float = 0.025) -> None:
        while True:
            await asyncio.sleep(period_s)
            now = time.time_ns()
            reasons = []
            if now - self.last_hb > self.L.jev_heartbeat_ms * MS:
                reasons.append("jev_heartbeat")
            if now - self.last_feed_wall > self.L.feed_silence_ms * MS:
                reasons.append("feed_silent")
            if self.last_feed_lag > self.L.feed_lag_ms * MS:
                reasons.append("feed_lag")
            if not self.ex.connected:
                reasons.append("exchange_disconnected")
            stale = ",".join(reasons) or None
            if stale and not self.stale:
                await self.cancel_everything(stale)
            self.stale = stale

            b = self.leash.breach(self.mark)
            if b == "daily_loss" and self.leash.halted != "daily_loss":
                self.leash.halted = "daily_loss"
                await self.flatten("daily_loss")
                self.alert("leash_hit", reason="daily_loss", pnl=self.leash.day_pnl(self.mark))
            elif b == "inventory_over_cap" and time.time_ns() >= self._flatten_wait_until:
                pos, cap = self.leash.position, self.L.max_inventory_btc
                excess = pos - cap if pos > 0 else pos + cap
                self.alert("leash_hit", reason="inventory_over_cap", position=pos)
                await self.flatten("inventory_over_cap", qty=-excess)

    async def status_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.reporter is None:
                try:
                    self.reporter = await bus.connect(self.cfg.paths.bus_dir / "reporter.sock", attempts=1)
                except (FileNotFoundError, ConnectionRefusedError):
                    continue
            L, lsh = self.L, self.leash
            pnl = lsh.day_pnl(self.mark) if self.mark else 0.0
            try:
                self.reporter.send_nowait({
                    "type": "guard_status", "position": lsh.position, "mark": self.mark,
                    "day_pnl": pnl, "halted": lsh.halted, "stale": self.stale,
                    "open_orders": len(lsh.open),
                    "limits": {"inventory": [abs(lsh.position), L.max_inventory_btc],
                               "day_loss": [max(-pnl, 0.0), L.max_loss_per_day_usd]},
                })
            except Exception:
                self.reporter = None

    async def run(self) -> None:
        await self.hub.start()
        self.last_hb = self.last_feed_wall = time.time_ns()
        await asyncio.gather(self.feed_loop(), self.exchange_loop(), self.watchdog(), self.status_loop())


def main() -> None:
    import argparse

    from . import config
    from .exchange import exchange_client
    from .ladder import Ladder

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--mode", choices=["paper", "live"], required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(a.config)
    limits = load_limits(cfg.paths.guard_config)
    if a.mode == "live":
        Ladder(cfg).require_live(limits)
    bus.run(Guard(cfg, limits, exchange_client(cfg, a.mode)).run())


if __name__ == "__main__":
    main()
