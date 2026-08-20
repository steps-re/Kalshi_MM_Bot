"""Could we run a delta-hedged book like a professional MM? Arithmetic, not opinion.

    python scripts/hedge_economics.py

A Kalshi binary is a digital option on the underlying. Its delta is

    dV/dS = phi(d2) / (S * sigma * sqrt(T))          d2 = N^-1(price)

so hedging one contract requires `dV/dS` BTC, i.e. `dV/dS * S` dollars of
notional on a crypto venue. Two things follow immediately and neither is a
matter of taste:

* delta RISES as expiry approaches (T shrinks in the denominator), so the last
  fifteen minutes - the only window this project ever traded - is the most
  expensive place on the curve to hedge;
* the hedge notional per $1-payout contract is far larger than the contract's
  own price, because a digital's leverage to the underlying near expiry is
  enormous.

This prices the CHEAPEST possible hedge: establish once, unwind once, at a
single round-trip cost in basis points, with no rebalancing at all. Real delta
hedging rebalances continuously and pays that cost many times over, so every
number here is a hard lower bound on what hedging would cost. If the lower
bound already exceeds the edge, the idea is dead without needing a gamma model.

Edges it is compared against are this project's own measured numbers, not
hopes: +0.4c per fill of maker capture (39 live cycles, ledger-exact) and
+1.0c to +1.5c per contract for the hold-to-settlement calibration trade
(12 days, day-clustered, fees included).
"""

from __future__ import annotations

import math

# BTC annualised vol. 15-minute realised vol on BTC has run 40-60% annualised;
# the conclusion below is not close enough for the choice to matter.
VOLS = (0.40, 0.50, 0.60)
SPOT = 71_700.0
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
# Round-trip cost on a crypto perp, in basis points of notional. 3bp is
# optimistic-but-real for a retail account on a good venue (maker both sides);
# 6bp is a taker round trip.
ROUND_TRIP_BPS = (3.0, 6.0)
# (label, contract price) - the two zones this project actually trades.
ZONES = (("deep tail (3c)", 0.03), ("tail edge (5c)", 0.05),
         ("mid (25c)", 0.25), ("at the money", 0.50))
HORIZONS_MIN = (15.0, 5.0, 1.0)

MAKER_CAPTURE_C = 0.4      # measured, per fill
CALIBRATION_EDGE_C = 1.5   # measured upper end, per contract


def norm_ppf(p: float) -> float:
    """Inverse standard normal (Acklam), plenty accurate for this."""

    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)

    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q
                 + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)

    q, r = p - 0.5, (p - 0.5) ** 2
    return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r
             + a[5]) * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1))


def phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def hedge_notional(price: float, minutes: float, vol: float) -> float:
    """Dollars of BTC needed to delta-hedge ONE $1-payout contract."""

    t_years = minutes / MINUTES_PER_YEAR
    d2 = norm_ppf(price)
    delta = phi(d2) / (SPOT * vol * math.sqrt(t_years))   # dV per $1 of S
    return delta * SPOT                                    # dollars of notional


def main() -> None:
    print((__doc__ or "").split("\n\n")[0])
    print(f"\nSpot ${SPOT:,.0f}. Hedge notional per ONE contract "
          f"($1 max payout):\n")
    print(f"{'zone':<18}{'T':>6}" + "".join(f"{f'vol {v:.0%}':>12}" for v in VOLS))

    for label, price in ZONES:
        for minutes in HORIZONS_MIN:
            cells = "".join(
                f"${hedge_notional(price, minutes, v):>11,.0f}" for v in VOLS)
            print(f"{label:<18}{minutes:>5.0f}m{cells}")

    print(f"""
Read the ATM row first: hedging a single $1 contract five minutes from expiry
needs roughly ${hedge_notional(0.50, 5.0, 0.50):,.0f} of BTC. The account has $35.
One contract is already out of reach by a factor of ~{hedge_notional(0.50, 5.0, 0.50) / 35:,.0f}, and
Kalshi's median depth on the crossing side is 74 contracts.

Now the cost, against edges this project has actually measured.
Establish-once/unwind-once only. No rebalancing. A hard lower bound.
""")
    print(f"{'zone':<18}{'T':>6}{'notional':>11}"
          + "".join(f"{f'cost @{b:.0f}bp':>13}" for b in ROUND_TRIP_BPS)
          + f"{'vs maker':>11}{'vs calib':>10}")

    for label, price in ZONES:
        for minutes in HORIZONS_MIN:
            notional = hedge_notional(price, minutes, 0.50)
            costs = [notional * bps / 10_000.0 * 100.0 for bps in ROUND_TRIP_BPS]
            cheapest = costs[0]
            print(f"{label:<18}{minutes:>5.0f}m{notional:>11,.0f}"
                  + "".join(f"{c:>12.2f}c" for c in costs)
                  + f"{MAKER_CAPTURE_C - cheapest:>+10.2f}c"
                  f"{CALIBRATION_EDGE_C - cheapest:>+9.2f}c")

    print(f"""
'vs maker'  = {MAKER_CAPTURE_C}c measured capture per fill, minus the cheapest hedge.
'vs calib'  = {CALIBRATION_EDGE_C}c measured calibration edge, minus the cheapest hedge.
Negative means the hedge costs more than the edge it protects, once, before
a single rebalance and before basis risk.

What the table cannot show, and what makes it worse:

1. GAMMA. A digital's delta near expiry is unstable - it collapses toward zero
   or blows toward the strike as the underlying moves. Holding a hedge static
   is not hedging; rebalancing multiplies the cost above by the number of
   rebalances, and the last minutes are when you must rebalance most.
2. BASIS. Kalshi settles on a specific index print at a specific instant. A
   perp hedge does not track that print in the final seconds, which is exactly
   when the digital's payoff is decided. The hedge is weakest precisely where
   the risk concentrates.
3. CAPITAL. Kalshi is fully collateralised - no leverage - so the Kalshi leg
   ties up its own cash while the crypto leg needs margin elsewhere.

The professional structure is real and it is not available at this size. What
IS available is in the docstring of the calibration work: diversification
across UNCORRELATED settlements, which lowers variance without paying a hedge
bill. Fifty strikes on one BTC ladder are one bet on BTC and diversify nothing
- which is why calibration_at_t.py clusters on series x day rather than
pretending each strike is a draw.""")


if __name__ == "__main__":
    main()
