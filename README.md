# Kalshi MM Bot

Small Kalshi market-making workbench for viewing orderbooks, recording/replaying markets, backtesting, and running live tests.

## Setup

Requires Python 3.14+ and a `.env` file in the project root:

```env
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=secrets/Kalshi Private API Key.pem
```

Install dependencies if needed:

```powershell
python -m pip install -e .
```

## Run

Open the GUI workbench:

```powershell
.\venv\Scripts\python.exe scripts\orderbook_viewer.py
```

The workbench is the main entry point. It includes live orderbook viewing, recording, replay/backtesting, optimization, and live trading.

## Anecdotal Live Results

Four 10-minute live snippets were run inside 15-minute markets, stopping roughly five minutes before close.

| Strategy | Run Deltas | Total |
| --- | --- | ---: |
| Adaptive | +$0.60, +$0.24, +$0.12, -$0.45 | +$0.51 |
| Dumb baseline | -$0.75, -$0.40, -$0.25, -$0.22 | -$1.62 |

The adaptive strategy outperformed the dumb baseline by a wide margin in this small sample. These are dollar deltas, not percentage returns, because current settings do not use the full account balance and scaling is unproven.

Live execution can lose money. Keep order sizes small, stop before close while testing, and verify no bot-prefixed orders remain resting after shutdown.
