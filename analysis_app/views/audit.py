"""What the re-run found, and which of the original conclusions survived it."""

from __future__ import annotations

import streamlit as st

from audit_data import audit, cents, stamp

data = audit()
corpus = data["corpus"]
scans = data["scans"]
whole = scans["all"]

st.title("The audit")
st.caption(
    "The project closed on a scan with six measurement defects. This is the same "
    "question asked again, on the full archive, with them fixed. For Nate."
)
stamp()

st.header("The short version")
st.markdown(
    f"""
**The verdict held. Almost none of the evidence originally given for it did.**

Re-run across {corpus['recordings']} recordings and
{corpus['elapsed_hours']:.1f} hours of book coverage - about six times what the
original scan used - **{whole['positive']} of {whole['testable']} testable slices
clear costs**, and a whole-window sign-flip placebo beats the best observed slice
{whole['placebo_beat_rate']:.1%} of the time. That is a stronger negative than
the one first published.

Then the pooling came off, and the picture changed. See
**Where the money might be**.
"""
)

st.header("1. The exit was priced at the wrong side of the book")
st.markdown(
    f"""
The scan computed `net = signed mid move - fee` while its own documentation said
the exit "rests as a maker (measured free)". A resting exit does not fill at the
mid. It fills at the **touch**, because someone has to cross to you. On books
filtered to 2 cents or narrower, that difference is half a spread on every trade.

Measured across the corpus, marking the exit at the mid cost
**{cents(whole['mid_convention_cost'])} per trade** against an exit rested at the
touch. The largest real effect anywhere in the baseline scan is
{cents(whole['best'])}. The assumption was worth several times the thing being
measured, in the direction that manufactures negatives.
"""
)
st.dataframe(
    [
        {"exit assumption": "rested at the touch (it fills)",
         "mean net/trade": cents(whole["exit_touch"]),
         "what it means": "best case, your passive exit always trades"},
        {"exit assumption": "marked at the mid",
         "mean net/trade": cents(whole["exit_mid"]),
         "what it means": "the original number, and not a price anyone gets"},
        {"exit assumption": "forced to cross out",
         "mean net/trade": cents(whole["exit_cross"]),
         "what it means": "worst case, you pay a second fee to leave"},
        {"exit assumption": "blended at the ledger's cross rates",
         "mean net/trade": cents(whole["exit_blended"]),
         "what it means": "the honest central estimate"},
    ],
    hide_index=True,
)

st.header("2. The t-statistics counted triggers, not price paths")
st.markdown(
    f"""
Every trigger inside one market rides the same price path. Slices here average
**{whole['triggers_per_window']:.0f} triggers per market**, so treating them as
independent draws overstates precision by roughly the square root of that.
Clustering the standard error on the market ticker moves the median SE by
**{whole['se_ratio']:.1f}x** - and it moves the venue conclusions much further,
because the old numbers combined an inflated effect with an understated error.
"""
)
st.dataframe(
    [
        {"venue": row["venue"],
         "old net": cents(row["old_net"]), "old t": f"{row['old_t']:+.1f}",
         "corrected net": cents(row["new_net"]), "corrected t": f"{row['new_t']:+.1f}",
         "markets": row["windows"]}
        for row in whole["legacy"]
    ],
    hide_index=True,
)
st.markdown(
    """
The published claim *"ETH-daily -0.79c at t = -22.6"*, cited as a decisively
replicated rejection, reproduces under the old conventions at t = -21.7, which
confirms the reimplementation is faithful. Corrected, that venue is not
distinguishable from zero. In-play sports, dismissed as "all negative", supply the
best slice in the baseline scan.
"""
)

st.header("3. Failure to replicate was read as refutation")
st.markdown(
    """
A null holdout is evidence only if the holdout could have seen the effect. Eight
slices were pre-registered from 8/18 and put to the later data, with the minimum
detectable effect computed **before** the verdict.
"""
)
for label, key in (("Forward, onto 8/19", "in_to_oos"),
                   ("Forward, onto the untouched 8/19 afternoon", "in_to_virgin"),
                   ("Backward control: the holdout's own winners, tested on 8/18",
                    "virgin_to_in")):
    rows = data["replication"].get(key, [])

    if not rows:
        continue

    st.subheader(label)
    st.dataframe(
        [
            {"slice": r["slice"],
             "in-sample": cents(r["in_sample"]),
             "holdout": cents(r.get("holdout")),
             "detects effect?": ("-" if r["verdict"] == "ABSENT"
                                 else f"{r.get('power', 0):.0%} of the time"),
             "verdict": r["verdict"]}
            for r in rows
        ],
        hide_index=True,
    )

