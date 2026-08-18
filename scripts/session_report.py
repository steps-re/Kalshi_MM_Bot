"""Per-venue live results from a running session's journals. Read-only.

    python scripts/session_report.py /var/tmp/session

The session runner reports per-CYCLE P&L, but the open question is per-VENUE:
does WTI actually out-earn ETH in live fills, as the simulated markout ranking
predicted? That needs the journals broken out by series, which is what this
does - markout, fill count, and favourable-rate per series, pooled across every
cycle recorded so far.

Fees come from the account ledger separately; this reports markout, which is
the leading indicator, and says plainly that it is not P&L. A venue that marks
well and still loses money is losing it to inventory and exit, which the session
runner's account-truth line already captures at the aggregate level.
"""

from __future__ import annotations

import collections
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.live.journal import read_journal  # noqa: E402
from kalshi_mm_bot.market.price import ONE_DOLLAR  # noqa: E402

TICKS_PER_CENT = ONE_DOLLAR // 100


def main() -> None:
    journal_dir = Path(sys.argv[1])
    per = collections.defaultdict(lambda: {"mk": [], "placed": 0, "filled": 0})

    for path in sorted(journal_dir.glob("*.jsonl")):
        for event in read_journal(path):
            series = str(event.get("market_ticker", "")).split("-")[0]

            if event.get("event") == "placed":
                per[series]["placed"] += 1
            elif event.get("event") == "filled":
                per[series]["filled"] += 1
                mid = event.get("mid_at_fill")

                if mid is not None:
                    drift = (mid - event["yes_price"]) / TICKS_PER_CENT
                    per[series]["mk"].append(
                        drift if event.get("action") == "buy" else -drift
                    )

    if not per:
        print("no journals yet")
        return

    print(f"{'series':<14}{'placed':>8}{'filled':>8}{'fill%':>7}{'markout':>10}{'in fav':>8}")

    for series, g in sorted(per.items(), key=lambda kv: -len(kv[1]["mk"])):
        mk = g["mk"]

        if not mk:
            continue

        fill_rate = g["filled"] / g["placed"] if g["placed"] else 0
        print(
            f"{series:<14}{g['placed']:>8}{g['filled']:>8}{fill_rate:>6.1%}"
            f"{st.mean(mk):>+9.3f}c{sum(1 for x in mk if x > 0) / len(mk):>8.0%}"
        )

    print()
    print("Live markout is the leading indicator, NOT P&L - a venue can mark well")
    print("and lose money to inventory and exit. Ranks venues; account is truth.")


if __name__ == "__main__":
    main()
