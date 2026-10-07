"""The local queue between processes: Unix sockets, one JSON object per line.

  var/bus/feed.sock      recorder -> jev, guard       normalized market events
  var/bus/guard.sock     jev <-> guard                intents, heartbeats / fills, status
  var/bus/reporter.sock  anyone -> reporter           alerts, state for the screen

Every hub is owned by exactly one process. If a peer dies, its socket closes and
the owner notices immediately; nobody shares memory.

Prevents: D6 (a crashed process can't take the guard down with it).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import orjson

log = logging.getLogger("bus")
Handler = Callable[[dict, "Peer"], Awaitable[None]]


class Peer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    async def send(self, msg: dict) -> None:
        self.writer.write(orjson.dumps(msg) + b"\n")
        await self.writer.drain()

    def send_nowait(self, msg: dict) -> None:
        self.writer.write(orjson.dumps(msg) + b"\n")

    async def recv(self) -> dict | None:
        line = await self.reader.readline()
        return orjson.loads(line) if line else None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()


class Hub:
    """Server side. Broadcasts to every connected peer; hands inbound messages to `on_message`."""

    def __init__(self, path: Path, on_message: Handler | None = None,
                 on_disconnect: Callable[[Peer], Awaitable[None]] | None = None) -> None:
        self.path = Path(path)
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.peers: set[Peer] = set()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        self.path.chmod(0o600)

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        peer = Peer(r, w)
        self.peers.add(peer)
        try:
            while (msg := await peer.recv()) is not None:
                if self.on_message:
                    await self.on_message(msg, peer)
        except (ConnectionError, asyncio.IncompleteReadError, orjson.JSONDecodeError) as e:
            log.warning("peer dropped on %s: %s", self.path.name, e)
        finally:
            self.peers.discard(peer)
            peer.close()
            if self.on_disconnect:
                await self.on_disconnect(peer)

    def publish(self, msg: dict) -> None:
        data = orjson.dumps(msg) + b"\n"
        for p in list(self.peers):
            try:
                p.writer.write(data)
            except Exception:
                self.peers.discard(p)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for p in list(self.peers):
            p.close()


async def connect(path: Path, retry_s: float = 0.5, attempts: int | None = None) -> Peer:
    n = 0
    while True:
        try:
            r, w = await asyncio.open_unix_connection(str(path))
            return Peer(r, w)
        except (FileNotFoundError, ConnectionRefusedError):
            n += 1
            if attempts is not None and n >= attempts:
                raise
            await asyncio.sleep(retry_s)


def run(coro) -> None:
    """asyncio.run on uvloop when available."""
    try:
        import uvloop
        uvloop.run(coro)
    except ImportError:
        asyncio.run(coro)


async def messages(peer: Peer) -> AsyncIterator[dict]:
    while (m := await peer.recv()) is not None:
        yield m
