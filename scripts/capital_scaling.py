"""What does a bigger account actually unlock on Kalshi? Arithmetic, not vibes.

    python scripts/capital_scaling.py

Kalshi is FULLY COLLATERALISED - no leverage, no portfolio margin across
strikes (assumed here; see the note at the end, it is worth verifying).
Selling a contract at 3c means posting the other 97c against the loss. So a
strategy's return is not `edge / price`, it is `edge / collateral`, and the
tail trades this project is chasing are the most collateral-hungry shape on
the exchange.

Three questions this answers, each with the project's own measured inputs:

1. Return on capital per settlement, if the calibration edge is real.
2. How much capital it takes to survive the correlated bad day, which is the
   real constraint on a tail-selling book - not the average.
3. Which strategies unlock at which balance, and which are edge-dead so that
   no balance helps.

Measured inputs, all from this repo: tail SELL +1.5c/contract and fave BUY
+1.0c/contract (12 days, day-clustered, fees in); maker capture +0.4c/fill
against ZERO realised P&L over ~68 live cycles; hedge notional from
hedge_economics.py.
"""

from __future__ import annotations

import math

TAIL_PRICE = 0.03          # sell YES here
TAIL_EDGE_C = 1.5          # measured, per contract
FAVE_PRICE = 0.97          # buy YES here (a 3c long-equivalent)
FAVE_EDGE_C = 1.0          # measured, per contract
BALANCES = (35, 500, 2_500, 10_000, 50_000, 250_000)


def payoff_stats(price: float, edge_c: float, selling: bool):
    """Mean, sd and collateral for one contract, in cents.

    Selling YES at `price`: keep `price` unless YES happens, then lose the
    complement. The true YES rate is backed out of the measured edge, which is
    the honest way round - we measured P&L, not probability.
    """

    price_c = price * 100.0
    win = price_c                      # cents kept if it expires worthless
    lose = 100.0 - price_c             # cents lost if it comes in

    if selling:
        # edge = (1-q)*win - q*lose  ->  solve for q
        q = (win - edge_c) / (win + lose)
        collateral = lose
        outcomes = ((1 - q, win), (q, -lose))
    else:
        # Buying a favourite at `price`: win (100 - price) if YES, lose `price`
        # if NO. edge = q*(100-price) - (1-q)*price = 100q - price, so:
        q = (edge_c + price_c) / 100.0
        q = min(max(q, 0.0), 1.0)
        collateral = price_c
        outcomes = ((q, 100.0 - price_c), (1 - q, -price_c))

    mean = sum(p * v for p, v in outcomes)
    var = sum(p * v * v for p, v in outcomes) - mean * mean
    return mean, math.sqrt(max(var, 0.0)), collateral, q


