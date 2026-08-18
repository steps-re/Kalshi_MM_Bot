"""Markout of our REAL fills at real horizons - where does the money actually go?

    gcloud storage rsync gs://steps-kalshi-book/journals /tmp/pj
    gcloud storage rsync -r gs://steps-kalshi-book/recordings /tmp/recs   # or use local
    python scripts/markout_horizon.py /tmp/pj /var/tmp/kalshi-recordings

The per-fill journal records the mid at the instant we learned of the fill - a
sub-second horizon. Every slice of that markout is positive while the account
bleeds, which means the loss is PAST that horizon: the fill looks good, then the
price finishes moving against the position over the next seconds. This joins each
real fill to the recorded book of the same market and measures the signed mid move
at 1/5/10/30/60 seconds after the fill - the horizons where the imbalance signal
(0.85c over ~5s) and the inventory/pre-settlement drift actually play out.

If markout is positive at 1s and turns negative by 30-60s, that curve IS the leak,
and it tells us what to fix: adverse selection (bites by ~5s), inventory drift
(tens of seconds), or settlement (the final minute). Broken out by A/B arm, it
also shows whether the microprice center changes the curve at the horizon that
matters - the fill-instant markout says it doesn't.

Uses our real fills (which carry adverse selection) against the recorded books, so
unlike a simulator it can see the move the fills provoke.
"""

from __future__ import annotations

import asyncio
import json
import statistics as st
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.feed_controller import (  # noqa: E402
    FeedController,
    ORDERBOOK_CHANNEL,
)
from kalshi_mm_bot.market.price import ONE_DOLLAR  # noqa: E402
from kalshi_mm_bot.recording import (  # noqa: E402
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)

TICKS_PER_CENT = ONE_DOLLAR // 100
# Horizon 0 = the recorded mid at (or just after) the fill instant. It exists to
# RECONCILE against the journal's own mid_at_fill: if the recorded t=0 markout
# matches the journal markout, the join's convention and alignment are correct and
# a negative curve at longer horizons is real adverse selection; if t=0 is already
# the sign-flip of the journal's, the join is measuring the wrong book side and the
# whole curve is an artifact, not a finding.
HORIZONS = (0.0, 1.0, 5.0, 10.0, 30.0, 60.0)


def parse_iso(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def arm_of(path: Path) -> str:
    parts = path.stem.split("_")
    return parts[2] if len(parts) >= 3 and parts[0] == "jrnl" else "?"


async def mid_timeline(recording: Path) -> dict[str, list[tuple[float, float]]]:
    """Per-ticker [(abs_utc, mid_ticks)] from one recording, absolute-time keyed."""

    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        return {}

    start = parse_iso(reader.manifest.started_at_utc)

    if start is None:
        return {}

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=0.0)
    controller = FeedController(rest=RecordedRestClient(reader.manifest), ws=ws)
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)

    await controller.connect()
    await controller.subscribe(reader.manifest.tickers, channels=(ORDERBOOK_CHANNEL,))

    while True:
        try:
            ticker = await controller.recv()
        except EOFError:
            break

        event = ws.last_event

        if event is None or ticker is None:
            continue

        book = controller.orderbooks.get(ticker)

        if book is None or book.best_bid is None or book.best_ask is None:
            continue

        out[ticker].append(
            (start + event.offset_seconds, (book.best_bid + book.best_ask) / 2)
        )

    return out


def mid_at(series: list[tuple[float, float]], when: float) -> float | None:
    """Mid at the first sample at or after `when`; None past the record's end."""

    i = bisect_left(series, (when, -1.0))
    return series[i][1] if i < len(series) else None


def load_fills(journal_dir: Path) -> list[dict]:
    fills = []

    for path in sorted(journal_dir.glob("*.jsonl")):
        arm = arm_of(path)

        for line in path.read_text().splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                event = json.loads(line)
            except ValueError:
                continue

            if event.get("event") != "filled":
                continue

            at = parse_iso(event.get("at"))

            if at is None or event.get("yes_price") is None:
                continue

            fills.append(
                {
                    "arm": arm,
                    "ticker": event["market_ticker"],
                    "at": at,
                    "yes_price": event["yes_price"],
                    "action": event.get("action"),
                    "mid_at_fill": event.get("mid_at_fill"),
                }
            )

    return fills


