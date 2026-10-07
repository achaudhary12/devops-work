"""How Jev gets paid: a report card for every fill.

For every fill: where was the mid 1s, 10s and 60s later? Scored per unit, after
fees, in bps of the fill price. Fills that age well paid us the spread; fills
that age badly were sold to someone who knew more.

Scores are bucketed by UTC hour, order size and market mood. Any bucket with a
negative 10s score (and enough fills to mean it) gets wider quotes tomorrow via
widen.json, which Jev reads at start.

Prevents: D1 (adverse selection is measured, then priced), D3 (scored after fees).

    python -m desk.report_card --tape var/tape.duckdb --day 2026-10-06
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from .config import NS, DeskConfig
from .jev import mood, size_bucket
from .replay import ReplayResult

HORIZONS_S = (1, 10, 60)


def fills_frame(res: ReplayResult, cfg: DeskConfig) -> pl.DataFrame:
    if not res.fills:
        return pl.DataFrame(schema={"ts": pl.Int64, "side": pl.Int8, "price": pl.Float64,
                                    "size": pl.Float64, "bucket": pl.Utf8, "mood": pl.Utf8,
                                    **{f"mo_{h}s": pl.Float64 for h in HORIZONS_S}})
    ts = np.array([f.ts for f in res.fills], np.int64)
    side = np.array([f.side for f in res.fills])
    px = np.array([f.price for f in res.fills])
    fee_per_unit = np.array([f.fee / f.size for f in res.fills])
    cols = {}
    for h in HORIZONS_S:
        later = ts + h * NS
        m = res.mid_at(later)
        m = np.where(later <= res.end_ts, m, np.nan)       # never score with tape we don't have
        cols[f"mo_{h}s"] = (side * (m - px) - fee_per_unit) / px * 1e4
    df = pl.DataFrame({
        "ts": ts, "side": side, "price": px,
        "size": [f.size for f in res.fills],
        "order_size": [f.order_size for f in res.fills],
        "vol": [f.vol for f in res.fills],
        **cols,
    })
    return df.with_columns(
        pl.from_epoch("ts", time_unit="ns").dt.hour().alias("hour"),
        pl.col("order_size").map_elements(lambda s: size_bucket(s, cfg), return_dtype=pl.Utf8).alias("size_b"),
        pl.col("vol").map_elements(lambda v: mood(v, cfg), return_dtype=pl.Utf8).alias("mood"),
    ).with_columns(
        pl.format("h{}|{}|{}", pl.col("hour").cast(pl.Utf8).str.zfill(2), "size_b", "mood").alias("bucket")
    )


def by_bucket(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return pl.DataFrame()
    return (df.drop_nulls("mo_10s").filter(pl.col("mo_10s").is_not_nan())
              .group_by("bucket")
              .agg(pl.len().alias("n"),
                   *[pl.col(f"mo_{h}s").mean().alias(f"mo_{h}s_bps") for h in HORIZONS_S],
                   (pl.col("mo_10s").std() / pl.len().sqrt()).alias("se_10s"))
              .sort("bucket"))


def update_widen(card: pl.DataFrame, old: dict[str, float], min_fills: int = 20) -> dict[str, float]:
    """Negative 10s bucket -> widen by its loss (at least 0.5bp). Positive -> halve the widening."""
    new = dict(old)
    for row in card.iter_rows(named=True):
        if row["n"] < min_fills:
            continue
        k, s = row["bucket"], row["mo_10s_bps"]
        if s < 0:
            new[k] = round(old.get(k, 0.0) + max(0.5, -s), 3)
        elif k in new:
            new[k] = round(new[k] / 2, 3)
            if new[k] < 0.1:
                del new[k]
    return new


def write_widen(path: Path, widen: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(widen, indent=1, sort_keys=True))


def main() -> None:
    import argparse

    from . import config, tape
    from .jev import Jev, load_widen
    from .replay import replay

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--tape")
    ap.add_argument("--day", required=True, help="UTC date to score, YYYY-MM-DD")
    ap.add_argument("--write", action="store_true", help="update widen.json for tomorrow")
    a = ap.parse_args()
    cfg = config.load(a.config)
    widen = load_widen(cfg.paths.widen_file)
    res = replay(tape.read_day(a.tape or cfg.paths.tape_db, a.day), cfg, Jev(cfg, widen),
                 trade_from_ns=tape._day_bounds(a.day)[0])
    card = by_bucket(fills_frame(res, cfg))
    with pl.Config(tbl_rows=100):
        print(card)
    if a.write:
        write_widen(cfg.paths.widen_file, update_widen(card, widen))
        print(f"wrote {cfg.paths.widen_file}")


if __name__ == "__main__":
    main()
