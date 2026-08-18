"""Turn the live session log into a small coffee scoreboard, uploaded to GCS.

    python scripts/scoreboard.py --log /var/log/kalshi-session.log --bucket steps-kalshi-book

The session runner appends one summary line per cycle:

    [14:11:19]   fills 141  markout +0.371c  P&L +1.32  balance $41.13

and one line per systemd run:

    [13:58:25] starting balance $41.18; floor $35.00

This reads those back into a compact JSON the overview site renders as a
scoreboard - balance now, coffee earned since the corrected strategy went live,
and every cycle's P&L. It is deliberately read-only: it never touches the
account, only the log the account already wrote.

The "corrected era" is anchored on a timestamp constant, not on cycle numbering,
because the cycle counter resets to 1 on every 12h restart and summing across
restarts by cycle number would double-count. Everything at or after that instant
is the passive-exit strategy; anything before it (there is none in the current
log) would be the pre-fix runs and is excluded.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

# The stake the account opened with, and the summer-coffee goal on top of it.
ORIGINAL_STAKE = 50.0
COFFEE_GOAL = 35.0  # this summer's coffees; the target balance is stake + goal

# A cycle line: [HH:MM:SS]   fills N  markout +x.xxxc  P&L +x.xx  balance $x.xx
CYCLE = re.compile(
    r"\[(\d{2}:\d{2}:\d{2})\]\s+fills\s+(\d+)\s+markout\s+"
    r"([+-][\d.]+)c\s+P&L\s+([+-][\d.]+)\s+balance\s+\$([\d,]+\.\d{2})"
)
START = re.compile(r"\[(\d{2}:\d{2}:\d{2})\] starting balance \$([\d,]+\.\d{2})")


def parse(log_text: str) -> dict:
    cycles: list[dict] = []
    starts: list[float] = []
    # The corrected (passive-exit) runner is the only one that prints "passively
    # (free)". Everything before the first such line is the pre-fix experiment
    # that crossed the spread to flatten and lost - already documented on the
    # findings page, and not part of the coffee fund. Only count from that
    # anchor, so a log spanning both eras reports the corrected strategy alone.
    corrected = False

    for line in log_text.splitlines():
        if "passively (free)" in line:
            corrected = True

        if not corrected:
            continue

        start = START.search(line)

        if start:
            starts.append(float(start.group(2).replace(",", "")))
            continue

        cycle = CYCLE.search(line)

        if cycle:
            cycles.append(
                {
                    "time": cycle.group(1),
                    "fills": int(cycle.group(2)),
                    "markout_cents": float(cycle.group(3)),
                    "pnl": float(cycle.group(4)),
                    "balance": float(cycle.group(5).replace(",", "")),
                }
            )

    balance = cycles[-1]["balance"] if cycles else (starts[-1] if starts else None)
    # Coffee earned is the sum of realised per-cycle P&L. Summing P&L rather than
    # differencing balances keeps it honest across restarts: a restart re-reads
    # the balance but the sum of what each cycle actually made is continuous.
    earned = round(sum(c["pnl"] for c in cycles), 2)
    won = sum(1 for c in cycles if c["pnl"] > 0)

    return {
        "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "balance": balance,
        "original_stake": ORIGINAL_STAKE,
        "coffee_goal": COFFEE_GOAL,
        "target_balance": ORIGINAL_STAKE + COFFEE_GOAL,
        "earned_since_fix": earned,
        "cycles_total": len(cycles),
        "cycles_won": won,
        "fills_total": sum(c["fills"] for c in cycles),
        "cycles": cycles[-60:],  # last 60 cycles for the chart
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="/var/log/kalshi-session.log")
    parser.add_argument("--bucket", default="steps-kalshi-book")
    parser.add_argument("--out", default="/var/tmp/scoreboard.json")
    args = parser.parse_args()

    log_path = Path(args.log)

    if not log_path.exists():
        print(f"no log at {log_path}")
        return

    board = parse(log_path.read_text())
    out = Path(args.out)
    out.write_text(json.dumps(board, indent=2))

    subprocess.run(
        ["gcloud", "storage", "cp", str(out), f"gs://{args.bucket}/session/scoreboard.json"],
        check=True,
        capture_output=True,
    )
    print(
        f"balance ${board['balance']}, earned ${board['earned_since_fix']} "
        f"over {board['cycles_total']} cycles -> uploaded"
    )


if __name__ == "__main__":
    main()
