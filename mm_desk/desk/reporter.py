"""The screen and the pager.

One live page (http://127.0.0.1:8787, put it behind an SSH tunnel, never public):
our quotes on the book, inventory, live report card by bucket, rung on the
ladder, and distance to every leash limit.

Telegram ping on every pull, every leash hit, every refused leash change, every
flatten / cancel-all, every tape gap, and every losing hour.
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

Prevents: nothing on its own — it makes every other prevention visible, so a
human sees D1-D10 happening instead of reading about it tomorrow.

    python -m desk.reporter
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

from . import bus
from .config import NS, DeskConfig
from .exchange import load_env
from .jev import bucket_key
from .ladder import Ladder
from .pull import reason_keys

log = logging.getLogger("reporter")
PING_KINDS = {"pull", "leash_hit", "leash_refused", "flatten", "cancel_all", "tape_gap",
              "losing_hour", "feed_disconnected", "exchange_disconnected"}


class Telegram:
    def __init__(self, token: str | None, chat: str | None) -> None:
        self.token, self.chat = token, chat
        self.q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def ping(self, text: str, key: str | None = None, quiet_s: float = 60.0) -> None:
        """Identical pings (same key) within quiet_s are collapsed into a count on the next one."""
        if key is not None:
            now = time.monotonic()
            last = self._last.get(key)
            if last is not None and now - last < quiet_s:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            self._last[key] = now
            n = self._suppressed.pop(key, 0)
            if n:
                text += f" (+{n} more like this in the last {quiet_s:.0f}s)"
        if not self.token:
            log.info("PING (telegram not configured): %s", text)
            return
        try:
            self.q.put_nowait(text)
        except asyncio.QueueFull:
            pass

    async def run(self) -> None:
        while True:
            text = await self.q.get()
            data = urllib.parse.urlencode({"chat_id": self.chat, "text": text[:4000]}).encode()
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            try:
                await asyncio.to_thread(urllib.request.urlopen, url, data, 10)
            except Exception as e:
                log.warning("telegram send failed: %s", type(e).__name__)
            await asyncio.sleep(1.0)   # Telegram rate limit; pings queue up, never drop silently


class Reporter:
    def __init__(self, cfg: DeskConfig) -> None:
        self.cfg = cfg
        env = load_env()
        self.tg = Telegram(env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"))
        self.hub = bus.Hub(cfg.paths.bus_dir / "reporter.sock", self.on_msg)
        self.jev: dict = {}
        self.guard: dict = {}
        self.alerts: deque[dict] = deque(maxlen=50)
        self.mids: deque[tuple[int, float]] = deque(maxlen=7200)
        self.unscored: deque[dict] = deque()
        self.card: dict[str, list[float]] = {}     # bucket -> [n, sum_bps]
        self.hour_start_pnl: float | None = None
        self.hour = None

    async def on_msg(self, m: dict, peer) -> None:
        t = m.get("type")
        if t == "jev_state":
            self.jev = m
            if m.get("best_bid") and m.get("best_ask"):
                self.mids.append((m["ts"], (m["best_bid"] + m["best_ask"]) / 2))
            self._score()
        elif t == "guard_status":
            self.guard = m
            self._hourly(m.get("day_pnl", 0.0))
        elif t == "fill":
            m["vol"] = self.jev.get("vol") or 0.0
            self.unscored.append(m)
        elif t == "alert":
            m.setdefault("ts", time.time_ns())
            self.alerts.appendleft(m)
            if m.get("kind") in PING_KINDS:
                detail = {k: v for k, v in m.items() if k not in ("type", "kind", "src", "ts")}
                key = f"{m['kind']}:{','.join(reason_keys(m.get('reason')))}"
                self.tg.ping(f"[{self.cfg.market.symbol}] {m['kind']} ({m.get('src')}) {detail}", key=key)

    def _score(self) -> None:
        """10s report card on live fills, by bucket. Same bps-after-fees definition as report_card."""
        if not self.mids:
            return
        now = self.mids[-1][0]
        while self.unscored and self.unscored[0]["ts"] + 10 * NS <= now:
            f = self.unscored.popleft()
            later = next((m for ts, m in self.mids if ts >= f["ts"] + 10 * NS), None)
            if later is None:
                continue
            bps = (f["side"] * (later - f["price"]) - f["fee"] / f["size"]) / f["price"] * 1e4
            k = bucket_key(f["ts"], f["size"], f["vol"], self.cfg)
            n, s = self.card.get(k, [0, 0.0])
            self.card[k] = [n + 1, s + bps]

    def _hourly(self, day_pnl: float) -> None:
        h = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        if self.hour is None:
            self.hour, self.hour_start_pnl = h, day_pnl
        elif h != self.hour:
            delta = day_pnl - (self.hour_start_pnl or 0.0)
            if delta < 0:
                self.alerts.appendleft({"type": "alert", "kind": "losing_hour", "ts": time.time_ns(),
                                        "hour": self.hour, "pnl": delta})
                self.tg.ping(f"[{self.cfg.market.symbol}] losing hour {self.hour}: ${delta:,.2f}")
            # day_pnl resets at UTC midnight; restart the hour baseline from wherever it is now
            self.hour, self.hour_start_pnl = h, day_pnl

    def state(self) -> dict:
        lad = Ladder(self.cfg)
        return {"jev": self.jev, "guard": self.guard, "alerts": list(self.alerts),
                "card": {k: {"n": n, "bps": s / n} for k, (n, s) in sorted(self.card.items())},
                "ladder": {"rung": lad.name, "inventory_usd": lad.inventory_usd,
                           "days": round(lad.days_on_rung(), 2), "good_fills": lad.state.good_fills}}

    async def http(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line = (await r.readline()).decode()
            while (await r.readline()) not in (b"\r\n", b"\n", b""):
                pass
            path = line.split(" ")[1] if " " in line else "/"
            if path.startswith("/state.json"):
                body, ctype = json.dumps(self.state(), default=str).encode(), "application/json"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            w.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
            await w.drain()
        finally:
            w.close()

    async def run(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        await self.hub.start()
        await asyncio.start_server(self.http, host, port)
        log.info("screen on http://%s:%d", host, port)
        await self.tg.run()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jev desk</title><style>
:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--line:#30363d;--good:#3fb950;--bad:#f85149;--warn:#d29922}
body{margin:0;background:var(--bg);color:var(--fg);font:14px ui-monospace,Menlo,monospace}
main{max-width:1100px;margin:auto;padding:16px;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
h2{margin:0 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.big{font-size:22px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse}td{padding:2px 4px;border-bottom:1px solid var(--line)}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar>i{display:block;height:100%}
</style></head><body><main>
<section><h2>Quotes on the book</h2><div id="q"></div></section>
<section><h2>Inventory &amp; PnL</h2><div id="inv"></div></section>
<section><h2>Leash</h2><div id="leash"></div></section>
<section><h2>Ladder</h2><div id="lad"></div></section>
<section style="grid-column:1/-1"><h2>Report card (10s, bps after fees)</h2><table id="card"></table></section>
<section style="grid-column:1/-1"><h2>Alerts</h2><table id="al"></table></section>
</main><script>
const f=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
async function tick(){try{const s=await (await fetch('state.json')).json();const j=s.jev||{},g=s.guard||{};
document.getElementById('q').innerHTML=j.pulled?`<div class="big warn">PULLED</div><div>${j.pulled}</div>`:
`<table><tr><td>our ask</td><td class="bad">${f(j.ask,1)}</td><td>${f(j.ask_size,3)}</td></tr>
<tr><td>best ask</td><td>${f(j.best_ask,1)}</td><td></td></tr><tr><td>best bid</td><td>${f(j.best_bid,1)}</td><td></td></tr>
<tr><td>our bid</td><td class="good">${f(j.bid,1)}</td><td>${f(j.bid_size,3)}</td></tr></table><div>mode ${j.mode||'—'} · vol $${f(j.vol)}/√s</div>`;
const pnl=g.day_pnl;document.getElementById('inv').innerHTML=`<div class="big">${f(g.position??j.inventory,4)} BTC</div>
<div class="${pnl<0?'bad':'good'}">day PnL $${f(pnl)}</div><div>open orders ${g.open_orders??'—'} · ${g.halted?'<b class=bad>HALTED '+g.halted+'</b>':''} ${g.stale?'<b class=warn>STALE '+g.stale+'</b>':''}</div>`;
document.getElementById('leash').innerHTML=Object.entries(g.limits||{}).map(([k,[v,c]])=>{const p=Math.min(100,100*v/c);
return `<div>${k} ${f(v,4)} / ${f(c,4)}</div><div class="bar"><i style="width:${p}%;background:var(--${p>80?'bad':p>50?'warn':'good'})"></i></div>`}).join('')||'guard not reporting';
const l=s.ladder;document.getElementById('lad').innerHTML=`<div class="big">${l.rung}</div><div>$${l.inventory_usd} max inventory · ${l.days} days · ${l.good_fills} good fills</div>`;
document.getElementById('card').innerHTML='<tr><td>bucket</td><td>n</td><td>bps</td></tr>'+Object.entries(s.card).map(([k,v])=>`<tr><td>${k}</td><td>${v.n}</td><td class="${v.bps<0?'bad':'good'}">${f(v.bps,3)}</td></tr>`).join('');
document.getElementById('al').innerHTML=s.alerts.map(a=>`<tr><td>${new Date(a.ts/1e6).toISOString().slice(11,19)}</td><td>${a.kind}</td><td>${a.src||''}</td><td>${a.reason||''}</td></tr>`).join('');
}catch(e){}}
tick();setInterval(tick,1000);
</script></body></html>"""


def main() -> None:
    import argparse

    from . import config

    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bus.run(Reporter(config.load(a.config)).run(port=a.port))


if __name__ == "__main__":
    main()
