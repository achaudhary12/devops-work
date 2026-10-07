import asyncio
import json

import pytest

from desk.guard import Guard, Leash, Limits, load_limits

LIM = Limits(max_inventory_btc=0.01, max_loss_per_day_usd=10.0, max_orders_per_sec=2.0,
             max_order_size_btc=0.005, max_price_dev_frac=0.01, jev_heartbeat_ms=500.0,
             feed_silence_ms=1000.0, feed_lag_ms=250.0)


class Clock:
    def __init__(self, t=1_767_225_600 * 10**9):
        self.t = t

    def __call__(self):
        return self.t


def leash(tmp_path, clock=None):
    return Leash(LIM, tmp_path / "leash.log", clock or Clock())


def test_inventory_cap_counts_open_orders(tmp_path):
    from dataclasses import replace
    L = Leash(replace(LIM, max_orders_per_sec=100.0), tmp_path / "leash.log", Clock())
    assert L.check_place(1, 1, 60_000, 0.005, 60_000) is None
    assert L.check_place(2, 1, 60_000, 0.005, 60_000) is None
    assert L.check_place(3, 1, 60_000, 0.001, 60_000) == "max_inventory"
    assert L.check_place(4, -1, 60_000, 0.005, 60_000) is None        # reducing side still fine


def test_order_size_price_and_rate(tmp_path):
    clk = Clock()
    L = leash(tmp_path, clk)
    assert L.check_place(1, 1, 60_000, 0.006, 60_000) == "order_size"
    assert L.check_place(2, 1, 50_000, 0.001, 60_000) == "price_far_from_mark"
    assert L.check_place(3, 1, 60_000, 0.001, None) == "no_mark"
    assert L.check_place(4, 1, 60_000, 0.001, 60_000) is None
    assert L.check_place(5, -1, 60_000, 0.001, 60_000) is None
    assert L.check_place(6, -1, 60_000, 0.001, 60_000) == "rate_limit"
    clk.t += 10**9
    assert L.check_place(7, -1, 60_000, 0.001, 60_000) is None


def test_daily_loss_breach_and_reset_next_day(tmp_path):
    clk = Clock()
    L = leash(tmp_path, clk)
    L.day_pnl(60_000)
    L.on_fill(1, 1, 60_000, 0.01, 0.0)
    assert L.breach(59_500) is None                  # -$5
    assert L.breach(58_900) == "daily_loss"          # -$11
    L.halted = "daily_loss"
    assert L.check_place(9, -1, 58_900, 0.001, 58_900).startswith("halted")
    clk.t += 86_400 * 10**9
    L.day_pnl(58_900)
    assert L.halted is None


def test_leash_refusal_is_logged(tmp_path):
    L = leash(tmp_path)
    L.refuse("opus", {"type": "set_limit", "max_inventory_btc": 5})
    rec = json.loads((tmp_path / "leash.log").read_text().splitlines()[0])
    assert rec["answer"] == "no" and rec["request"]["max_inventory_btc"] == 5


def test_guard_toml_requires_every_limit(tmp_path):
    p = tmp_path / "guard.toml"
    p.write_text("max_inventory_btc = 0.01\n")
    with pytest.raises(ValueError, match="missing"):
        load_limits(p)
    p.write_text("\n".join(f"{k} = {v}" for k, v in LIM.__dict__.items()))
    assert load_limits(p) == LIM


class FakeEx:
    connected = True

    def __init__(self):
        self.calls = []

    async def place_post_only(self, *a): self.calls.append(("place", a))
    async def cancel(self, oid): self.calls.append(("cancel", oid))
    async def cancel_all(self): self.calls.append(("cancel_all",))
    async def flatten(self, q): self.calls.append(("flatten", q))


class FakePeer:
    def __init__(self):
        self.sent = []

    async def send(self, m): self.sent.append(m)


def test_guard_says_no_to_leash_changes_over_the_bus(tmp_cfg):
    async def go():
        g = Guard(tmp_cfg, LIM, FakeEx())
        peer = FakePeer()
        await g.on_jev({"type": "set_limit", "max_loss_per_day_usd": 1e9}, peer)
        await g.on_jev({"type": "market_order", "side": 1, "size": 1}, peer)
        return g, peer
    g, peer = asyncio.run(go())
    assert [m["answer"] for m in peer.sent] == ["no", "no"]
    assert len(tmp_cfg.paths.leash_log.read_text().splitlines()) == 2
    assert not any(c[0] == "place" for c in g.ex.calls)


def test_guard_cancels_everything_when_jev_goes_quiet(tmp_cfg):
    import time

    async def go():
        ex = FakeEx()
        g = Guard(tmp_cfg, LIM, ex)
        g.mark = 60_000.0
        g.last_hb = g.last_feed_wall = time.time_ns()
        task = asyncio.create_task(g.watchdog(period_s=0.01))
        await asyncio.sleep(0.1)
        assert ("cancel_all",) not in ex.calls
        g.last_feed_wall = time.time_ns() + 10**10          # keep feed fresh; only Jev goes quiet
        await asyncio.sleep(0.6)
        task.cancel()
        return g, ex
    g, ex = asyncio.run(go())
    assert ("cancel_all",) in ex.calls
    assert "jev_heartbeat" in g.stale


def test_guard_flattens_on_daily_loss(tmp_cfg):
    import time

    async def go():
        ex = FakeEx()
        g = Guard(tmp_cfg, LIM, ex)
        g.mark = 60_000.0
        g.leash.day_pnl(60_000.0)
        g.leash.on_fill(1, 1, 60_000.0, 0.01, 0.0)
        g.mark = 58_000.0                                  # -$20
        g.last_hb = g.last_feed_wall = time.time_ns() + 10**10
        task = asyncio.create_task(g.watchdog(period_s=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        return g, ex
    g, ex = asyncio.run(go())
    assert ("flatten", -0.01) in ex.calls
    assert g.leash.halted == "daily_loss"
