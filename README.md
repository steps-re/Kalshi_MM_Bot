# Kalshi MM Bot

Kalshi market-making workbench: view orderbooks, record and replay markets,
backtest, screen the exchange, and run live tests.

This is the Steps Ventures fork of
[nathanonderko/Kalshi_MM_Bot](https://github.com/nathanonderko/Kalshi_MM_Bot).

## How it ended (project closed 2026-08-19)

Two days of live measurement against the exchange's own ledger, ~$13 of a $50
account spent buying the answers. The verdict: **on Kalshi's fee schedule there
is no general edge, for a resting maker or a selective taker.** Pooled across
everything, even the strongest order-book imbalance averages -0.014c per trade.
A strategy that takes on imbalance without conditioning on entry price and market
phase loses.

Re-audited after close against 100+ hours of recorded books, with the scan's six
measurement defects fixed and a no-signal control band added. The sweeping
version of the verdict does not survive contact with the control, and the narrow
version is stronger than what was first published. Four corrections:

- The imbalance slope is **+0.224c +/- 0.058 per unit OBI** (clustered on the
  market), not the +0.85c first reported, and it is absent on ETH-daily. It is
  also one-sided: bid-heavy books predict, ask-heavy books do not.
- "Replicated across three independent periods" does not hold. Of eight
  pre-registered slices, five were absent from the true holdout and five of the
  eight tested had under 50% power. A null at 32% power refutes nothing.
- The original in-sample table rested on **two markets per venue**. `KXGOLD15M`
  had exactly two 15-minute windows in the data the scan read, and its published
  "+1.39c, t=2.0" treated 931 ticks inside two price paths as 931 independent
  draws. The recordings were recovered (they were in a second bucket that 403s on
  one of two accounts), and those venues now have 46-54 markets each.
- With a **control band** added (books with no imbalance, which carry no signal
  by construction), the picture sharpens: the imbalance signal is worth
  **+0.6c to +0.9c per trade** over a balanced book, monotone across 441 and 642
  markets. Taking starts about 0.9c under water, so the lift alone does not pay.
  One cell clears it and replicates on independent data - `KXBTCD`, 2-5c entries,
  last quarter hour before expiry - and it does **not** generalise to the
  15-minute family.

Full detail in [the audit section of
docs/research-notes.md](docs/research-notes.md).

- **The story, interactive:** https://kalshi-findings-737705633649.us-central1.run.app
  (start on "What we found" - includes how Nate's +$1.50 reconciles)
- **[docs/research-notes.md](docs/research-notes.md)** - the full research log,
  finding by finding, through the final chapter
- **[docs/playbook.md](docs/playbook.md)** - the venue-agnostic method: how to
  evaluate any order-book venue for market making in an evening
- **[LESSONS.md](LESSONS.md)** - the earlier long-form write-up (instrument
  bugs, backtest limits), kept as the record of the middle of the journey

## The fee arithmetic (measured, not the docs)

What the ledger actually charges - calibrated against every real fill
(`market/fees.py`, `calibrate_from_fills`):

    maker (resting order fills):  $0.00, always
    taker (crossing the spread):  0.07 * contracts * P * (1-P),
                                  rounded UP to the next $0.0001

Not the whole-cent rounding the docs describe, and makers trade free - the two
facts that decided everything. The taker fee peaks at P=$0.50 (1.75c/contract)
and collapses in the tails, so *where* a book trades sets the price of every
forced exit.

---

**Everything below this line is the project's earlier analysis, kept for the
record.** Parts of it were superseded by live measurement - notably, the fee
wall below assumed takers-pay-both-sides before maker-free was proven.

## The early fee-wall analysis

Because the fee is proportional to `P * (1 - P)`, it collapses in the tails.
Improving the touch where the spread leaves room for it, here is where the
arithmetic works:

| quoted spread | viable YES price band          |
| ------------- | ------------------------------ |
| 1c-3c         | at or below $0.077, or at or above $0.923 |
| 4c            | below $0.17 or above $0.83     |
| 5c            | below $0.31 or above $0.69     |
| 6c and wider  | anywhere                       |

A one-tick market cannot be improved - there is nowhere to stand between the
bid and the ask - so the whole spread is capturable by joining. That is why 1c
and 3c land in the same band.

**The tight, liquid, midpoint markets are structurally unquotable.** The edge,
if there is one, is in wide spreads and tail prices.

### What the live exchange actually offers

A complete scan of every open Kalshi market (2026-08-16), via `/events`:

- **84,181 open markets**; 8,061 with two-sided quotes and 24h volume of 50+
  contracts, carrying **66.0M contracts a day**.
- Under the *pessimistic* fee assumption (taker rate charged on both sides),
  **44% of liquid markets clear their own fee**, carrying 26% of the volume.
- The hit rate is 88% in the deep tails and 27% near the money — the tails
  dominate, but the rule is spread versus fee at that price, not "tails only".

At 10% participation the fee-permitted edge is roughly $30k/day. **That is a
ceiling on what the fee structure allows, not a forecast** — it assumes
capturing the quoted spread on every round trip with no adverse selection, and
concentrates in thinly traded wide-spread markets. Markout is what separates the
ceiling from reality.

> An earlier version of this README reported 2,504 markets and $64/day. That
> scan stopped paging early and covered ~3% of the exchange. See LESSONS.md §6.

### The other venue

Polymarket is the only other venue with open enough data to screen. **Every one
of its fee schedules sets `takerOnly: true`** — makers pay nothing and earn a
15-25% rebate — and its tick is 0.1c on most active markets. Run the same screen
there and every liquid market passes, which means the screen has stopped being
the binding analysis: the constraints there are adverse selection and
competition, not fees.

So the fee wall is a property of *one fee schedule*, not of prediction markets.
Which makes the Kalshi maker-fee question decisive: Kalshi's published schedule
charges takers, and public summaries say most standard markets carry **no maker
fee**. If that applies to the account, most of the exchange opens up.
`calibrate_from_fills` settles it with one session of real fills.

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
