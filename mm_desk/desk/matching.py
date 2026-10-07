"""The fill model, shared by replay, paper trading and shadow mode.

A resting order joins the back of the visible queue at its price when it lands.
It fills only after every order queued ahead of it at that price has traded, or
when a trade prints *through* its price. Cancels ahead of us earn us nothing.
Post-only orders that would cross on landing are rejected.

Prevents: D7 (one honest fill model everywhere; shadow vs replay compare like with like).
"""
from __future__ import annotations

from dataclasses import dataclass

from .book import Book
from .events import BookUpdate, Trade
from .orders import Order


@dataclass(slots=True)
class Resting:
    order: Order
    queue_ahead: float
    landed: int


class QueueSim:
    def __init__(self, depth: int = 10) -> None:
        self.book = Book(depth)
        self.resting: dict[int, Resting] = {}

    def on_book(self, ev: BookUpdate) -> None:
        self.book.apply(ev)

    def land_place(self, o: Order, t: int) -> bool:
        bb, ba = self.book.best_bid, self.book.best_ask
        crosses = (o.side > 0 and ba is not None and o.price >= ba[0]) or \
                  (o.side < 0 and bb is not None and o.price <= bb[0])
        if crosses:
            return False
        self.resting[o.oid] = Resting(o, self.book.size_at(o.side, o.price), t)
        return True

    def land_cancel(self, oid: int) -> Order | None:
        r = self.resting.pop(oid, None)
        return r.order if r else None

    def on_trade(self, ev: Trade) -> list[tuple[Resting, float]]:
        """Returns (resting, qty) for every fill this print causes. Mutates order.filled."""
        out = []
        for r in list(self.resting.values()):
            o = r.order
            if o.side != -ev.aggressor:          # sellers hit bids, buyers lift asks
                continue
            through = (ev.price < o.price) if o.side > 0 else (ev.price > o.price)
            if through:
                qty = o.remaining
            elif ev.price == o.price:
                qty = ev.size - r.queue_ahead
                r.queue_ahead = max(0.0, r.queue_ahead - ev.size)
            else:
                continue
            qty = round(min(qty, o.remaining), 10)
            if qty <= 0:
                continue
            o.filled = round(o.filled + qty, 10)
            out.append((r, qty))
            if o.remaining <= 0:
                del self.resting[o.oid]
        return out
