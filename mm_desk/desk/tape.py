"""The tape: every raw message we ever received, in DuckDB, exactly as it arrived.

Two tables:
  tape(recv_ts, exch_ts, seq, kind, event JSON, raw)  -- one row per normalized event
  gaps(recv_ts, kind, expected_seq, got_seq, reason)  -- every hole, recorded, never filled

Readers return events in receive order. Nothing here resamples, interpolates or
forward-fills. A day read starts from the last book snapshot before the day so
the book is real, and replay is told not to trade before the day begins.

Prevents: D7 (one source of truth), D4 (gaps are explicit, not silently bridged).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import numpy as np

from .config import NS
from .events import Event, from_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS tape (
    recv_ts BIGINT NOT NULL,
    exch_ts BIGINT NOT NULL,
    seq     BIGINT NOT NULL,
    kind    VARCHAR NOT NULL,
    snapshot BOOLEAN NOT NULL DEFAULT FALSE,
    event   VARCHAR NOT NULL,
    raw     VARCHAR
);
CREATE TABLE IF NOT EXISTS gaps (
    recv_ts BIGINT NOT NULL,
    kind    VARCHAR NOT NULL,
    expected_seq BIGINT,
    got_seq BIGINT,
    reason  VARCHAR NOT NULL
);
"""


def connect(path: str | Path, read_only: bool = False):
    import duckdb

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def _day_bounds(day: str | date) -> tuple[int, int]:
    d = date.fromisoformat(str(day))
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(start.timestamp()) * NS, int((start + timedelta(days=1)).timestamp()) * NS


def read_range(path: str | Path, start_ns: int, end_ns: int, warmup_s: int = 300) -> list[Event]:
    """Events in [start_ns, end_ns), preceded by everything since the last book snapshot
    at or before start_ns - warmup_s (so the book is rebuilt from real messages)."""
    con = connect(path, read_only=True)
    try:
        lead = start_ns - warmup_s * NS
        snap = con.execute(
            "SELECT max(recv_ts) FROM tape WHERE kind='book' AND snapshot AND recv_ts <= ?", [lead]
        ).fetchone()[0]
        lo = snap if snap is not None else start_ns
        rows = con.execute(
            "SELECT event FROM tape WHERE recv_ts >= ? AND recv_ts < ? ORDER BY recv_ts, seq",
            [lo, end_ns],
        ).fetchall()
    finally:
        con.close()
    return [from_json(r[0]) for r in rows]


def read_day(path: str | Path, day: str | date) -> list[Event]:
    return read_range(path, *_day_bounds(day))


Day = tuple[str, list[Event], int]   # (label, events incl. warmup, trade_from_ns)


def iter_days(path: str | Path, start: str, end: str) -> Iterator[Day]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        ev = read_day(path, d)
        if ev:
            yield d.isoformat(), ev, _day_bounds(d)[0]
        d += timedelta(days=1)


def gaps(path: str | Path, start_ns: int = 0, end_ns: int = 2**62) -> list[tuple]:
    con = connect(path, read_only=True)
    try:
        return con.execute("SELECT * FROM gaps WHERE recv_ts >= ? AND recv_ts < ? ORDER BY recv_ts",
                           [start_ns, end_ns]).fetchall()
    finally:
        con.close()


def feed_delay_ms(events: list[Event]) -> dict[str, float]:
    """Measured exchange->us delay. Feed Latency.feed_ms with p95, not the mean."""
    lag = np.array([(e.recv_ts - e.exch_ts) / 1e6 for e in events])
    if len(lag) == 0:
        return {}
    return {"p50": float(np.percentile(lag, 50)), "p95": float(np.percentile(lag, 95)),
            "p99": float(np.percentile(lag, 99)), "max": float(lag.max())}
