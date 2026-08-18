"""Compare the A/B arms the session runner recorded, honestly.

    python scripts/ab_report.py /var/tmp/session/ab_ledger.jsonl

The runner appends one row per cycle tagged with its arm (a min_profit_edge
value). This groups by arm and reports, per arm, the account P&L per cycle with a
standard error and a t-stat against zero, plus mean per-fill markout. It then
does pairwise Welch t-tests between arms.

The whole point is to resist the trap that sank the first read of this project:
a single lucky cycle. Account P&L per cycle has a large swing (~$0.8), so an arm
looking good over a handful of cycles means nothing. The report prints n and the
standard error next to every mean and says plainly when a difference is inside
the noise. Markout is a lower-variance leading signal but it is NOT P&L - an arm
can mark better and still lose, which is the adverse-selection gap we are hunting,
so both are shown and neither is taken as the verdict alone.
"""

from __future__ import annotations

import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []

    for line in path.read_text().splitlines():
        line = line.strip()

        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

    return rows


def welch_t(a: list[float], b: list[float]) -> tuple[float, float]:
    """Welch t and its degrees of freedom; (0, 0) if either arm is too thin."""

    if len(a) < 2 or len(b) < 2:
        return 0.0, 0.0

    va, vb = st.variance(a), st.variance(b)
    na, nb = len(a), len(b)
    se = math.sqrt(va / na + vb / nb)

    if se == 0:
        return 0.0, 0.0

    t = (st.mean(a) - st.mean(b)) / se
    df_num = (va / na + vb / nb) ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    return t, (df_num / df_den if df_den else 0.0)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/var/tmp/session/ab_ledger.jsonl")

    if not path.exists():
        print(f"no A/B ledger at {path}")
        return

    rows = load(path)

    if not rows:
        print("A/B ledger is empty")
        return

    by_arm: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        by_arm[row.get("arm", "?")].append(row)

    print(f"{len(rows)} cycles across {len(by_arm)} arm(s) from {path}\n")
    print(f"{'arm':<10}{'cycles':>7}{'$/cycle':>10}{'SE':>8}{'t vs 0':>8}"
          f"{'sum $':>9}{'markout':>10}{'fills':>8}")

    arm_pnls: dict[str, list[float]] = {}

    for arm in sorted(by_arm):
        cycles = by_arm[arm]
        pnls = [c["pnl"] for c in cycles if c.get("pnl") is not None]
        marks = [c["markout_cents"] for c in cycles if c.get("markout_cents") is not None]
        fills = sum(c.get("fills", 0) for c in cycles)
        arm_pnls[arm] = pnls
        mean = st.mean(pnls) if pnls else 0.0
        se = (st.stdev(pnls) / math.sqrt(len(pnls))) if len(pnls) > 1 else 0.0
        t = mean / se if se else 0.0
        mk = st.mean(marks) if marks else 0.0
        print(f"{arm:<10}{len(cycles):>7}{mean:>+10.3f}{se:>8.3f}{t:>+8.2f}"
              f"{sum(pnls):>+9.2f}{mk:>+9.3f}c{fills:>8}")

    print()
    arms = sorted(arm_pnls)

    if len(arms) >= 2:
        print("pairwise account-P&L comparison (Welch t; |t|<2 = inside the noise):")

        for i in range(len(arms)):
            for j in range(i + 1, len(arms)):
                a, b = arms[i], arms[j]
                t, df = welch_t(arm_pnls[a], arm_pnls[b])
                verdict = "DIFFERENT" if abs(t) >= 2 else "inside the noise"
                print(f"  {a} vs {b}: t={t:+.2f} (df~{df:.0f})  {verdict}")

    # How many cycles per arm would it take to resolve the current spread?
    pooled = [p for ps in arm_pnls.values() for p in ps]

    if len(pooled) > 1:
        sd = st.stdev(pooled)
        print(f"\nPooled per-cycle sd ${sd:.2f}. To detect a $0.15/cycle arm "
              f"difference at 80% power needs ~{7.84 * sd * sd / 0.15**2:.0f} cycles "
              f"per arm; $0.25 needs ~{7.84 * sd * sd / 0.25**2:.0f}.")


if __name__ == "__main__":
    main()
