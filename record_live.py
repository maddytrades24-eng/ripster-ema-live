"""Optional headless recorder: keeps the LIVE stream running and saving bars without opening the dashboard.

    python record_live.py            (leave it running during market hours; Ctrl+C to stop)

Why: with live-only data the EMAs need history to accumulate (see README). Do NOT run this and the dashboard at the
same time with the same key - Databento limits open connections. Run one or the other.
"""
import csv, os, sys, time
from pathlib import Path

import databento_feed as feed

key = os.getenv("DATABENTO_API_KEY", "")
wl = Path(__file__).parent / "watchlists" / "watchlist.csv"
syms = [r["symbol"].strip().upper() for r in csv.DictReader(open(wl))] if wl.exists() else ["SPY", "QQQ", "NVDA", "AMD"]
syms = [s for s in dict.fromkeys(syms) if s not in feed.UNSUPPORTED]
once = "--once" in sys.argv
print(f"Recording {len(syms)} symbols from {feed.DATASET} (live only). Ctrl+C to stop.", flush=True)
try:
    while True:
        r = feed.fetch_intraday(syms, key, feed.DATASET)
        lv = r["live"]
        print(time.strftime("%H:%M:%S"), lv["status"], f"| stored bars: {lv['bars']:,}",
              f"| last bar: {lv['last_bar'].strftime('%H:%M ET') if lv['last_bar'] is not None else '-'}",
              f"| {lv['error']}" if lv["error"] else "", flush=True)
        if once:
            break
        time.sleep(60)
except KeyboardInterrupt:
    pass
finally:
    feed.stop_live()
