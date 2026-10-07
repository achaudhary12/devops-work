"""No peeking. Research, replay and live run the same pricing code. Every number
Jev uses must carry a timestamp older than the quote it produces, plus our real
measured delay. Replay one hour of tape and fail if any quote touched a future
message.

Two independent checks:
  1. Timestamps: for every quote, as_of <= decision_ts, decision_ts is the recv_ts
     of the event being processed, and live_ts >= as_of + measured order delay.
  2. Poisoning: corrupt every message after a cut point. Every quote decided
     before the cut must come out bit-for-bit identical. If anything — pricing,
     signals, pull logic, the fill model — read ahead, the poison leaks backwards.
And a self-test proving check 2 catches a trader that does peek.
"""
from dataclasses import replace

import pytest

from desk.config import MS, NS
from desk.events import BookUpdate, Trade
from desk.fit import fit_pressure
from desk.jev import Jev
from desk.replay import PeekError, replay
from desk.synth import synth_tape


def poison(e):
    """Make the future unmistakable: prices +$5,000, sizes x7, all aggressors flipped."""
    if isinstance(e, BookUpdate):
        return replace(e, bids=tuple((p + 5000, s * 7) for p, s in e.bids),
                       asks=tuple((p + 5000, s * 7) for p, s in e.asks))
    if isinstance(e, Trade):
        return replace(e, price=e.price + 5000, size=e.size * 7, aggressor=-e.aggressor)
    return e


def quotes_before(res, t):
    return [q for q in res.quotes if q.decision_ts < t]


def test_every_quote_is_built_from_the_past(cfg, hour_tape):
    res = replay(hour_tape, cfg, keep_quotes=True)
    assert len(res.quotes) > 1000, "Jev should be quoting most of the hour"
    recv = {e.recv_ts for e in hour_tape}
    delay = int(cfg.latency.order_ms * MS)
    for q in res.quotes:
        assert q.as_of <= q.decision_ts, q
        assert q.decision_ts in recv, "decision time must be a moment we actually received something"
        assert q.live_ts >= q.as_of + delay, q


@pytest.mark.parametrize("cut_minute", [17, 41])
def test_poisoned_future_changes_nothing_in_the_past(cfg, hour_tape, cut_minute):
    t_cut = hour_tape[0].recv_ts + cut_minute * 60 * NS
    k = next(i for i, e in enumerate(hour_tape) if e.recv_ts >= t_cut)
    t_cut = hour_tape[k].recv_ts
    clean = replay(hour_tape, cfg, keep_quotes=True)
    dirty = replay(hour_tape[:k] + [poison(e) for e in hour_tape[k:]], cfg, keep_quotes=True)

    a, b = quotes_before(clean, t_cut), quotes_before(dirty, t_cut)
    assert len(a) > 100
    assert a == b, "a quote before the cut changed when only the future changed: something peeked"
    fa = [f for f in clean.fills if f.ts < t_cut]
    fb = [f for f in dirty.fills if f.ts < t_cut]
    assert [(f.ts, f.side, f.price, f.size) for f in fa] == [(f.ts, f.side, f.price, f.size) for f in fb]


class PeekingJev(Jev):
    """A cheater: nudges its centre toward the price 10 messages ahead."""

    def __init__(self, cfg, tape):
        super().__init__(cfg)
        self.tape, self.i = tape, -1

    def on_event(self, ev):
        self.i += 1
        d = super().on_event(ev)
        ahead = self.tape[min(self.i + 10, len(self.tape) - 1)]
        if d.quote and isinstance(ahead, Trade):
            shift = round((ahead.price - d.view.wmid) * 0.5, 1)
            d.quote = replace(d.quote, bid=d.quote.bid and d.quote.bid + shift,
                              ask=d.quote.ask and d.quote.ask + shift)
        return d


def test_the_poison_test_catches_a_cheater(cfg):
    tape = synth_tape(600, seed=3)
    k = len(tape) // 2
    t_cut = tape[k].recv_ts
    dirty_tape = tape[:k] + [poison(e) for e in tape[k:]]
    clean = replay(tape, cfg, PeekingJev(cfg, tape), keep_quotes=True)
    dirty = replay(dirty_tape, cfg, PeekingJev(cfg, dirty_tape), keep_quotes=True)
    assert quotes_before(clean, t_cut) != quotes_before(dirty, t_cut)


def test_coefficients_fit_on_the_test_window_are_refused(cfg):
    tape = synth_tape(900, seed=5)
    half = len(tape) // 2
    fit = fit_pressure(tape[:half], cfg)
    replay(tape[half:], fit.apply(cfg))                      # strictly after: fine
    with pytest.raises(PeekError):
        replay(tape, fit.apply(cfg))                         # overlapping: refused
