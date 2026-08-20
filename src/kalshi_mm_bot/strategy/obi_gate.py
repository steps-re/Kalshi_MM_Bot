"""Pull the endangered quote when the book says it is about to be picked off.

The one validated signal in this project is top-of-book imbalance. Stated at the
exit convention that matches what a maker actually gets, from the round-2 table
in `docs/adversarial/claims2.md` (recovered corpus, one number per market,
nothing selected, 30s, SE ~0.039):

    band          TOUCH exit   MID-TO-MID
    ctrl <0.2       -0.905c      +0.013c
    >0.9            -0.014c      +0.861c

Both columns matter and they say different things.

The mid-to-mid column is a real forecast. The obvious objection - that at
extreme imbalance you consume the thin side, makers pull back, and the mid
ratchets up on a widening spread nobody can trade against - was tested by
decomposing the move into the side we cross and the side that must come to us
(`docs/adversarial/README.md`, round 2). The THICK side lifts +0.800c from
control to extreme, so spread widening accounts for only about 0.16c of the
0.86c. The bid genuinely rises after a bid-heavy signal. The forecast is real.

The TOUCH column is what a maker actually collects, because a resting quote
fills at the touch. Its *lift* is +0.891c, monotone across 641 to 658 markets
per band and replicating on the independent archive corpus. Its *level* at
|OBI| > 0.9 is -0.014c: still, marginally, under water.

Quote the touch level in this file, not the mid-to-mid one. Earlier drafts led
with +0.86c, which is the right number for "does the book predict the next
move" and the wrong one for "what does our resting quote earn."

**Buying that move is not refuted.** An earlier draft of this file said it was,
citing `exit_fill_study.py`. That script's fill detector missed the ordinary
case - a marketable buy consuming our resting level while the bid stays below it
- and re-priced every missed fill as a forced cross, which manufactured the
negative result. Corrected and bracketed, the same 598 triggers give +0.6c to
+1.1c per trade at the upper fill bound and -0.4c to 0.0c at the conservative
one. The round trip is undecided, and live orders decide it, not more book
replay. (On the archive the bid-heavy half carries roughly 2.5x the ask-heavy
half, which is what the one-sidedness finding predicts, but that split does not
replicate on the recovered corpus's 19 markets. Treat it as a hypothesis.)

So this wrapper is not the last idea standing. It is a different use of the
same forecast: don't pay for the move, stop being on the wrong side of it. A
maker's only loss is adverse selection - the resting quote that fills seconds
before the price moves through it. Gating those fills converts the forecast into
avoided losses, which pay no spread and no fee.

That is the argument. The offline evidence for it is point 5 below, and it is
weak.

## What is blocked, precisely

When OBI > +threshold (price about to rise), a fill on our resting SELL that
*creates or extends a short* is the adverse case. If we are long, that same fill
merely banks the spread a moment early - forgone upside, not a loss - and
blocking it would strand inventory. So:

    OBI > +t: block SELL intents only while position <= 0
    OBI < -t: block BUY  intents only while position >= 0

Risk-reducing quotes always pass. The gate holds for seconds (an OBI episode),
not minutes, and composes under `phased:` which owns the end-of-life behaviour.

## What is NOT established, and must not be implied by this file

1. **The buy half has no measured support.** The audit found the signal is
   one-sided: relative to a balanced book, bid-heavy predicts +0.68c at 5s and
   ask-heavy predicts +0.02c (`docs/research-notes.md`, "it is one-sided").
   Blocking SELLs on a bid-heavy book is the evidence-backed half. Blocking BUYs
   on an ask-heavy book is symmetry, not measurement. `gate_buys=0` runs only
   the supported half. It is 1 by default solely because that is what the live
   A/B arm has been running since 2026-08-19, and changing a default mid-flight
   would silently redefine the treatment.
2. **This design lost badly once already.** "v1 withhold exposed side" is in the
   notes at 977 fills against a 3,582-fill baseline, $3.85 captured against
   $15.34, inventory -$14.78, flat on 1 of 7 runs. This file is that design plus
   the increases-only carve-out. The carve-out is the whole hypothesis and it
   has never been backtested. `gate_dose_study.py` cannot test it either: it
   scores a veto against a fill history in which nothing was vetoed, so it sees
   none of the inventory consequence that sank v1.
3. **The offline study and this gate do not read the same variable.** Here OBI
   is the self-book, instantaneous, on every orderbook event. There it is a
   separate collector's book, joined with up to ~2s of staleness and 35%
   coverage. Nothing has measured how often the two agree.
4. **A blocked intent is a real cancel** (`sync_quotes` cancels any resting order
   whose intent disappears), so re-creating it costs queue position. No study in
   this repo prices that, and this project has an entire script devoted to how
   much queue position matters.
5. **The dose study does not support 90 over anything else.** Re-run with the
   joins fixed and errors clustered on the market, selectivity is flat from 50%
   to 95% - every threshold blocks fills 1.5c to 2.2c worse than it keeps, with
   overlapping error bars - the strongest reading in the whole run is its own
   post-fill placebo, and the sell half, the half with measured support, shows a
   gradient of +0.065c +/-1.392. See `docs/research-notes.md` and
   `docs/adversarial/gate-dose-study.txt`.
6. **The live A/B agrees with it.** 18 cycles, 9 per arm, 2026-08-19 into
   08-20: control +$0.007/cycle, gated -$0.051/cycle, Welch t=0.33, paired
   difference -$0.058/cycle at t=-0.35. The gated arm marked out *worse*
   (+0.434c against +0.567c) and took *more* fills (471 against 401). Not
   significant either way - 9 cycles per arm cannot see an effect this size,
   and ~46 per arm would be needed - but there is no sign of the effect in
   either the offline study or the live run. Nothing here changed behaviour, so
   that result still applies to this code.

`threshold_hundredths <= 0` disables the gate entirely - the control arm. Run
`obigate:phased:adaptive` with `--ab-arms 'obi_gate=0;obi_gate=90'` and the two
arms differ by nothing but this file's if-statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.strategy.types import (
    PortfolioView,
    QuoteIntent,
    Strategy,
    StrategyContext,
)


@dataclass
class OBIGatedStrategy:
    """Wraps a strategy, dropping risk-adding quotes on the endangered side."""

    inner: Strategy
    # Hundredths, so it survives the int-only adaptive-param pipeline: 90 means
    # gate at |OBI| >= 0.90. Zero or negative disables the gate (control arm).
    threshold_hundredths: int = 90
    # Whole contracts of combined touch depth required before the ratio is
    # believed. With no floor a 1-lot ask against a 19-lot bid scores 0.90
    # mechanically - defect #5 in `taker_extract.py`, which records touch sizes
    # precisely so a floor can be applied. 0 keeps the unfloored behaviour the
    # live A/B arm started with; raising it starts a new experiment.
    min_touch_contracts: int = 0
    # 1 blocks both sides, 0 blocks only SELLs on a bid-heavy book. See point 1
    # in the module docstring: only the sell half has measured support.
    gate_buys: int = 1
    blocked_sells: int = field(default=0, init=False)
    blocked_buys: int = field(default=0, init=False)
    # (ticker, quote_id) pairs already counted in the episode currently running
    # on that ticker. Without this the counters increment once per orderbook
    # EVENT, so a single suppressed quote riding a 3-second episode across 40
    # book updates reports 40 blocks, and any "fills avoided" read off these
    # numbers is inflated by the event rate.
    _episode_side: dict[str, str] = field(default_factory=dict, init=False)
    _episode_blocked: set[tuple[str, str]] = field(default_factory=set, init=False)

    @property
    def name(self) -> str:
        return f"obigate{self.threshold_hundredths}_{getattr(self.inner, 'name', 'strategy')}"

    def _end_episode(self, market_ticker: str) -> None:
        if self._episode_side.pop(market_ticker, None) is None:
            return

        self._episode_blocked = {
            key for key in self._episode_blocked if key[0] != market_ticker
        }

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        intents = self.inner.on_orderbook(
            context=context,
            market_ticker=market_ticker,
            orderbook=orderbook,
            portfolio=portfolio,
        )
        ticker = str(market_ticker)

        if self.threshold_hundredths <= 0 or not intents:
            self._end_episode(ticker)
            return intents

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_bid is None or best_ask is None:
            self._end_episode(ticker)
            return intents

        bid_size = orderbook.bids[best_bid]
        ask_size = orderbook.asks[best_ask]
        total = bid_size + ask_size

        if total <= 0 or total < self.min_touch_contracts * COUNT_SCALE:
            self._end_episode(ticker)
            return intents

        obi = (bid_size - ask_size) / total
        threshold = self.threshold_hundredths / 100.0

        if obi >= threshold:
            endangered = "sell"
        elif obi <= -threshold and self.gate_buys:
            endangered = "buy"
        else:
            self._end_episode(ticker)
            return intents

        # A new episode whenever the endangered side changes, so a book that
        # flips from bid-heavy to ask-heavy counts as two.
        if self._episode_side.get(ticker) != endangered:
            self._end_episode(ticker)
            self._episode_side[ticker] = endangered

        position = portfolio.position(market_ticker)
        kept: list[QuoteIntent] = []

        for intent in intents:
            if intent.action != endangered:
                kept.append(intent)
                continue

            # Signing off `action` alone is only correct while every strategy
            # quotes YES. A `buy` of NO is economically a short of YES, so it
            # would be endangered by the opposite book. Rather than guess, pass
            # anything that is not plainly a YES quote through untouched.
            if getattr(intent, "side", "yes") != "yes":
                kept.append(intent)
                continue

            # A fill that reduces the position is always allowed: stranding
            # inventory is the measured worse failure. Only fills that would
            # open or extend a position against the predicted move are blocked.
            increases = (
                position <= 0 if endangered == "sell" else position >= 0
            )

            if not increases:
                kept.append(intent)
                continue

            key = (ticker, str(intent.quote_id))

            if key in self._episode_blocked:
                continue

            self._episode_blocked.add(key)

            if endangered == "sell":
                self.blocked_sells += 1
            else:
                self.blocked_buys += 1

        return tuple(kept)
