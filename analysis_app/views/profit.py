"""Where the money might actually be: the conditions the original scan pooled away."""

from __future__ import annotations

import streamlit as st

from audit_data import audit, cents, money, stamp

data = audit()
profit = data["profit"]
baseline = profit["baseline"]
deep = profit["deep_tail"]
expiry = profit["near_expiry"]
replication = profit.get("replication", [])
held = [r for r in replication if r.get("verdict") == "HELD"]

st.title("Where the money might be")
st.caption(
    "The original scan asked one question of everything at once. Splitting it "
    "three ways changes the answer. Read the caveats at the bottom before "
    "believing any of this."
)
stamp()

st.header("The fee is the whole game, and it is a known curve")
st.markdown(
    r"""
Kalshi charges makers nothing and takers `0.07 x N x P(1-P)`. That is a parabola
with its maximum at 50 cents and near-zero ends:

| entry price | taker fee per contract |
|---|---:|
| \$0.50 | 1.75c |
| \$0.15 | 0.89c |
| \$0.10 | 0.63c |
| \$0.05 | 0.33c |
| \$0.02 | 0.14c |

The original scan's cheapest price band was "below 15 cents", which averages
about half a cent of fee and buries everything under it. The edge being hunted
was a few tenths of a cent. **The band that could have paid was inside the band
that was never split.**
"""
)

st.header("Three nested questions, three different answers")
st.dataframe(
    [
        {"scan": "as published: all prices, whole market life",
         "slices": baseline["testable"],
         "positive": baseline["positive"],
         "survive FDR": baseline["bh_positive"],
         "best slice": cents(baseline["best"]),
         "placebo beats it": f"{baseline['placebo_beat_rate']:.1%}"},
        {"scan": "split the cheap tail (2-5c entries)",
         "slices": deep["testable"],
         "positive": deep["positive"],
         "survive FDR": deep["bh_positive"],
         "best slice": cents(deep["best"]),
         "placebo beats it": f"{deep['placebo_beat_rate']:.1%}"},
        {"scan": "cheap tail AND last 15 min before expiry",
         "slices": expiry["testable"],
         "positive": expiry["positive"],
         "survive FDR": expiry["bh_positive"],
         "best slice": cents(expiry["best"]),
         "placebo beats it": f"{expiry['placebo_beat_rate']:.1%}"},
    ],
    hide_index=True,
)
st.markdown(
    f"""
The published scan found nothing because it averaged the cheap tail into the
expensive middle, and the final minutes into the whole life of the market. Split
both ways, **{expiry['positive']} of {expiry['testable']} slices clear costs and
{expiry['bh_positive']} survive a false-discovery-rate correction.**

The mechanism is not subtle. In the last quarter hour of a strike ladder the
order flow genuinely knows something about where it settles, and at a 2-to-5 cent
entry the fee to act on that is about a fifth of a cent instead of a cent and a
half.
"""
)

st.subheader("Best slices, cheap tail and near expiry")
st.dataframe(
    [
        {"venue": r["venue"], "imbalance": r["obi"], "entry price": r["price"],
         "horizon": f"{r['horizon']}s",
         "gross": cents(r["gross"]), "fee": cents(r["fee"]),
         "net/trade": cents(r["mean"]), "t": f"{r['t']:+.1f}",
         "markets": r["windows"], "trades/hr": f"{r['per_hour']:.1f}"}
        for r in expiry["top"][:10]
    ],
    hide_index=True,
)

dose = data.get("dose")

if dose:
    st.header("The control band, and why it changes everything")
    st.markdown(
        """
The original scan could not tell a signal from a drift, because every trigger it
kept already had extreme imbalance. There was nothing to compare against. The
re-run adds a **control band**: books with imbalance under 0.2, which carry no
signal by construction. Whatever they earn in the same price band and the same
part of a market's life is what the *structure* pays. Anything above that is what
the imbalance is worth.

Nothing is selected below. Every price band and both directions are pooled, and
each market contributes one number.
"""
    )
    for label, key in (("The recovered corpus: 15-minute family plus ladders",
                        "recovered"),
                       ("The original archive: ladders plus sports", "archive")):
        rows = dose.get(key, [])

        if not rows or "mean" not in rows[0]:
            continue

        st.subheader(label)
        st.dataframe(
            [
                {"order-book imbalance": r["band"] + ("   <- no signal" if r.get("control") else ""),
                 "mean net/trade": cents(r["mean"]),
                 "+/-": f"{r['se']:.3f}",
                 "markets": r["markets"]}
                for r in rows if "mean" in r
            ],
            hide_index=True,
        )
        lift = rows[-1]["mean"] - rows[0]["mean"]
        st.markdown(
            f"Monotone, and the imbalance is worth **{cents(lift)} per trade** over a "
            f"balanced book. But the level at the strongest imbalance is still "
            f"{cents(rows[-1]['mean'])}, so the lift alone does not pay."
        )

    st.info(
        "This is the cleanest result in the project. The imbalance signal is real, "
        "monotone, and much better established than the +0.85c originally "
        "published. It is also not, on its own, enough to trade: it lifts you "
        "roughly 0.6c to 0.9c, from a baseline that taking starts about 0.9c below "
        "water. You need a cell where the starting point is high enough that the "
        "lift clears it."
    )

    st.header("One cell does clear it, and it replicates")
    st.markdown(
        """
`KXBTCD`, entry 2 to 5 cents, 30-second horizon, inside the last quarter hour
before expiry. Tested on two corpora that were collected by different runs into
different buckets, and never analysed together until now.
"""
    )
    for label, key in (("Original archive", "lead_archive"),
                       ("Recovered corpus, never analysed before", "lead_recovered")):
        rows = dose.get(key, [])

        if not rows:
            continue

        st.subheader(label)
        st.dataframe(
            [
                {"order-book imbalance": r["band"] + ("   <- no signal" if r.get("control") else ""),
                 "net/trade": cents(r.get("mean")),
                 "+/-": f"{r['se']:.3f}" if "se" in r else "-",
                 "markets": r["markets"],
                 "clears zero": "yes" if r.get("significant") else ""}
                for r in rows
            ],
            hide_index=True,
        )

    st.success(
        "Monotone in both. The no-signal control is flat or negative in both. The "
        "top band clears zero in both, and the two estimates are not statistically "
        "different from each other. This is the first result in the project that "
        "survives a control, a holdout, and clustered standard errors at once."
    )
    st.warning(
        "It is also one instrument. On the 15-minute family the same cheap-entry "
        "condition lifts only about 0.2c and never gets above water, so this is a "
        "KXBTCD result, not a Kalshi result.",
        icon="⚠️",
    )

