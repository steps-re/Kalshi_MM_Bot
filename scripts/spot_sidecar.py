"""Record BTC/ETH spot beside the orderbook collector, for settlement analysis.

    python scripts/spot_sidecar.py --bucket steps-kalshi-book

Two of the untested alpha candidates need the underlying's price recorded next
to the book, and neither can be evaluated retroactively because we never wrote
spot down:

* **Settlement-average lock-in.** Kalshi's crypto windows settle on the
  60-second average of an index. During the final minute the settlement value is
  progressively *determined* - each observed second locks in a share of the
  average - so its variance collapses linearly to zero while the book may keep
  pricing an open question. Testing that requires spot at 1s resolution against
  the book's last minute.
* **Window-open dislocation.** A freshly opened window has a thin book and no
  history; whether its first prices track the spot-implied fair value cannot be
  asked without the spot.

One row a second, JSONL, hourly files, uploaded to GCS beside the book
recordings. Coinbase is primary and Kraken the fallback - both BTC/USD, the
right currency for Kalshi's index (BRTI). The USDT venues are deliberately not
used here: a 9bp basis was measured between USD and USDT, big enough to flip a
strike's moneyness.

Failures never stop the loop; a gap in the file is honest, a crashed recorder
is a week of silence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def read_spot(client: httpx.Client, product: str, kraken_pair: str) -> float | None:
    try:
        r = client.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker"
        ).json()
        return (float(r["bid"]) + float(r["ask"])) / 2
    except Exception:
        pass

    try:
        r = client.get(
            "https://api.kraken.com/0/public/Ticker", params={"pair": kraken_pair}
        ).json()["result"]
        k = next(iter(r.values()))
        return (float(k["a"][0]) + float(k["b"][0])) / 2
    except Exception:
        return None


def upload(path: Path, bucket: str) -> None:
    try:
        subprocess.run(
            ["gcloud", "storage", "cp", str(path), f"gs://{bucket}/spot/{path.name}"],
            check=True,
            capture_output=True,
        )
        log(f"uploaded {path.name}")
        path.unlink()
    except Exception as error:
        # Keep the file; the next rotation retries the upload implicitly by
        # leaving it on disk for manual sweep. Losing data to a failed upload
        # would be worse than using some disk.
        log(f"upload failed for {path.name}: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="steps-kalshi-book")
    parser.add_argument("--workdir", default="/var/tmp/spot")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=5)
    current_hour = None
    handle = None

    log("spot sidecar starting")

    while True:
        started = time.time()
        hour = datetime.now(UTC).strftime("%Y%m%dT%H")

        if hour != current_hour:
            if handle is not None:
                handle.close()
                upload(workdir / f"spot_{current_hour}.jsonl", args.bucket)

            current_hour = hour
            handle = (workdir / f"spot_{hour}.jsonl").open("a")
            log(f"rotated to spot_{hour}.jsonl")

        try:
            btc = read_spot(client, "BTC-USD", "XBTUSD")
            eth = read_spot(client, "ETH-USD", "ETHUSD")

            if btc is not None or eth is not None:
                handle.write(
                    json.dumps({"t": time.time(), "btc": btc, "eth": eth}) + "\n"
                )
                handle.flush()
        except Exception as error:
            log(f"tick failed: {type(error).__name__} {error}")

        time.sleep(max(0.1, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