st.markdown(
    """
Five of eight slices were **absent** from the untouched holdout, because those
markets do not trade in that part of the day. Most of the rest could not have
detected their own effect. The backward control settles it: freeze the holdout's
own winners and test them on the original period, and every one comes back
underpowered too. Neither period was ever able to judge the other, which is why
the "winner lists" came out disjoint.
"""
)

st.header("4. The premise was overstated, and it is one-sided")
obi = data.get("obi")

if obi:
    five = next((h for h in obi["horizons"] if h["horizon"] == 5.0), None)

    if five:
        st.markdown(
            f"""
"+0.85c per unit OBI at 5s, n = 1.36M updates, monotonic on every major venue"
becomes, with the same estimator clustered on the market:

- pooled slope **{cents(five['pooled_slope'])} per unit OBI**
  ({five['book_updates']:,} book updates)
- per market **{cents(five['per_market'])} +/- {five['se']:.3f}**
  (t = {five['t']:+.1f} across {five['markets']} markets)

The signal is real and survives clustering. It is a fraction of the advertised
size, and it is not present everywhere.
"""
        )
        st.dataframe(
            [
                {"venue": v["venue"],
                 "slope per unit OBI": cents(v["slope"]),
                 "+/-": f"{v['se']:.3f}" if v["se"] is not None else "-",
                 "markets": v["markets"],
                 "real?": "yes" if v["significant"] else "not distinguishable from 0"}
                for v in five["venues"] if v["markets"] >= 3
            ],
            hide_index=True,
        )
        st.subheader("And it only works in one direction")
        st.dataframe(
            [{"order-book imbalance": b["label"],
              "forward mid move": cents(b["mean"]),
              "observations": f"{b['n']:,}"}
             for b in five["buckets"]],
            hide_index=True,
        )
        balanced = next((b["mean"] for b in five["buckets"] if "balanced" in b["label"]), 0.0)
        heavy = next((b["mean"] for b in five["buckets"] if b["label"].startswith(">")), 0.0)
        light = next((b["mean"] for b in five["buckets"] if b["label"].startswith("<")), 0.0)
        st.markdown(
            f"""
These are strike ladders that mostly decay toward zero, so the shared negative
drift is expected. The asymmetry is not. Measured against the balanced bucket,
**bid-heavy books predict {cents(heavy - balanced)} and ask-heavy books predict
{cents(light - balanced)}**. The scan's rule takes the trade in whichever
direction imbalance points, so roughly half its trades were placed on the side
where the signal does not exist.
"""
        )
else:
    st.info("OBI predictivity numbers pending the next data export.")

st.header("5. The headline table rested on two markets")
st.markdown(
    f"""
The {corpus['recordings']}-recording archive contains no 15-minute-window data at
all, so for a while none of the original in-sample table could be checked. The
recordings turned out never to have been lost: the collector had been writing to
a **second bucket** that returns 403 for one of the two accounts on this project,
and that 403 was read as absence. Switching accounts recovered 159 recordings and
16.6GB.

With that data in hand the headline claim resolves, and not in its favour.
"""
)
st.dataframe(
    [
        {"venue": "KXGOLD15M", "markets behind the published +1.39c (t=2.0)": 2,
         "markets available once recovered": 53},
        {"venue": "KXSOL15M", "markets behind the published +1.39c (t=2.0)": 2,
         "markets available once recovered": 46},
        {"venue": "KXDOGE15M", "markets behind the published +1.39c (t=2.0)": 2,
         "markets available once recovered": 50},
        {"venue": "KXWTI15M", "markets behind the published +1.39c (t=2.0)": 2,
         "markets available once recovered": 51},
    ],
    hide_index=True,
)
st.markdown(
    """
`KXGOLD15M` had exactly two windows in the data the scan read: 11:30 and 16:15 on
18 August. **Thirty minutes of one market's life.** The reported t of 2.0 came
from treating 931 ticks inside those two price paths as 931 independent draws.
That is the whole explanation for the original chapter, and it is simpler than
the search-noise story it told about itself: not hundreds of slices mined for a
winner, but a standard error computed across two markets.

It also explains the replication failure directly. An estimate built on two
windows is a draw from a very wide distribution. Of course a different day
produced different winners.
"""
)

st.header("One thing the audit itself got wrong")
st.markdown(
    f"""
The scan read the book at the first update at or *after* each horizon rather than
the last one at or before it, which peeks at the very move being predicted. That
is a real defect and it was expected to matter. It is worth
**{cents(whole['lookahead'])} per trade**, including on the quietest books where
the next update landed more than ten seconds late. Fixed anyway, in four scripts,
but it explains nothing.
"""
)