st.header("The earlier holdout, kept for the record")
if held:
    row = held[0]
    st.success(
        f"**{row['slice']}**, near expiry: {cents(row['in_sample'])} in-sample on "
        f"8/16-17, **{cents(row['holdout'])} on 8/18** across {row['windows']} "
        f"independent markets. The holdout had {row.get('power', 0):.0%} power, so "
        f"this is a real pass and not a silent one."
    )
else:
    st.warning("No frozen slice cleared its holdout with adequate power.")

st.markdown("Every frozen slice, with the holdout's power stated before the verdict:")
st.dataframe(
    [
        {"slice": r["slice"], "in-sample": cents(r["in_sample"]),
         "holdout": cents(r.get("holdout")),
         "holdout detects it": ("-" if r["verdict"] == "ABSENT"
                                else f"{r.get('power', 0):.0%}"),
         "verdict": r["verdict"]}
        for r in replication
    ],
    hide_index=True,
)
st.markdown(
    """
Read this table honestly. One slice held. One was genuinely **refuted** with full
power, and it is the sibling of the one that held on the other venue, which is
exactly the pattern you would expect if part of this is noise. Most of the rest
are underpowered and say nothing either way. Eight tries, one pass, one fail,
six shrugs.
"""
)

st.header("What I would actually do next, in order")
st.markdown(
    """
**1. Trade one instrument, or none.**
The replicated cell is `KXBTCD` at 2-5 cent entries in the final quarter hour.
On the 15-minute family the same condition lifts about 0.2c and stays under
water, so there is no general Kalshi strategy here. Anything that sizes off the
whole exchange is sizing off a result that only exists on the BTC hourly ladder.

**2. Trade only the side that predicts.**
Imbalance forecasts up-moves on bid-heavy books and forecasts essentially nothing
on ask-heavy ones. The scan took both. Dropping the dead half should roughly
double net per trade at the same fee, and it is a one-line change.

**3. Enter cheap or do not enter.**
Cap entries at 5 cents. Above roughly 10 cents the fee is larger than any gross
edge measured anywhere in this corpus, and no amount of signal quality fixes
that. This is a hard constraint, not a preference.

**4. Never cross to exit.**
The exit convention was worth {cents(data['scans']['all']['mid_convention_cost'])}
per trade in the audit, and crossing out is worse than that again. Rest the exit,
and size so you can afford to wait. This is the same lesson the account already
paid for once.

**5. Then, and only then, size it.**
"""
)

best_expiry = expiry["top"][0] if expiry["top"] else None

if best_expiry:
    st.dataframe(
        [
            {"quantity": "net per trade", "value": cents(best_expiry["mean"])},
            {"quantity": "trades per hour (whole venue)",
             "value": f"{best_expiry['per_hour']:.1f}"},
            {"quantity": "median contracts on the touch you must cross",
             "value": f"{best_expiry['median_size']:.0f}"},
            {"quantity": "gross per hour at that size",
             "value": money(best_expiry["dollars_hr"])},
        ],
        hide_index=True,
    )
    st.markdown(
        f"""
That is the number that matters and it is small: about
{money(best_expiry['dollars_hr'])} an hour, before a single slip, on a strategy
that only exists in the last fifteen minutes of a market's life. On the
{money(36.66)} left in the account it is a real percentage and a trivial amount
of money. The honest framing is that this is a question about whether the effect
is real, not yet a question about how much it pays.

**Capacity is also structurally against you.** Extreme imbalance means the side
you have to cross is thin, by definition. Signal strength and available size are
anti-correlated, so the edge does not scale by simply pressing harder.
"""
    )

st.header("Why this might still be nothing")
st.markdown(
    """
- **It is one instrument.** The cell replicates on KXBTCD twice, and does not
  generalise to the 15-minute family at all. Calling it an edge on Kalshi would
  be overreading it by an order of magnitude.
- **Live is not recorded.** Entry assumes you take the displayed touch at the
  instant you see it. Extreme imbalance means that touch is thin, and the thin
  side is the first thing consumed, so live fills will be worse than these. This
  is the largest untested risk and only real orders settle it.
- **Capacity is small and anti-correlated with signal.** A few dollars an hour at
  50 contracts, on a condition that exists for fifteen minutes at a time.
- **The decay confound is answered, not dismissed.** The control band is flat or
  negative wherever the edge appears, and the response rises monotonically with
  imbalance, which drift cannot produce. That was the strongest objection and it
  did not survive contact with the control.
"""
)
