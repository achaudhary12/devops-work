"""The jev process: Jev on the live feed, in one of three modes.

  shadow  trades nothing. Logs every quote it would have posted and runs it
          through QueueSim against the live feed using *wall-clock* decision
          times, so our own compute and event-loop lag are charged.
  paper   sends orders to the guard, which runs a PaperExchange.
  live    sends orders to the guard, which holds the real keys.

Jev never talks to the exchange. In paper/live it heartbeats the guard every
100ms; if this process hangs or dies, the guard cancels everything.

Prevents: D6 (heartbeat), D7 (same Jev, same plan_actions, same QueueSim as replay),
D10 (no brief, no trading).

    python -m desk.live --mode shadow
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from itertools import count
from pathlib import Path

import orjson

from . import brief, bus
from .config import MS, DeskConfig
from .events import BookUpdate, Trade, from_dict
from .jev import Jev, load_widen
from .matching import QueueSim
from .orders import Order, Place, plan_actions
from .pull import reason_keys

log = logging.getLogger("jev")


class ShadowBook:
    """Shadow-mode accounting, same rules as replay."""

    def __init__(self, cfg: DeskConfig) -> None:
        self.cfg = cfg
        self.sim = QueueSim(cfg.signals.book_levels)
        self.pending: list[tuple[int, int, object]] = []
        self._seq = count()
        self.cash = self.inventory = 0.0
        self.places = self.fills = self.rejects = 0
        self.start_ns = time.time_ns()

    def schedule(self, act, decision_wall: int) -> None:
        land = decision_wall + int((self.cfg.latency.order_ms + self.cfg.latency.feed_ms) * MS)
        heapq.heappush(self.pending, (land, next(self._seq), act))
        if isinstance(act, Place):
            self.places += 1

    def on_event(self, ev, now: int, on_fill, on_reject) -> None:
        while self.pending and self.pending[0][0] <= now:
            _, _, act = heapq.heappop(self.pending)
            if isinstance(act, Place):
                if not self.sim.land_place(act.order, now):
                    self.rejects += 1
                    on_reject(act.order.oid)
            else:
                self.sim.land_cancel(act.oid)
        if isinstance(ev, BookUpdate):
            self.sim.on_book(ev)
        elif isinstance(ev, Trade):
            for r, qty in self.sim.on_trade(ev):
                o = r.order
                fee = o.price * qty * self.cfg.market.maker_fee
                self.cash -= o.side * o.price * qty + fee
                self.inventory = round(self.inventory + o.side * qty, 10)
                self.fills += 1
                on_fill(o, qty)

    def summary(self) -> dict:
        mid = self.sim.book.mid() or 0.0
        return {"start_ns": self.start_ns, "end_ns": time.time_ns(), "places": self.places,
                "fills": self.fills, "rejects": self.rejects,
                "pnl": self.cash + self.inventory * mid, "inventory": self.inventory}


class LiveJev:
    def __init__(self, cfg: DeskConfig, mode: str) -> None:
        self.cfg, self.mode = cfg, mode
        self.jev = Jev(cfg, load_widen(cfg.paths.widen_file))
        self.working: dict[int, Order] = {}
        self.guard: bus.Peer | None = None
        self.reporter: bus.Peer | None = None
        self.shadow = ShadowBook(cfg) if mode == "shadow" else None
        self.last_recv = 0
        self.last_keys: tuple[str, ...] = ("start",)
        self.last_quote = None
        self.log_path = cfg.paths.root / mode / f"{brief.today()}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")

    def write(self, rec: dict) -> None:
        self._log.write(orjson.dumps(rec) + b"\n")

    def to_reporter(self, msg: dict) -> None:
        if self.reporter:
            try:
                self.reporter.send_nowait(msg)
            except Exception:
                self.reporter = None

    def _drop(self, oid: int) -> None:
        for s, o in list(self.working.items()):
            if o.oid == oid:
                del self.working[s]

    # -- guard link ----------------------------------------------------------
    async def heartbeat(self) -> None:
        while True:
            if self.guard:
                await self.guard.send({"type": "hb", "ts": time.time_ns(), "feed_recv": self.last_recv})
            await asyncio.sleep(0.1)

    async def guard_listener(self) -> None:
        async for u in bus.messages(self.guard):
            t = u.get("type")
            if t == "fill":
                self.jev.inventory = u["position"]          # guard is the source of truth
                o = next((o for o in self.working.values() if o.oid == u["oid"]), None)
                if o is not None:
                    o.filled = round(o.filled + u["size"], 10)
                    if o.remaining <= 0:
                        self._drop(o.oid)
                self.write({"type": "fill", **u})
            elif t in ("rejected", "cancelled"):
                self._drop(u["oid"])
            elif t == "alert":
                self.to_reporter(u)
                if u.get("kind") in ("cancel_all", "flatten"):
                    self.working.clear()
        raise SystemExit("guard went away — exiting so systemd restarts us clean")

    # -- main loop -----------------------------------------------------------
    async def run(self) -> None:
        if self.mode != "shadow":
            from .guard import load_limits
            brief.check(self.cfg, load_limits(self.cfg.paths.guard_config).max_loss_per_day_usd)
            self.guard = await bus.connect(self.cfg.paths.bus_dir / "guard.sock")
            asyncio.create_task(self.guard_listener())
            asyncio.create_task(self.heartbeat())
        try:
            self.reporter = await bus.connect(self.cfg.paths.bus_dir / "reporter.sock", attempts=1)
        except (FileNotFoundError, ConnectionRefusedError):
            log.warning("reporter not running; continuing without the screen")
        feed = await bus.connect(self.cfg.paths.bus_dir / "feed.sock")
        next_state = 0
        async for m in bus.messages(feed):
            if m.get("type") == "gap":
                self.to_reporter({"type": "alert", "kind": "tape_gap", "src": "recorder", **m})
                continue
            if m.get("type") != "event":
                continue
            ev = from_dict(m["event"])
            self.last_recv = ev.recv_ts
            now = time.time_ns()
            if self.shadow:
                self.shadow.on_event(ev, now, self._shadow_fill, self._drop)
            d = self.jev.on_event(ev)
            decision_wall = time.time_ns()

            keys = reason_keys(d.reason)
            if keys != self.last_keys:                 # one alert per pull episode, not per message
                if keys and not set(keys) <= {"warmup", "book_invalid"}:
                    self.to_reporter({"type": "alert", "kind": "pull", "src": "jev", "reason": d.reason})
                self.write({"type": "pull" if keys else "resume", "ts": decision_wall, "reason": d.reason})
                self.last_keys = keys
            self.last_quote = d.quote

            for act in plan_actions(d.quote, self.working, self.cfg):
                if isinstance(act, Place):
                    o = act.order
                    self.working[o.side] = o
                    self.write({"type": "place", "ts": decision_wall, "as_of": d.quote.as_of,
                                "oid": o.oid, "side": o.side, "price": o.price, "size": o.size})
                    if self.shadow:
                        self.shadow.schedule(act, decision_wall)
                    else:
                        await self.guard.send({"type": "place", "oid": o.oid, "side": o.side,
                                               "price": o.price, "size": o.size})
                else:
                    self._drop(act.oid)
                    if self.shadow:
                        self.shadow.schedule(act, decision_wall)
                    else:
                        await self.guard.send({"type": "cancel", "oid": act.oid})

            if decision_wall >= next_state:
                next_state = decision_wall + 1_000 * MS
                self.push_state(d)

    def _shadow_fill(self, o: Order, qty: float) -> None:
        self.jev.on_fill(o.side, qty)
        if o.remaining <= 0:
            self._drop(o.oid)
        self.write({"type": "fill", "ts": time.time_ns(), "oid": o.oid, "side": o.side,
                    "price": o.price, "size": qty, "shadow": True})

    def push_state(self, d) -> None:
        q = d.quote
        st = {"type": "jev_state", "mode": self.mode, "ts": time.time_ns(),
              "inventory": self.jev.inventory, "pulled": d.reason,
              "bid": q.bid if q else None, "ask": q.ask if q else None,
              "bid_size": q.bid_size if q else 0, "ask_size": q.ask_size if q else 0,
              "best_bid": d.view.best_bid if d.view else None,
              "best_ask": d.view.best_ask if d.view else None,
              "vol": d.view.vol if d.view else None}
        if self.shadow:
            st["shadow"] = self.shadow.summary()
            Path(self.log_path.with_suffix(".summary.json")).write_bytes(orjson.dumps(st["shadow"]))
        self.to_reporter(st)
        self._log.flush()


def main() -> None:
    import argparse

    from . import config

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--mode", choices=["shadow", "paper", "live"], required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bus.run(LiveJev(config.load(a.config), a.mode).run())


if __name__ == "__main__":
    main()
