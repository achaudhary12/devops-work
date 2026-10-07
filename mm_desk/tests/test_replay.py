import pytest

from desk.config import MS, NS, DeskConfig
from desk.events import BookUpdate, Funding, Trade
from desk.jev import Decision
from desk.matching import QueueSim
from desk.orders import Order
from desk.pricing import Quote
from desk.replay import DoNothing, SymmetricQuoter, replay

T0 = 1_767_225_600 * NS


def snap(t, bid=100.0, bid_sz=2.0, ask=100.1, ask_sz=2.0, seq=0):
    return BookUpdate(t, t, seq, ((bid, bid_sz),), ((ask, ask_sz),), snapshot=True)


def test_queue_ahead_must_trade_first():
    sim = QueueSim()
    sim.on_book(snap(T0))
    o = Order(+1, 100.0, 1.0)
    assert sim.land_place(o, T0)
    assert sim.on_trade(Trade(T0, T0, 1, 100.0, 1.5, -1)) == []      # 1.5 of the 2.0 ahead
    fills = sim.on_trade(Trade(T0, T0, 2, 100.0, 1.0, -1))            # 0.5 ahead, 0.5 to us
    assert [q for _, q in fills] == [0.5]
    assert o.remaining == pytest.approx(0.5)


def test_trade_through_our_price_fills_us_fully():
    sim = QueueSim()
    sim.on_book(snap(T0))
    o = Order(+1, 100.0, 1.0)
    sim.land_place(o, T0)
    assert [q for _, q in sim.on_trade(Trade(T0, T0, 1, 99.8, 0.01, -1))] == [1.0]


def test_buyers_dont_fill_our_bid():
    sim = QueueSim()
    sim.on_book(snap(T0, bid_sz=0.0))
    sim.land_place(Order(+1, 100.0, 1.0), T0)
    assert sim.on_trade(Trade(T0, T0, 1, 100.0, 5.0, +1)) == []


def test_post_only_cross_rejected():
    sim = QueueSim()
    sim.on_book(snap(T0))
    assert not sim.land_place(Order(+1, 100.1, 1.0), T0)


class Fixed:
    """Quotes one fixed bid, once."""

    def __init__(self, px, size=1.0, cancel_at=None):
        self.px, self.size, self.cancel_at, self.inventory = px, size, cancel_at, 0.0

    def on_fill(self, side, size):
        self.inventory += side * size

    def on_event(self, ev):
        if self.cancel_at is not None and ev.recv_ts >= self.cancel_at:
            return Decision(None, "cancel", None)
        q = Quote(self.px, self.size, None, 0.0, self.px, 0.0, ev.recv_ts, ev.recv_ts, ev.recv_ts)
        return Decision(q, None, None)


def test_latency_order_not_live_until_it_lands():
    cfg = DeskConfig().with_("latency", feed_ms=30, order_ms=40).with_("market", maker_fee=0.0)
    tape = [snap(T0, bid_sz=0.0),
            Trade(T0 + 50 * MS, T0 + 50 * MS, 1, 99.9, 1.0, -1),       # before we land (70ms)
            Trade(T0 + 80 * MS, T0 + 80 * MS, 2, 99.9, 1.0, -1)]       # after
    r = replay(tape, cfg, Fixed(100.0))
    assert [f.ts for f in r.fills] == [T0 + 80 * MS]


def test_cancel_in_flight_can_still_be_filled():
    cfg = DeskConfig().with_("latency", feed_ms=30, order_ms=40).with_("market", maker_fee=0.0)
    tape = [snap(T0, bid_sz=0.0),
            snap(T0 + 100 * MS, bid_sz=0.0, seq=1),                    # we decide to cancel here
            Trade(T0 + 150 * MS, T0 + 150 * MS, 2, 99.9, 1.0, -1),     # cancel lands at 170ms
            Trade(T0 + 200 * MS, T0 + 200 * MS, 3, 99.9, 1.0, -1)]
    r = replay(tape, cfg, Fixed(100.0, cancel_at=T0 + 100 * MS))
    assert len(r.fills) == 1 and r.fills[0].ts == T0 + 150 * MS


def test_fees_and_funding_are_charged():
    cfg = DeskConfig().with_("latency", feed_ms=0, order_ms=0).with_("market", maker_fee=0.001)
    tape = [snap(T0, bid_sz=0.0),
            Trade(T0 + 1, T0 + 1, 1, 99.9, 1.0, -1),
            Funding(T0 + 2, T0 + 2, 2, 0.01, T0 + 8 * 3600 * NS, 100.0, settled=True)]
    r = replay(tape, cfg, Fixed(100.0))
    assert r.fees == pytest.approx(0.1)
    assert r.funding == pytest.approx(1.0)        # long 1 BTC * $100 * 1%
    assert r.cash == pytest.approx(-100.0 - 0.1 - 1.0)


def test_baselines_run(cfg, hour_tape):
    tape = hour_tape[: len(hour_tape) // 6]
    assert replay(tape, cfg, DoNothing()).pnl == 0.0
    sym = replay(tape, cfg, SymmetricQuoter(cfg))
    assert sym.places > 0
