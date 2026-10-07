"""Brief, ladder, recorder, report card, pass-or-die, shadow."""
from dataclasses import replace

import numpy as np
import pytest

from desk import brief, tape
from desk.config import NS
from desk.events import BookUpdate, Trade
from desk.ladder import Ladder
from desk.pass_or_die import judge, run_days
from desk.recorder import Recorder
from desk.replay import replay
from desk.report_card import by_bucket, fills_frame, update_widen
from desk.shadow import compare
from desk.synth import synth_tape

T0 = 1_767_225_600 * NS


# -- morning brief --------------------------------------------------------------------

def test_no_brief_no_trading(tmp_cfg):
    with pytest.raises(SystemExit, match="we do not trade today"):
        brief.check(tmp_cfg)


def test_brief_needs_all_three_lines(tmp_cfg):
    p = brief.path_for(tmp_cfg)
    p.parent.mkdir(parents=True)
    p.write_text("feel: choppy\nmax_loss_usd: 10\nstop: \n")
    with pytest.raises(SystemExit, match="stop"):
        brief.check(tmp_cfg)


def test_brief_cannot_be_looser_than_the_guard(tmp_cfg):
    brief.write(tmp_cfg, "calm", 50, "any liquidation over $5M")
    with pytest.raises(SystemExit, match="guard allows"):
        brief.check(tmp_cfg, guard_max_loss=10)
    brief.write(tmp_cfg, "calm", 8, "any liquidation over $5M")
    assert brief.check(tmp_cfg, guard_max_loss=10)["stop"] == "any liquidation over $5M"


# -- ladder ------------------------------------------------------------------------------

def test_ladder_needs_a_human_to_go_live(tmp_cfg):
    from datetime import datetime, timedelta, timezone
    lad = Ladder(tmp_cfg)
    later = lambda d: datetime.now(timezone.utc) + timedelta(days=d)  # noqa: E731
    assert lad.name == "shadow"
    with pytest.raises(SystemExit):
        lad.require_live()
    lad.week(0.0, 0, 0.0, now=later(8))
    assert lad.name == "paper"
    assert "waiting for a human" in lad.week(1.0, 100, 0.5, now=later(8))
    assert lad.name == "paper"
    tmp_cfg.paths.go_live_file.write_text('approved_by = "me"\napproved_at = "2026-10-07"\nmax_inventory_usd = 200\n')
    lad.week(1.0, 100, 0.5, now=later(8))
    assert lad.name == "live_100usd"
    lad.require_live()


def test_ladder_doubles_on_good_fills_and_drops_on_a_losing_week(tmp_cfg):
    tmp_cfg.paths.root.mkdir(exist_ok=True)
    tmp_cfg.paths.go_live_file.write_text('approved_by = "me"\napproved_at = "x"\nmax_inventory_usd = 200\n')
    lad = Ladder(tmp_cfg)
    lad.state.rung = 2
    lad.week(5.0, 300, 0.4)
    assert lad.name == "live_100usd"
    lad.week(5.0, 300, -0.1)                       # negative report card: fills don't count
    assert lad.name == "live_100usd"
    lad.week(5.0, 250, 0.2)
    assert lad.name == "live_200usd"
    assert "GO_LIVE caps" in Ladder(tmp_cfg).week(5.0, 600, 0.3)     # 400 > the human's 200
    lad.week(-1.0, 10, 0.3)
    assert lad.name == "live_100usd"


# -- recorder -------------------------------------------------------------------------

class ListFeed:
    def __init__(self):
        self.resyncs = 0

    async def resync(self):
        self.resyncs += 1


def test_recorder_records_gaps_and_never_bridges_them(tmp_cfg):
    import asyncio

    async def go():
        feed = ListFeed()
        rec = Recorder(tmp_cfg, feed)
        b = lambda s, snap=False: BookUpdate(T0 + s, T0 + s + 5, s, ((100.0, 1.0),), ((100.1, 1.0),), snap)  # noqa: E731
        rec.record(b"{}", [b(1, True), b(2), b(3)])
        rec.record(b"{}", [b(5)])                                   # 4 is missing
        rec.record(b"{}", [b(6)])                                   # still untrusted: dropped
        rec.record(b"{}", [b(7, True), b(8)])                       # snapshot heals
        rec.record(b"{}", [Trade(T0 + 9, T0 + 14, 9, 100.0, 0.1, 1)])
        await asyncio.sleep(0)
        rec.flush()
        rec.con.close()
        return feed
    feed = asyncio.run(go())
    seqs = [e.seq for e in tape.read_range(tmp_cfg.paths.tape_db, 0, 2**62)]
    assert seqs == [1, 2, 3, 7, 8, 9]
    g = tape.gaps(tmp_cfg.paths.tape_db)
    assert len(g) == 1 and g[0][2:4] == (4, 5) and g[0][4] == "seq_break"
    assert feed.resyncs == 1


# -- report card, pass or die, shadow --------------------------------------------------

def test_report_card_widens_losing_buckets(cfg, hour_tape):
    res = replay(hour_tape, cfg)
    df = fills_frame(res, cfg)
    assert len(df) == len(res.fills) > 0
    late = df.filter(df["ts"] > res.end_ts - 10 * NS)
    assert late["mo_10s"].is_nan().all(), "fills without 10s of future tape are not scored"
    card = by_bucket(df)
    widen = update_widen(card, {}, min_fills=5)
    losing = card.filter((card["mo_10s_bps"] < 0) & (card["n"] >= 5))["bucket"].to_list()
    assert set(losing) == {k for k, v in widen.items() if v > 0}


def test_pass_or_die_on_synthetic_tape(cfg):
    days = [(f"d{i}", synth_tape(1800, seed=i, start_ns=T0 + i * 86_400 * NS), 0) for i in range(2)]
    v = judge(run_days(days, cfg), cfg)
    names = [l.name for l in v.lines]
    assert any("10s report card" in n for n in names)
    assert any("violent" in n for n in names)
    text = v.render()
    assert "CI" in text and text.splitlines()[-1] in ("SURVIVES", "DIES")
    # no violent days in the set: unproven, so the version dies
    assert not v.survives


def test_shadow_far_from_replay_means_replay_is_lying(cfg, hour_tape):
    rep = replay(hour_tape[: len(hour_tape) // 4], cfg)
    same = {"places": rep.places, "fills": len(rep.fills), "pnl": rep.pnl}
    assert all(c.ok for c in compare(same, rep, cfg))
    half = {"places": rep.places, "fills": len(rep.fills) // 2, "pnl": rep.pnl}
    assert not all(c.ok for c in compare(half, rep, cfg))
