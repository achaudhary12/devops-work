# deaths.md — how this desk loses money

Written before any code. Every module in `mm_desk/` names the deaths it
prevents in its docstring (`Prevents: D1, D4 ...`). If a module prevents nothing
on this list, it should not exist. If we find a new way to lose money, it goes
here first, then into code.

---

## D1. Adverse selection — we get filled right before the price runs through us
Someone who knows more than us lifts our ask a moment before the price jumps.
Every fill looks like spread captured; the 10s markout says we sold the bottom.

- Guard rails: centre skewed by book pressure (`pricing.py`), pull on one-way
  aggression / book collapse (`pull.py`), per-fill markouts at 1s/10s/60s and
  bucket widening (`report_card.py`), red-team spoof/sweep tapes (`red_team.py`).

## D2. Inventory runs away — we pile up on one side and the market keeps going
We buy every dip in a falling market. Spread income is linear, inventory loss
is not.

- Guard rails: centre = weighted mid − inventory·risk·vol² (`pricing.py`),
  adding-side size shrinks to zero at max inventory (`pricing.py`), hard
  inventory cap in the guard process (`guard.py`), pass-or-die requires mean
  time-back-to-flat < 90s (`pass_or_die.py`).

## D3. Fees eat the spread
A 1-tick spread on a venue charging maker fees is a guaranteed loss after the
round trip.

- Guard rails: full spread never below round-trip fees + 1 tick
  (`pricing.py`), every replay fill charged real fees and funding (`replay.py`),
  report card is scored *after* fees (`report_card.py`), pass-or-die must beat a
  dumb symmetric quoter and doing nothing (`pass_or_die.py`).

## D4. Stale data — we quote a price that no longer exists
The feed lags 400ms, we think the mid is 60,000 and it's already 60,050. We
are a free option for anyone with a faster feed.

- Guard rails: pull when exchange→receive lag > 250ms (`pull.py`), guard
  cancels everything on stale heartbeat or disconnect (`guard.py`), every input
  carries a timestamp and the no-peeking test proves quotes only use the past
  (`signals.py`, `tests/test_no_peeking.py`), replay charges measured latency on
  every action (`replay.py`).

## D5. Liquidation cascade fills every order we have
A big liquidation sweeps the book; every resting bid gets hit on the way down
and we end at max inventory on the wrong side.

- Guard rails: pull on any liquidation > `$X` notional (`pull.py`), pull on
  one book side losing 60% in < 1s (`pull.py`), guard inventory cap and daily
  loss cap with flatten (`guard.py`).

---

## Our own additions

## D6. The strategy process dies with orders resting
Jev crashes, hangs or loses its socket; its orders keep sitting on the book
with nobody pricing them.

- Guard rails: one process per job (recorder, jev, guard, reporter); the guard alone holds the trade
  keys and cancels everything when the Jev heartbeat stops or the exchange
  connection drops (`guard.py`); auto-restart via systemd (`deploy/`).

## D7. Lying backtest — research peeks or the fill model is too kind
Replay that fills us at the touch with no queue, uses the close of the bar we're
trading in, or fits coefficients on the test window. It prints money; live bleeds.

- Guard rails: one pricing code path for research, replay, shadow and live
  (`jev.py`); fills only after the visible queue ahead has traded (`replay.py`);
  coefficients fit on tape strictly before the test window and the replay
  refuses overlapping windows (`fit.py`, `replay.py`); no-peeking test
  (`tests/test_no_peeking.py`); shadow vs replay within 30% or replay is lying
  (`shadow.py`).

## D8. Funding print against our inventory
We carry a position across the funding timestamp and pay the rate on the full
notional, wiping out a day of spread.

- Guard rails: pull 2 minutes before funding (`pull.py`), replay charges funding
  on inventory held at the print (`replay.py`).

## D9. Runaway order loop / rate limits
A bug requotes 500 times a second; the exchange bans the key mid-position, and
now we can't cancel.

- Guard rails: requote only on ≥ 1 tick or material size change (`orders.py`),
  token-bucket max orders/sec in the guard (`guard.py`).

## D10. Someone (including a model) loosens the leash
A prompt, a config edit or a "temporary" override raises max inventory on a
bad day.

- Guard rails: limits live only in the guard process and its own file, loaded
  once at start; any request to change them over the bus is refused and written
  to `leash.log` (`guard.py`). Going live requires a human-signed `GO_LIVE`
  file (`ladder.py`). No morning brief, no trading (`brief.py`).

---

## Red team rules
Appended automatically by `python -m desk.red_team` whenever an attack makes
money against Jev in replay. Each rule must be turned into a code change and a
regression tape before the next promotion on the ladder.

<!-- red-team rules below this line -->