async def main() -> None:
    journal_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pj")
    rec_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "/var/tmp/kalshi-recordings")

    recordings = sorted(p for p in rec_dir.iterdir() if (p / "manifest.json").exists())

    fills = load_fills(journal_dir)
    fills_by_ticker: dict[str, list[dict]] = defaultdict(list)

    for fill in fills:
        fills_by_ticker[fill["ticker"]].append(fill)

    # markouts[horizon] and by_arm[arm][horizon] = list of signed cents.
    # Streamed one recording at a time: a fill's 15M ticker lives in exactly one
    # recording, so matching per-recording and discarding its timeline keeps
    # memory flat across the whole corpus (122 recordings would not fit at once).
    markouts: dict[float, list[float]] = defaultdict(list)
    by_arm: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    journal_markouts: list[float] = []  # the fill's own mid_at_fill, matched fills only
    matched: set[int] = set()

    for rec in recordings:
        try:
            timeline = await mid_timeline(rec)
        except Exception as error:  # noqa: BLE001
            print(f"  skipped {rec.name}: {type(error).__name__} {error}")
            continue

        for ticker, series in timeline.items():
            series.sort()

            for fill in fills_by_ticker.get(ticker, ()):
                sign = 1.0 if fill["action"] == "buy" else -1.0
                hit = False

                for horizon in HORIZONS:
                    future = mid_at(series, fill["at"] + horizon)

                    if future is None:
                        continue

                    hit = True
                    move = sign * (future - fill["yes_price"]) / TICKS_PER_CENT
                    markouts[horizon].append(move)
                    by_arm[fill["arm"]][horizon].append(move)

                if hit:
                    matched.add(id(fill))

                    if fill["mid_at_fill"] is not None:
                        journal_markouts.append(
                            sign * (fill["mid_at_fill"] - fill["yes_price"]) / TICKS_PER_CENT
                        )

    print(f"{len(fills)} fills, {len(matched)} matched to a recorded book; "
          f"{len(recordings)} recordings scanned\n")

    if journal_markouts:
        jm = st.mean(journal_markouts)
        rec0 = st.mean(markouts[0.0]) if markouts.get(0.0) else None
        print("RECONCILIATION (matched fills):")
        print(f"  journal mid_at_fill markout : {jm:+.3f}c")
        print(f"  recorded book t=0 markout   : "
              f"{rec0:+.3f}c" if rec0 is not None else "  recorded t=0: none")

        if rec0 is not None:
            if abs(rec0 - jm) < 0.15:
                print("  -> agree: join convention/alignment OK; longer-horizon curve is real.")
            elif abs(rec0 + jm) < 0.15:
                print("  -> SIGN-FLIPPED vs journal: the join reads the wrong book side. "
                      "The curve is an ARTIFACT, not a finding. Fix before trusting it.")
            else:
                print("  -> disagree (not a clean flip): alignment/state mismatch. "
                      "Treat the curve as unverified.")
        print()

    print("markout vs horizon (all fills):")
    print(f"  {'horizon':>8}{'n':>8}{'mean':>10}")

    for horizon in HORIZONS:
        vals = markouts[horizon]

        if vals:
            print(f"  {horizon:>6.0f}s{len(vals):>8}{st.mean(vals):>+9.3f}c")

    print("\nmarkout vs horizon by A/B arm (does the microprice center bend the curve?):")

    for arm in sorted(by_arm):
        cells = []

        for horizon in HORIZONS:
            vals = by_arm[arm][horizon]
            cells.append(f"{horizon:.0f}s {st.mean(vals):+.3f}c" if vals else f"{horizon:.0f}s -")
        print(f"  {arm:<12} " + "  ".join(cells))

    print("\nA curve that starts positive and turns negative IS the leak; the horizon "
          "where it crosses says what to fix (adverse selection ~5s, inventory ~30s, "
          "settlement ~60s). Equal curves across arms => the microprice center does "
          "not change where we bleed.")


if __name__ == "__main__":
    asyncio.run(main())
