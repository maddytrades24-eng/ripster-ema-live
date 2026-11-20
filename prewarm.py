"""Pre-market warm-up: load history into the local bar store BEFORE the open (no live connection is opened).

    python prewarm.py            (run any time before 09:30 ET; takes seconds to a minute)

The dashboard does the same thing automatically when it starts, so this script is optional. Use it (e.g. from Windows
Task Scheduler) when you want the data already on disk before you ever open the dashboard. Safe to run repeatedly:
bars are de-duplicated and live bars are never overwritten. It does not count against Databento's open-connection limit.
"""
import csv
import os
from pathlib import Path

import databento_feed as feed
import signal_engine as eng

key = os.getenv("DATABENTO_API_KEY", "")
if not feed._valid_key(key):
    raise SystemExit("Set DATABENTO_API_KEY in .env first.")
wl = Path(__file__).parent / "watchlists" / "watchlist.csv"
syms = [r["symbol"].strip().upper() for r in csv.DictReader(open(wl))] if wl.exists() else ["SPY", "QQQ", "NVDA", "AMD"]
syms = [s for s in dict.fromkeys(syms) if s not in feed.UNSUPPORTED]
print(f"Pre-warming {len(syms)} symbols from {feed.DATASET} (last {feed.WARM_DAYS} days, regular session)...", flush=True)

e = feed.Engine(key, feed.DATASET, syms, feed._db_path(feed.DATASET, key), autostart=False)
try:
    e.backfill()
    cands, latest = feed.build_candles({s: e.frame(s) for s in syms})
    ready = 0
    for s in syms:
        b = cands.get(s)
        txt = eng.warm_text(len(b["m10"]), len(b["h1"])) if b else "no data"
        ready += txt == "Ready ✓"
        print(f"  {s:<6} {txt}")
    print(f"{ready}/{len(syms)} symbols fully warm. Last stored bar: {latest.strftime('%Y-%m-%d %H:%M ET') if latest is not None else '-'}")
finally:
    e.stop()
