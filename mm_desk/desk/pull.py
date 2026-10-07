"""When Jev disappears. Disappearing is a skill, not a failure.

Pull every quote, instantly, when any of these hit:
  * one side of the book loses 60% of its size in under 1s
  * aggressive volume in one direction is 4x normal
  * a liquidation larger than $X prints
  * our feed is older than 250ms
  * funding is 2 min away
Come back only after 20s of normal market.

Prevents: D1 (one-way aggression), D4 (stale feed), D5 (cascades, book
collapse), D8 (funding).
"""
from __future__ import annotations

import re
from collections import deque

from .config import MS, NS, Pull as PullCfg
from .signals import SignalState, View


_SUFFIX = re.compile(r"_[\d.]+(usd|ms)$")


def reason_keys(reason: str | None) -> tuple[str, ...]:
    """'feed_lag_312ms,funding_soon' -> ('feed_lag', 'funding_soon'): the trigger, not the reading."""
    return tuple(_SUFFIX.sub("", r) for r in reason.split(",")) if reason else ()


def _window_max(q: deque[tuple[int, float]], t: int, x: float, cutoff: int) -> float:
    while q and q[-1][1] <= x:
        q.pop()
    q.append((t, x))
    while q[0][0] < cutoff:
        q.popleft()
    return q[0][1]


class PullSwitch:
    def __init__(self, cfg: PullCfg) -> None:
        self.cfg = cfg
        # monotonic deques: front is the max depth seen in the last window
        self._bid_max: deque[tuple[int, float]] = deque()
        self._ask_max: deque[tuple[int, float]] = deque()
        self._seen_liq_seq = -1
        self.pulled_until = 0          # ns; quoting allowed when t >= pulled_until
        self.reason: str | None = None

    def check(self, v: View, st: SignalState) -> list[str]:
        """Return every trigger that is firing now (empty = market looks normal)."""
        c, t = self.cfg, v.as_of
        hits: list[str] = []

        cutoff = t - int(c.book_drop_window_s * NS)
        max_bid = _window_max(self._bid_max, t, v.bid_depth, cutoff)
        max_ask = _window_max(self._ask_max, t, v.ask_depth, cutoff)
        keep = 1.0 - c.book_drop_frac
        if max_bid > 0 and v.bid_depth < keep * max_bid:
            hits.append("bid_side_collapse")
        if max_ask > 0 and v.ask_depth < keep * max_ask:
            hits.append("ask_side_collapse")

        if v.warm and v.flow_normal > 0 and abs(v.net_flow) > c.flow_mult * v.flow_normal:
            hits.append("one_way_flow_buy" if v.net_flow > 0 else "one_way_flow_sell")

        liq = st.last_liq
        if liq is not None and liq.seq != self._seen_liq_seq:
            self._seen_liq_seq = liq.seq
            notional = liq.price * liq.size
            if c.liq_notional_usd is None or notional > c.liq_notional_usd:
                hits.append(f"liquidation_{notional:.0f}usd")

        if v.feed_lag_ns > c.max_feed_lag_ms * MS:
            hits.append(f"feed_lag_{v.feed_lag_ns / MS:.0f}ms")

        f = st.funding
        if f is not None and 0 <= f.next_funding_ts - t <= c.funding_guard_s * NS:
            hits.append("funding_soon")
        return hits

    def update(self, v: View, st: SignalState) -> str | None:
        """Returns a reason string while pulled, None when quoting is allowed."""
        hits = self.check(v, st)
        t = v.as_of
        if hits:
            self.pulled_until = t + int(self.cfg.calm_s * NS)
            self.reason = ",".join(hits)
        if t < self.pulled_until:
            return self.reason
        self.reason = None
        return None
