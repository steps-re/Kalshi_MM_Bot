# Kalshi MM Bot

Kalshi market-making workbench: view orderbooks, record and replay markets,
backtest, screen the exchange, and run live tests.

This is the Steps Ventures fork of
[nathanonderko/Kalshi_MM_Bot](https://github.com/nathanonderko/Kalshi_MM_Bot).

## Read this first: the fee arithmetic

Kalshi's trading fee is

    fee = round_up_to_next_cent(0.07 * contracts * P * (1 - P))

applied **per order**. At $0.50 that is **1.75c per side, 3.5c per round trip**,
against a **1c tick**. A market maker who captures an entire 2c spread at the
midpoint still loses 1.5c per contract per round trip. That is not a tuning
problem and no parameter fixes it.

Because the fee is proportional to `P * (1 - P)`, it collapses in the tails.
Improving the touch by a cent per side, here is where the arithmetic works:

| quoted spread | viable YES price band          |
| ------------- | ------------------------------ |
| 1c            | nowhere                        |
| 2c            | nowhere                        |
| 3c            | below $0.08 or above $0.92     |
| 4c            | below $0.17 or above $0.83     |
| 5c            | below $0.31 or above $0.69     |
| 6c and wider  | anywhere                       |

Reproduce with `python scripts/screen_markets.py --from-json <saved payload>`,
or against the live exchange with `--prod`.

**The tight, liquid, midpoint markets are structurally unquotable.** The edge,
if there is one, is in wide spreads and tail prices.

One caveat that changes everything and is worth one live session to settle:
this assumes the 0.07 formula applies to **maker** fills too. If your account
is billed a flat per-contract maker fee on a given market instead, the picture
is completely different. `calibrate_from_fills` in `sim/fees.py` reconciles the
model against the `fees_paid` Kalshi reports on real executions. Run it before
trusting any backtest.

## Setup

Requires Python 3.14+ and a `.env` file in the project root:

```env
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=secrets/Kalshi Private API Key.pem
```

```sh
uv sync          # or: python -m pip install -e .
uv run pytest -q
```

## Workflow

```sh
# 1. Which markets can pay their own fees?
python scripts/screen_markets.py --prod --min-volume 500

# 2. Record the promising ones (captures close times, needed for expiry logic)
python scripts/record_markets.py --prod TICKER1 TICKER2 --duration-sec 3600

# 3. What happened, and was it edge or luck?
python scripts/analyze_session.py recordings/<session>

# 4. Does it survive on data it was not fitted to?
python scripts/analyze_session.py recordings/* --walk-forward

# 5. Dry run against live data, no orders sent
python scripts/live_trade.py TICKER --strategy horizon

# 6. Live, small, with limits
python scripts/live_trade.py TICKER --strategy horizon --execute
```

The GUI workbench (`python scripts/orderbook_viewer.py`) still covers live
viewing, recording, replay, optimization and live trading in one window.

## Strategies

- **`horizon`** (default) - prices adverse selection off measured volatility,
  scales inventory risk toward the binary payoff as expiry approaches, ramps
  size with available edge, and refuses any order whose edge cannot cover its
  own fee. Goes reduce-only then flattens into the close.
- **`adaptive`** - the previous strategy, kept as a baseline.
- **`dumb`** - joins the touch. The control.

Compare against `dumb`, not against nothing. A strategy that beats no baseline
has not been shown to do anything.

## What to read in a report

`scripts/analyze_session.py` prints, in order of how much you should trust it:

1. **Markout** - did the market move against us right after we traded? On a ten
   minute session the P&L is noise and the markout is not. Negative markout
   means we are being picked off, and no spread fixes that.
2. **Markout by time to close** - where in a market's life the danger is. This
   is the quantified version of "the last minutes of a 15-minute BTC market
   behave differently".
3. **P&L attribution** - spread capture versus inventory drift versus fees.
   Money made from an accidental position is not market making.
4. **Drawdown** - a dollar of profit against five of drawdown does not scale.
5. **Net liquidation** - fees paid, leftover inventory valued by walking the
   book it would actually be sold into, not marked at mid.

## Risk limits

`live/risk.py` enforces position, session loss, drawdown-from-peak, order rate,
consecutive rejections, and **feed silence**. The last one matters most:
resting orders do not cancel themselves when the websocket stalls, so a quiet
feed is the most dangerous state the system can be in. The kill switch latches.

```python
from kalshi_mm_bot.live.risk import RiskLimits
limits = RiskLimits.conservative(contracts=10, loss_dollars=5.0)
```

## Backtests are net, and validated out of sample

- P&L is net of fees. `gross_*` fields show the difference.
- Inventory is marked at the price it could be unwound into, walking the book.
- The optimizer maximises **net liquidation**, and breaks ties toward ending
  flat rather than toward trading more.
- `sim/validation.py` runs expanding-window walk-forward. Fit on earlier
  recordings, score on later ones, and report the gap. An in-sample result with
  nothing left out of sample is a description of that recording's noise.

## Live results

Earlier anecdotal runs (four 10-minute snippets inside 15-minute markets,
adaptive +$0.51 total against dumb -$1.62) are **not evidence of edge**. Four
observations of a dollar-scale quantity cannot distinguish a working strategy
from a coin flip, and those runs predate fee accounting in the simulator. Treat
them as a smoke test that the plumbing works.

Live execution can lose money. Keep order sizes small, set risk limits, stop
before close while testing, and verify no bot-prefixed orders are left resting
after shutdown.
