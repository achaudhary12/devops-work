# Jev — a BTC-PERP market-making desk

We stand on both sides of the BTC-PERP book, sell to buyers a little higher, buy
from sellers a little lower, and keep the difference. Opus is the researcher.
Jev is the trader. **Only a human says go live.**

Read [`deaths.md`](deaths.md) first: the ten ways this desk loses money. Every
module's docstring names the deaths it prevents.

## Processes

Four processes, talking over Unix sockets in `var/bus/`. None shares memory, so
any one can die without taking the others with it.

| process | module | holds keys | job |
|---|---|---|---|
| recorder | `desk/recorder.py` | no | every raw book/trade/liq/funding message → DuckDB, gaps logged, never filled |
| jev | `desk/live.py` → `desk/jev.py` | no | prices and wants quotes; heartbeats the guard |
| guard | `desk/guard.py` | **yes** | the only one that talks to the exchange; enforces the leash; cancels everything on stale data / disconnect / silence from Jev |
| reporter | `desk/reporter.py` | no | live page on `127.0.0.1:8787` + Telegram pings |

## How Jev prices (`signals.py`, `pricing.py`)

```
weighted mid  = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
book pressure = (bid_size - ask_size) / (bid_size + ask_size)
centre        = weighted mid + pressure_coef * pressure - inventory * risk * vol²
full spread   = max(round-trip fees + 1 tick) + vol_spread_k * vol + report-card widening
```

`pressure_coef` is fitted by `fit.py` on past tape only. `replay()` raises
`PeekError` if the tape overlaps the fit window. The side that adds inventory
shrinks to zero at `max_inventory`.

**Pulls** (`pull.py`) cancel every quote when:
- one book side loses 60% in under 1s
- one-way aggressive flow reaches 4× normal
- a liquidation larger than `$X` prints
- the feed lags more than 250ms
- funding is less than 2 minutes away

Jev comes back only after 20s of normal market.

## Research loop

```bash
pip install -r requirements.txt
python -m pytest                      # 45 tests incl. the no-peeking test (~80s)

# fit on the past, judge on the future
python -m desk.fit         --config desk.toml --from 2026-09-20 --to 2026-09-30   # paste output into desk.toml
python -m desk.pass_or_die --config desk.toml --from 2026-10-01 --to 2026-10-06
python -m desk.report_card --config desk.toml --day 2026-10-06 --write   # -> var/widen.json
python -m desk.red_team   --config desk.toml --day 2026-10-04           # Sundays, via timer
```

- **Replay** (`replay.py`, `matching.py`) runs event by event:
  - A quote fills only after the visible queue ahead of it at that price has traded, or when a print trades through it.
  - Cancels ahead of us earn us nothing.
  - Every action lands after the measured order + feed delay.
  - Maker fees and funding are charged.
- **No peeking** (`tests/test_no_peeking.py`):
  - Every quote's inputs must be older than the quote, plus the delay.
  - A poisoned-future test corrupts every message after a cut point and requires every earlier quote to come out identical.
  - A self-test proves the poisoned-future test catches a trader that does look ahead.
- **Pass or die** (`pass_or_die.py`) runs only on unseen tape. Each line gets a 95% minute-block bootstrap CI:
  - 10s report card > 0 after fees
  - $ per 1000 quotes beats a symmetric quoter and beats doing nothing
  - max drawdown < 5%
  - no day loses more than 1%
  - average time back to flat < 90s
  - the edge lines hold separately on calm days and on violent days

  No violent days in the set means the version is unproven, so it dies.

## The ladder (`ladder.py`)

shadow 7 days → paper 7 days → **live $100** → double after 500 fills with a
positive report card → … Any losing week drops one rung.

- **Shadow vs replay:** `shadow.py` compares the shadow run with a replay of the same window. If they are more than 30% apart, replay is lying.
- **Going live:** the step onto real money needs `var/GO_LIVE`, which no code ever writes:
  ```toml
  approved_by = "your name"
  approved_at = "2026-10-21"
  max_inventory_usd = 400
  ```

## Every session

```bash
python -m desk.brief suggest      # drafts "feel" from the tape and max loss from guard.toml
python -m desk.brief write --feel "..." --max-loss 10 --stop "..."
```
No brief, or a brief looser than the guard → `desk.live` refuses paper/live:
*we do not trade today*.

## Dry run of the whole stack (synthetic feed, paper exchange)

```bash
export DESK_HOME=./var && mkdir -p var
cp desk.example.toml desk.toml
cp guard.example.toml var/guard.toml && chmod 600 var/guard.toml
python -m desk.brief --config desk.toml write --feel "dry run" --max-loss 10 --stop "any tape gap"
python -m desk.reporter --config desk.toml &
python -m desk.recorder --config desk.toml --synth &
python -m desk.guard    --config desk.toml --mode paper &
python -m desk.live     --config desk.toml --mode paper &
open http://127.0.0.1:8787
```

## Deploy

On a VPS near the exchange, use the systemd units in `deploy/` (`Restart=always`),
plus the timers for the nightly report card and the Sunday red team. Keep the screen
behind an SSH tunnel. `.env` holds a **trade-only, withdrawals-off, IP-whitelisted**
key, readable only by the `desk` user.

## Not done yet — on purpose

- **No exchange adapter.** `[exchange]` is unspecified. Implement `recorder.FeedAdapter` and `exchange.ExchangeClient` for the venue; until then `--synth` and `--mode paper` are the only paths, and `--mode live` raises.
- **`[$X]` liquidation threshold is unset.** Unset means Jev pulls on *every* liquidation. Set `pull.liq_notional_usd` from the venue's liquidation size distribution.
- **Measured delays are placeholders.** Use `tape.feed_delay_ms(...)["p95"]` and the guard's order RTT.
- **Synthetic tape is for plumbing, not for edge.** Pass-or-die only counts recorded tape.
