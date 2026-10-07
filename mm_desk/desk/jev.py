"""Jev, the trader. One class, used unchanged by research, replay, shadow and live.

Jev is fed events one at a time and returns the quotes it wants resting. It
never sees a clock, never touches the exchange and never holds keys: the
caller (replay engine, shadow runner or live runner + guard) does that.

Prevents: D7 (one code path), plus everything signals/pricing/pull prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import MS, DeskConfig
from .events import Event
from .pricing import Quote, price
from .pull import PullSwitch
from .signals import SignalState, View


def mood(vol: float, cfg: DeskConfig) -> str:
    return "violent" if vol > cfg.signals.violent_vol else "calm"


def size_bucket(size: float, cfg: DeskConfig) -> str:
    lo, hi = cfg.signals.size_buckets
    return "small" if size <= lo else ("medium" if size <= hi else "large")


def bucket_key(ts_ns: int, size: float, vol: float, cfg: DeskConfig) -> str:
    hour = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).hour
    return f"h{hour:02d}|{size_bucket(size, cfg)}|{mood(vol, cfg)}"


def load_widen(path: Path) -> dict[str, float]:
    """bucket_key -> extra spread in bps, written nightly by report_card.py."""
    try:
        return {k: float(v) for k, v in json.loads(path.read_text()).items()}
    except FileNotFoundError:
        return {}


@dataclass(slots=True)
class Decision:
    quote: Quote | None          # None = pulled / not ready: cancel everything
    reason: str | None           # why we are not quoting
    view: View | None


class Jev:
    def __init__(self, cfg: DeskConfig, widen: dict[str, float] | None = None) -> None:
        self.cfg = cfg
        self.signals = SignalState(cfg.signals)
        self.pull = PullSwitch(cfg.pull)
        self.widen = widen or {}
        self.inventory = 0.0
        # recv_ts already includes the feed delay; what's left is getting the order there
        self.delay_ns = int(cfg.latency.order_ms * MS)

    def on_fill(self, side: int, size: float) -> None:
        self.inventory = round(self.inventory + side * size, 10)

    def on_event(self, ev: Event) -> Decision:
        self.signals.update(ev)
        v = self.signals.view(warmup_s=self.cfg.pull.flow_warmup_s)
        if v is None:
            return Decision(None, "book_invalid", None)
        reason = self.pull.update(v, self.signals)
        if reason is not None:
            return Decision(None, reason, v)
        if not v.warm:
            return Decision(None, "warmup", v)

        bid, bsz, ask, asz, centre, spread = price(v, self.inventory, self.cfg)
        widen_bps = self.widen.get(bucket_key(v.as_of, max(bsz, asz), v.vol, self.cfg), 0.0)
        if widen_bps > 0:
            bid, bsz, ask, asz, centre, spread = price(v, self.inventory, self.cfg, widen_bps)
        decision_ts = ev.recv_ts
        q = Quote(bid=bid, bid_size=bsz, ask=ask, ask_size=asz, centre=centre, spread=spread,
                  as_of=v.as_of, decision_ts=decision_ts, live_ts=decision_ts + self.delay_ns)
        return Decision(q, None, v)