def main() -> None:
    print((__doc__ or "").split("\n\n")[0])

    print("\n=== 1. Return on capital per settlement (if the edge is real) ===\n")
    print(f"{'trade':<22}{'edge':>7}{'sd':>8}{'collateral':>12}"
          f"{'ROC/settle':>12}{'sd/edge':>9}")

    for label, price, edge, selling in (
        ("tail SELL @3c", TAIL_PRICE, TAIL_EDGE_C, True),
        ("fave BUY @97c", FAVE_PRICE, FAVE_EDGE_C, False),
    ):
        mean, sd, collateral, q = payoff_stats(price, edge, selling)
        print(f"{label:<22}{mean:>+6.2f}c{sd:>7.1f}c{collateral:>11.0f}c"
              f"{mean / collateral:>11.2%}{sd / abs(mean):>9.1f}")

    print("""
The ROC looks extraordinary because the holding period is minutes, but note
the sd/edge column: one trade is ~8x more noise than signal. This shape only
pays over MANY independent settlements, and 'independent' is doing the work.""")

    print("\n=== 2. Surviving the correlated bad day ===\n")
    tail_mean, tail_sd, tail_collat, tail_q = payoff_stats(
        TAIL_PRICE, TAIL_EDGE_C, True)
    print(f"Implied true YES rate on a 3c tail: {tail_q:.2%} "
          f"(priced {TAIL_PRICE:.0%})")
    print("""
Fifty strikes on one BTC ladder are ONE bet. If the underlying gaps through
the tail strikes, every tail-sell on that ladder loses together. That is the
ruin scenario, and it is why capital matters more than the average suggests.
""")
    print("""A position limit follows directly: cap exposure to any ONE
underlying so that a total gap through its tails costs at most a set fraction
of the account. Below, that fraction is 25%.
""")
    print(f"{'balance':>10}{'cap per ladder':>16}{'gap costs':>12}"
          f"{'ladders for 100 pos':>21}{'expected/settle':>17}")

    for balance in BALANCES:
        per_ladder = int(balance * 100 * 0.25 / tail_collat)
        gap_cost = per_ladder * tail_collat / 100.0
        ladders_needed = math.ceil(100 / per_ladder) if per_ladder else 0
        expected = 100 * tail_mean / 100.0 if per_ladder else 0.0
        print(f"${balance:>9,}{per_ladder:>16,}{gap_cost:>11.2f}$"
              f"{ladders_needed:>21,}{expected:>16.2f}$")

    print("""
'ladders for 100 pos' is the binding number and it is a COUNT OF UNCORRELATED
EVENTS, not dollars. At $35 the limit is 9 contracts per underlying, so
reaching 100 concurrent positions needs 12 genuinely independent events
running at once. Capital raises the per-ladder cap; it does not conjure
independent events. That is why the exchange's breadth, not the balance, is
the first constraint - and why the 'other' family sample matters more than
another zero on the account.""")

    print("\n=== 3. Diversification: what independence is worth ===\n")
    print(f"{'independent events':>20}{'portfolio sd':>15}{'edge':>10}"
          f"{'Sharpe/settle':>15}{'P(lose money)':>15}")

    for n in (1, 5, 25, 100, 400):
        edge = n * tail_mean
        sd = math.sqrt(n) * tail_sd
        sharpe = edge / sd
        # Normal approximation, adequate at n>=25, indicative below.
        p_loss = 0.5 * math.erfc(sharpe / math.sqrt(2))
        print(f"{n:>20,}{sd:>14.1f}c{edge:>+9.1f}c{sharpe:>15.2f}"
              f"{p_loss:>15.1%}")

    print("""
This is the whole argument for the 'other' family - politics, entertainment,
sports settle on genuinely different things, so they diversify. Crypto ladders
do not diversify against each other at all.""")

    print("\n=== 4. What unlocks at what balance ===\n")
    rows = (
        ("$35 (now)", "calibration at 1 contract, single positions",
         "proves sign only; one bad ladder is the account"),
        ("~$500", "20-30 concurrent tail positions across events",
         "first size at which diversification is real"),
        ("~$2,500", "deep-tail delta hedging becomes possible ($25-44/contract)",
         "only cell where hedge cost < edge: tails, 15m out"),
        ("~$10,000", "hedged book on deep tails; meaningful event breadth",
         "still cannot hedge ATM near expiry ($259/contract)"),
        ("~$100,000+", "the professional structure: full ladder, live delta hedge",
         "needs infra + crypto venue connectivity, not just cash"),
    )
    print(f"{'balance':<14}{'unlocks':<50}{'caveat'}")

    for balance, unlocks, caveat in rows:
        print(f"{balance:<14}{unlocks:<50}{caveat}")

    print("""
=== 5. What NO balance fixes ===

  maker market making   ZERO realised P&L over ~68 live cycles in the best
                        configuration. Scaling zero is zero, and fills get
                        worse with size, not better.
  taker sniping         killed by fee arithmetic, which is proportional to
                        size. 0 of 141 slices cleared costs.
  weather forecasting   the market's Brier beat NBM's (0.098 vs 0.131). The
                        public forecast is already in the price.
  the OBI gate          null offline and null live (t=0.33).

Those four are edge-dead, not capital-starved. Money changes nothing about
them.

=== Worth verifying before sizing anything ===

This assumes NO collateral netting across strikes within an event. If Kalshi
does net offsetting positions, vertical spreads (sell the 3c tail, buy the 2c
tail above it) would cap the ruin scenario AND cut collateral per position,
which changes section 2 materially. One spread and a balance check settles it.""")


if __name__ == "__main__":
    main()
