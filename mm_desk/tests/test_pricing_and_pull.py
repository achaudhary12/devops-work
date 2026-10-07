import numpy as np
import pytest

from desk.config import MS, NS, DeskConfig
from desk.events import BookUpdate, Funding, Liquidation, Trade
from desk.pricing import min_spread, price
from desk.pull import PullSwitch
from desk.signals import SignalState, View

T0 = 1_767_225_600 * NS


def view(wmid=60_000.0, pressure=0.0, vol=5.0, bb=None, ba=None, **kw):
    bb = bb if bb is not None else round(wmid - 0.05, 1)
    ba = ba if ba is not None else round(wmid + 0.05, 1)
    d = dict(as_of=T0, best_bid=bb, best_ask=ba, wmid=wmid, pressure=pressure, net_flow=0.0,
             flow_normal=1.0, vol=vol, bid_depth=10.0, ask_depth=10.0, feed_lag_ns=0, warm=True)
    d.update(kw)
    return View(**d)


@pytest.mark.parametrize("fee", [0.0, 0.0001, 0.0002, -0.00005])
def test_gap_never_below_fees_plus_tick(fee):
    cfg = DeskConfig().with_("market", maker_fee=fee)
    rng = np.random.default_rng(0)
    for _ in range(2000):
        v = view(wmid=float(rng.uniform(20_000, 120_000)), pressure=float(rng.uniform(-1, 1)),
                 vol=float(rng.uniform(0, 40)))
        inv = float(rng.uniform(-0.06, 0.06))
        bid, bsz, ask, asz, *_ = price(v, inv, cfg)
        if bid is not None and ask is not None:
            assert ask - bid >= min_spread(cfg, v.wmid) - 1e-9
        if bid is not None:
            assert bid < v.best_ask, "post-only bid would cross"
        if ask is not None:
            assert ask > v.best_bid, "post-only ask would cross"


def test_spread_widens_with_vol():
    cfg = DeskConfig().with_("market", maker_fee=0.0)
    calm = price(view(vol=1.0), 0.0, cfg)
    wild = price(view(vol=30.0), 0.0, cfg)
    assert (wild[2] - wild[0]) > (calm[2] - calm[0])


def test_long_inventory_moves_both_quotes_down_and_shrinks_buying():
    cfg = DeskConfig().with_("market", maker_fee=0.0)
    flat = price(view(vol=10.0), 0.0, cfg)
    long_ = price(view(vol=10.0), 0.03, cfg)
    assert long_[0] < flat[0] and long_[2] < flat[2]
    assert long_[1] < flat[1], "bid size shrinks as we get longer"
    assert long_[3] == pytest.approx(cfg.pricing.base_size), "selling side stays full size"
    full = price(view(vol=10.0), cfg.pricing.max_inventory, cfg)
    assert full[0] is None, "no bid at all at max inventory"


def test_pressure_leans_the_centre():
    cfg = DeskConfig().with_("pricing", pressure_coef=2.0)
    assert price(view(pressure=0.8), 0.0, cfg)[4] > price(view(pressure=-0.8), 0.0, cfg)[4]


def test_weighted_mid_and_pressure_formulas():
    st = SignalState(DeskConfig().signals)
    st.update(BookUpdate(T0, T0, 1, ((100.0, 3.0),), ((101.0, 1.0),), snapshot=True))
    v = st.view()
    assert v.wmid == pytest.approx((100 * 1 + 101 * 3) / 4)
    assert v.pressure == pytest.approx((3 - 1) / 4)


# -- pulls -------------------------------------------------------------------------------

def book(t, bid_sz=5.0, ask_sz=5.0, lag_ms=10, seq=1):
    return BookUpdate(t - lag_ms * MS, t, seq, tuple((60_000 - k * 0.1, bid_sz) for k in range(10)),
                      tuple((60_000.1 + k * 0.1, ask_sz) for k in range(10)), snapshot=True)


def run(events, cfg=None):
    cfg = cfg or DeskConfig()
    st, ps, out = SignalState(cfg.signals), PullSwitch(cfg.pull), []
    for e in events:
        st.update(e)
        v = st.view(warmup_s=0)
        out.append(ps.update(v, st) if v else "invalid")
    return out


def test_book_side_collapse_pulls():
    r = run([book(T0), book(T0 + 500 * MS, bid_sz=1.5)])
    assert r[0] is None and "bid_side_collapse" in r[1]


def test_slow_drain_does_not_pull():
    evs = [book(T0 + i * 400 * MS, bid_sz=5.0 * 0.8 ** i) for i in range(4)]
    assert run(evs)[-1] is None


def test_stale_feed_pulls():
    assert "feed_lag" in run([book(T0, lag_ms=300)])[0]


def test_liquidation_pulls_only_above_threshold():
    cfg = DeskConfig().with_("pull", liq_notional_usd=1_000_000)
    small = Liquidation(T0 + 1, T0 + 1, 2, 60_000, 1.0, -1)       # $60k
    big = Liquidation(T0 + 2, T0 + 2, 3, 60_000, 20.0, -1)        # $1.2M
    r = run([book(T0), small, big], cfg)
    assert r[1] is None and r[2].startswith("liquidation_")


def test_funding_two_minutes_out_pulls():
    f = Funding(T0 + 1, T0 + 1, 2, 1e-4, T0 + 100 * NS, 60_000.0)
    assert run([book(T0), f])[1] == "funding_soon"


def test_one_way_flow_pulls():
    cfg = DeskConfig().with_("pull", flow_warmup_s=0)
    evs, t = [book(T0)], T0
    for i in range(300):                      # 10 minutes of balanced, small flow
        t += 2 * NS
        evs.append(Trade(t, t, i + 10, 60_000, 0.05, 1 if i % 2 else -1))
        evs.append(book(t + 1, seq=i + 10))
    t += NS
    evs += [Trade(t + k, t + k, 999 + k, 60_000, 0.5, -1) for k in range(4)]
    assert "one_way_flow_sell" in run(evs, cfg)[-1]


def test_comes_back_only_after_20s_of_normal_market():
    evs = [book(T0), book(T0 + 1, lag_ms=400)]
    evs += [book(T0 + s * NS) for s in range(1, 25)]
    r = run(evs)
    back = [i for i, x in enumerate(r) if i > 1 and x is None]
    first_back_ts = evs[back[0]].recv_ts
    assert first_back_ts - evs[1].recv_ts >= 20 * NS
