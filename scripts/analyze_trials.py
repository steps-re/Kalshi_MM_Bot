"""Pool queue-experiment trials and report what they actually say.

    python scripts/analyze_trials.py data/markout/*.json
    python scripts/analyze_trials.py data/markout --by close

Each `queue_experiments.py` run writes its own file, so the interesting sample
is spread across a directory by the time there is enough of it to mean anything.
This pools them and answers the three questions the trials were bought to
answer: do we fill, what does it cost, and are we being picked off.

The markout convention: every trial buys, so a fill is good when the mid rises
afterwards. `markout = mid_at_fill - our_price`, positive is in our favour. It
is reported in cents per contract so it can be compared directly against the
fee and the spread, which are quoted the same way.

Three habits this enforces, each one bought with a bug:

* **Unreadable is not zero.** A fee we could not parse is excluded and counted
  separately, never summed as free. A reader that silently returned 0 once made
  48 taker fills that really cost $0.5879 look costless.
* **A zero needs a control.** A maker total of $0.00 means nothing unless taker
  fills in the same sample were charged something. Without that, a broken
  parser and a free market are the same observation.
* **Time to close is a regime, not a covariate.** A 15-minute window at twelve
  minutes out and at thirty seconds out differ in spread, depth and price by an
  order of magnitude. Pooling across that reports the average of two different
  markets, so `--by close` splits it.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.market.price import MONEY_SCALE, ONE_DOLLAR  # noqa: E402

TICKS_PER_CENT = ONE_DOLLAR // 100

CLOSE_BUCKETS = (
    ("12m+", 720, float("inf")),
    ("6-12m", 360, 720),
    ("3-6m", 180, 360),
    ("1-3m", 60, 180),
    ("<1m", 0, 60),
)


def load(paths: list[Path]) -> list[dict]:
    """Read every trial from files or directories, newest schema tolerated."""

    trials: list[dict] = []

    for path in paths:
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]

        for file in files:
            try:
                payload = json.loads(file.read_text())
            except (OSError, ValueError) as error:
                print(f"  skipped {file.name}: {error}", file=sys.stderr)
                continue

            if isinstance(payload, list):
                trials.extend(t for t in payload if isinstance(t, dict))

    return trials


def markout_cents(trial: dict) -> float | None:
    """Signed edge in cents, positive when the market moved our way.

    None when the book at fill time was never captured, which is true of every
    trial recorded before that field existed. Those trials are real fills and
    still say something about fill rates and fees; they simply cannot speak to
    adverse selection, and must not be counted as zero drift.
    """

    mid_at_fill = trial.get("mid_at_fill")

    if mid_at_fill is None or not trial.get("filled"):
        return None

    return (mid_at_fill - trial["our_price"]) / TICKS_PER_CENT


def fill_summary(trials: list[dict]) -> str:
    lines = [f"{'mode':<9}{'trials':>7}{'filled':>8}{'rate':>7}{'med depth':>11}{'med fill s':>12}"]

    for mode in ("touch", "inside", "cross"):
        subset = [t for t in trials if t.get("mode") == mode]

        if not subset:
            continue

        filled = [t for t in subset if t.get("filled")]
        times = [t["seconds_to_fill"] for t in filled if t.get("seconds_to_fill")]
        lines.append(
            f"{mode:<9}{len(subset):>7}{len(filled):>8}{len(filled) / len(subset):>7.0%}"
            f"{st.median([t['depth_ahead'] for t in subset]):>11,.0f}"
            f"{(st.median(times) if times else float('nan')):>12.1f}"
        )

    return "\n".join(lines)


def fee_summary(trials: list[dict]) -> str:
    filled = [t for t in trials if t.get("filled")]
    readable = [t for t in filled if t.get("fee_micros") is not None]
    unreadable = len(filled) - len(readable)

    makers = [t for t in readable if not t.get("is_taker")]
    takers = [t for t in readable if t.get("is_taker")]
    maker_total = sum(t["fee_micros"] for t in makers)
    taker_total = sum(t["fee_micros"] for t in takers)

    lines = [
        f"{'':<9}{'fills':>7}{'total fee':>12}{'mean/fill':>12}",
        f"{'maker':<9}{len(makers):>7}{maker_total / MONEY_SCALE:>12.4f}"
        f"{(maker_total / len(makers) / MONEY_SCALE if makers else 0):>12.4f}",
        f"{'taker':<9}{len(takers):>7}{taker_total / MONEY_SCALE:>12.4f}"
        f"{(taker_total / len(takers) / MONEY_SCALE if takers else 0):>12.4f}",
    ]

    if unreadable:
        lines.append(
            f"\n!! {unreadable} filled trial(s) had no readable fee. Excluded, NOT "
            "counted as free."
        )

    if makers and maker_total == 0:
        if takers and taker_total > 0:
            lines.append(
                f"\nmakers pay nothing; control holds - {len(takers)} taker fill(s) "
                f"were charged ${taker_total / MONEY_SCALE:.4f}."
            )
        else:
            lines.append(
                "\nNO CONTROL: makers read as free but no taker fill in this sample "
                "was charged either, so a broken fee reader looks identical to a "
                "free market. Cross once before believing it."
            )

    return "\n".join(lines)


def markout_summary(trials: list[dict], *, by_close: bool) -> str:
    scored = [(t, markout_cents(t)) for t in trials if t.get("filled")]
    usable = [(t, m) for t, m in scored if m is not None]
    missing = len(scored) - len(usable)

    if not usable:
        return (
            "no trial recorded the book at fill time, so adverse selection is "
            "unmeasured. Re-run to capture mid_at_fill."
        )

    def row(label: str, items: list[tuple[dict, float]]) -> str:
        values = sorted(m for _, m in items)
        wins = sum(1 for v in values if v > 0)
        return (
            f"{label:<11}{len(values):>6}{st.mean(values):>10.2f}c"
            f"{st.median(values):>10.2f}c{values[0]:>9.2f}c{values[-1]:>9.2f}c"
            f"{wins / len(values):>9.0%}"
        )

    header = (
        f"{'group':<11}{'fills':>6}{'mean':>11}{'median':>11}"
        f"{'worst':>10}{'best':>10}{'in favour':>10}"
    )
    lines = [header]

    for mode in ("touch", "cross"):
        subset = [(t, m) for t, m in usable if t.get("mode") == mode]

        if subset:
            lines.append(row(mode, subset))

    if by_close:
        lines.append("")
        lines.append("resting fills by time left in the window:")
        lines.append(header)

        for label, low, high in CLOSE_BUCKETS:
            subset = [
                (t, m)
                for t, m in usable
                if t.get("mode") == "touch"
                and t.get("seconds_to_close") is not None
                and low <= t["seconds_to_close"] < high
            ]

            if subset:
                lines.append(row(label, subset))

    resting = [m for t, m in usable if t.get("mode") == "touch"]

    if len(resting) >= 5:
        mean, median = st.mean(resting), st.median(resting)

        if (mean < 0) != (median < 0):
            # The signature of market making: win small, often; lose big,
            # rarely. Quoting the median here would describe a profitable
            # strategy that loses money, and quoting the mean would describe a
            # broken one that fills favourably most of the time. Both are true
            # and neither is the summary.
            worst = min(resting)
            lines.append(
                f"\n!! mean ({mean:+.2f}c) and median ({median:+.2f}c) disagree in "
                "sign on resting fills."
            )
            lines.append(
                f"   Most fills go our way and the tail takes it back - the worst "
                f"single fill was {worst:+.2f}c, against a median of {median:+.2f}c."
            )
            lines.append(
                "   Neither number alone describes this. What decides it is whether "
                "the tail can be cut without losing the wins, which is an inventory "
                "and risk question, not a quoting one."
            )

    if missing:
        lines.append(
            f"\n{missing} filled trial(s) predate mid_at_fill and are excluded "
            "from markout - they are not evidence of zero drift."
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--by",
        choices=("mode", "close"),
        default="close",
        help="Split markout by quoting mode only, or also by time to close.",
    )
    args = parser.parse_args()

    trials = load(args.paths)

    if not trials:
        print("no trials found")
        return

    print(f"{len(trials)} trial(s) pooled\n")
    print(fill_summary(trials))
    print()
    print(fee_summary(trials))
    print()
    print("MARKOUT (buy-side; positive means the mid moved our way after we filled)")
    print(markout_summary(trials, by_close=args.by == "close"))


if __name__ == "__main__":
    main()
