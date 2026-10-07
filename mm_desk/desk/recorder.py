"""The recorder. From minute one, save every raw book update, trade, liquidation
and funding print with exchange time and our receive time. This tape is the only
truth we test on. Never resample it, never fill gaps silently.

  * recv_ts is stamped the moment a frame comes off the socket, before parsing
  * raw frame + normalized event are both stored
  * book sequence breaks and disconnects are written to `gaps`, logged loudly,
    pushed to the reporter, and followed by a fresh snapshot — never bridged
  * normalized events are re-published on the feed bus for jev and the guard

Prevents: D7 (the only truth), D4 (gaps and lag are visible).

    python -m desk.recorder            # needs an exchange adapter, see exchange.py
    python -m desk.recorder --synth    # dry-run with the synthetic feed
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from typing import AsyncIterator, Protocol

import orjson

from . import bus, tape
from .config import DeskConfig
from .events import BookUpdate, Event

log = logging.getLogger("recorder")


class FeedAdapter(Protocol):
    """One per exchange. Must stamp nothing itself: the recorder owns recv_ts."""

    async def frames(self) -> AsyncIterator[tuple[int, bytes]]:
        """Yield (recv_ts_ns, raw_frame) forever; raise ConnectionError on disconnect."""
        ...

    def normalize(self, raw: bytes, recv_ts: int) -> list[Event]:
        """Raw frame -> zero or more events. Book `seq` must be contiguous per stream."""
        ...

    async def resync(self) -> None:
        """Request a fresh book snapshot (after a gap or reconnect)."""
        ...


class Recorder:
    def __init__(self, cfg: DeskConfig, feed: FeedAdapter, flush_rows: int = 2000,
                 flush_s: float = 1.0) -> None:
        self.cfg, self.feed = cfg, feed
        self.con = tape.connect(cfg.paths.tape_db)
        self.hub = bus.Hub(cfg.paths.bus_dir / "feed.sock")
        self.rows: list[tuple] = []
        self.flush_rows, self.flush_s = flush_rows, flush_s
        self.last_book_seq: int | None = None
        self._last_flush = time.monotonic()
        self.stats = {"events": 0, "gaps": 0}

    def gap(self, recv_ts: int, kind: str, expected: int | None, got: int | None, reason: str) -> None:
        self.stats["gaps"] += 1
        self.flush()
        self.con.execute("INSERT INTO gaps VALUES (?, ?, ?, ?, ?)", [recv_ts, kind, expected, got, reason])
        log.error("TAPE GAP %s %s expected=%s got=%s", kind, reason, expected, got)
        self.hub.publish({"type": "gap", "recv_ts": recv_ts, "kind": kind, "reason": reason})

    def record(self, raw: bytes, events: list[Event]) -> None:
        raw_s = raw.decode("utf-8", "replace")
        for i, ev in enumerate(events):
            if isinstance(ev, BookUpdate):
                if ev.snapshot:
                    self.last_book_seq = ev.seq
                elif self.last_book_seq is not None and ev.seq != self.last_book_seq + 1:
                    self.gap(ev.recv_ts, "book", self.last_book_seq + 1, ev.seq, "seq_break")
                    self.last_book_seq = None          # untrusted until the next snapshot
                    asyncio.get_running_loop().create_task(self.feed.resync())
                    continue
                elif self.last_book_seq is None:
                    continue                           # deltas without a snapshot are meaningless
                else:
                    self.last_book_seq = ev.seq
            d = asdict(ev)
            self.rows.append((ev.recv_ts, ev.exch_ts, ev.seq, ev.kind,
                              bool(getattr(ev, "snapshot", False)),
                              orjson.dumps(d).decode(), raw_s if i == 0 else None))
            self.hub.publish({"type": "event", "event": d})
            self.stats["events"] += 1
        if len(self.rows) >= self.flush_rows or time.monotonic() - self._last_flush > self.flush_s:
            self.flush()

    def flush(self) -> None:
        if self.rows:
            self.con.executemany("INSERT INTO tape VALUES (?, ?, ?, ?, ?, ?, ?)", self.rows)
            self.rows.clear()
        self._last_flush = time.monotonic()

    async def run(self) -> None:
        await self.hub.start()
        backoff = 0.5
        while True:
            try:
                async for recv_ts, raw in self.feed.frames():
                    backoff = 0.5
                    try:
                        events = self.feed.normalize(raw, recv_ts)
                    except Exception as e:   # unknown frame: keep the raw bytes, say so
                        log.exception("normalize failed")
                        self.rows.append((recv_ts, recv_ts, -1, "unparsed", False, "{}", raw.decode("utf-8", "replace")))
                        self.gap(recv_ts, "parse", None, None, f"normalize_error:{type(e).__name__}")
                        continue
                    self.record(raw, events)
                return                      # finite feed (synthetic) ended
            except ConnectionError as e:
                now = time.time_ns()
                self.gap(now, "all", None, None, f"disconnect:{e}")
                self.last_book_seq = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self.flush()


class SynthFeed:
    """Plays synthetic tape in real time, for dry runs of the whole stack."""

    def __init__(self, seconds: float = 3600, seed: int = 0, speed: float = 1.0) -> None:
        from .synth import synth_tape
        self.events, self.speed = synth_tape(seconds, seed=seed, start_ns=time.time_ns()), speed

    async def frames(self) -> AsyncIterator[tuple[int, bytes]]:
        from dataclasses import replace

        from .events import Funding, to_json
        # re-anchor exchange time to *now*: generating the tape takes seconds
        t0, w0, wall0 = self.events[0].exch_ts, time.monotonic_ns(), time.time_ns()
        for ev in self.events:
            wait = (ev.exch_ts - t0) / self.speed - (time.monotonic_ns() - w0)
            if wait > 0:
                await asyncio.sleep(wait / 1e9)
            shift = wall0 - t0
            ev = replace(ev, exch_ts=ev.exch_ts + shift)
            if isinstance(ev, Funding):
                ev = replace(ev, next_funding_ts=ev.next_funding_ts + shift)
            yield time.time_ns(), to_json(ev)

    def normalize(self, raw: bytes, recv_ts: int) -> list[Event]:
        from dataclasses import replace

        from .events import from_json
        return [replace(from_json(raw), recv_ts=recv_ts)]

    async def resync(self) -> None:
        return None


def main() -> None:
    import argparse

    from . import config
    from .exchange import feed_adapter

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--synth", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(a.config)
    feed = SynthFeed() if a.synth else feed_adapter(cfg)
    bus.run(Recorder(cfg, feed).run())


if __name__ == "__main__":
    main()
