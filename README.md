# MTF Ripster Cloud – Signals Dashboard (LIVE + pre-market history warm-up)

Signals run on the Databento **Live** API (`databento.Live`, schema `ohlcv-1m`). Indicator warm-up is loaded before the
open from the Databento **Historical** API (same schema). Completed 1-minute bars (09:30–16:00 ET) are saved in
`data/live_<dataset>.db` and aggregated into 1M / 10M / 1H candles.

## Signals
| Signal | Rule |
|---|---|
| **Bullish / Bearish Reversal Trigger** | the **forming** 10M candle (latest live price) breaks beyond the 10M EMA 5/12 zone from the opposite side; announced immediately, once per candle |
| **Confirmed Bullish / Bearish Reversal** | that 10M candle **closes** beyond the 5/12 zone |
| **Bullish / Bearish 10M Pullback Held** | in an active trend a completed 10M candle pulls back into the 5/12 zone and holds (trend side of the 8/9 EMA, or the opposite side of the zone is not broken) |
| BUY / SELL / EXIT | completed 10M candles + 1H EMA 34/50 trend; EXIT on a 10M close beyond EMA 12 |
| 180/200 Cloud Touch | a completed 10M candle touches/approaches the 10M EMA 180/200 cloud (0.15% band) |

Every new signal prints in the terminal as `[SIGNAL] ...`, is saved to SQLite, appears in the **Live signal log**, and is
spoken as ticker + signal type (e.g. "NVDA bullish reversal trigger", "NVDA confirmed bullish reversal",
"NVDA bullish 10 minute pullback held").
The forming candle is built from completed 1M bars, so a Trigger can lag the real price by up to one minute.

## Pre-market warm-up (no more waiting after the open)
An EMA is only trusted after it has seen its own period in completed candles (`10M 5/12` = 12 bars, `1H 34/50` = 50 bars,
`10M 180/200` = 200 bars). Instead of waiting for those bars to arrive live, the dashboard loads the last
`WARMUP_DAYS` (default 20) calendar days of regular-session 1M bars from the Databento **Historical** API as soon as it
starts, into the same SQLite store the live stream writes to. Start it any time before 09:30 ET and the clouds are
fully warm and visible (see **Cloud trend** at the top of the Scanner tab) before the first live bar.

* `python prewarm.py` does the same load without opening the dashboard or a live connection (optional, schedulable).
* Started mid-session? The live subscription replays today's bars from 09:30 ET, so there is no gap.
* Safe to repeat: bars are de-duplicated and live bars are never overwritten. Restarts reuse what is on disk.
* If the Historical request fails (plan/entitlement, network), the sidebar says so and the dashboard falls back to the
  old behaviour: signals stay gated by the **Warm-up** column until enough live bars accumulate.
* Settings (optional, `.env`): `WARMUP_DAYS`, `WARMUP_MIN_BARS`, `WARMUP_BATCH`.

## Databento connection limit
Databento limits open Live connections per key. This app keeps **one** connection per key and closes it before opening a
new one, but a Jupyter notebook, a second terminal, or another dashboard using the same key also counts.
If you see "open connection limit": close those, then press **Reload connection** in the sidebar.

## Run (Windows PowerShell, inside this folder)
    python -m venv .venv          # first time only
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt     # first time only
    python -m streamlit run app.py
Put `DATABENTO_API_KEY` and `DATABENTO_DATASET` in `.env` (or paste the key in the sidebar).
Optional recorder (no browser): `.\.venv\Scripts\python.exe record_live.py`

Educational tool, not financial advice.
