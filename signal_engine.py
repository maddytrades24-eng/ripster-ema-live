"""EMA-cloud signal engine (completed candles only) + SQLite signal history.

Timeframes
  1H  EMA 34/50            trend filter for BUY / SELL
  10M EMA 5/8/9/12         Reversal Trigger (forming candle) -> Confirmed Reversal (candle close),
                           10M Pullback Held, BUY / SELL / EXIT
  10M EMA 180/200          cloud-touch alert
Trigger = the FORMING 10M candle (built from the 1M bars received so far) is beyond the 5/12 zone.
Everything else uses completed candles only. Nothing fires until each EMA has seen at least its own period in
completed bars (warm-up gating), because the data comes only from the live stream and accumulates over time.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "signals.db"
TOL = 0.001          # 0.1% tolerance when a candle "touches" a zone
ZONE_PROX = 0.0015   # 0.15% "approaching" band around the 180/200 cloud
NEED_ZONE, NEED_1H, NEED_CLOUD = 12, 50, 200   # completed bars needed: 10M 5/12 | 1H 34/50 | 10M 180/200


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def warm_text(n10: int, n1h: int) -> str:
    parts = []
    if n10 < NEED_ZONE: parts.append(f"5/12 {n10}/{NEED_ZONE}")
    if n1h < NEED_1H: parts.append(f"1H {n1h}/{NEED_1H}")
    if n10 < NEED_CLOUD: parts.append(f"180/200 {n10}/{NEED_CLOUD}")
    return " · ".join(parts) if parts else "Ready ✓"


def _num(x):
    return "—" if pd.isna(x) else round(float(x), 2)


def _ev(i, r, sym, signal, why):
    return {"i": i, "symbol": sym, "signal": signal, "price": float(r.close),
            "bar_ts": r.start.isoformat(), "reason": why}


def _blank_row(sym, px, warm):
    d = "—"
    return {"Symbol": sym, "Last Price": _num(px) if px is not None else d, "1H Trend": "Warm-up", "1H EMA 34": d,
            "1H EMA 50": d, "1H Cloud": d, "10M EMA 5": d, "10M EMA 8": d, "10M EMA 9": d, "10M EMA 12": d,
            "10M Cloud": d, "Setup": "No setup", "Reversal": d, "Pullback": d, "180/200": d,
            "Signal": "WAIT", "Status": "WAIT", "Signal Time": d, "New": False, "NewSigs": [], "Candle": d,
            "Warm-up": warm, "180/200 Cloud": d, "vs 5/12": d, "Overall": "Warm-up"}


def analyze(sym, m10, h1, daily=None, tol=TOL, m3=None, m1=None, m10f=None):
    """Return (row, events). Completed candles drive everything except the Reversal Trigger, which reads the forming
    10M candle `m10f`. `daily`/`m3` are accepted but unused."""
    n10, n1h = len(m10), len(h1)
    warm = warm_text(n10, n1h)
    px = m1["close"].iloc[-1] if m1 is not None and len(m1) else None
    if n10 == 0:
        return _blank_row(sym, px, warm), []

    m = m10.copy()
    for n in (5, 8, 9, 12, 180, 200):
        m[f"e{n}"] = ema(m["close"], n)
    m["n"] = range(1, n10 + 1)
    m = m.reset_index()
    if n1h:
        h = h1.copy()
        h["e34"], h["e50"], h["hn"] = ema(h["close"], 34), ema(h["close"], 50), range(1, n1h + 1)
        right = h.reset_index()[["end", "e34", "e50", "hn"]].rename(columns={"end": "hend"})
        right["hend"] = right["hend"].astype(m["end"].dtype)
        m = pd.merge_asof(m.sort_values("end"), right.sort_values("hend"), left_on="end", right_on="hend")
    else:
        m["e34"], m["e50"], m["hn"] = float("nan"), float("nan"), 0
    top1, bot1 = m[["e34", "e50"]].max(axis=1), m[["e34", "e50"]].min(axis=1)
    ok1 = m["hn"].fillna(0) >= NEED_1H
    m["bull"] = ok1 & (m["close"] > top1) & (m["e34"] > m["e50"])
    m["bear"] = ok1 & (m["close"] < bot1) & (m["e34"] < m["e50"])

    pos, side, in_pb, in_zone = 0, None, False, False
    events, rev, pbs, zone = [], [], [], []
    for i, r in enumerate(m.itertuples()):
        if r.n >= NEED_ZONE:
            zt, zb = max(r.e5, r.e12), min(r.e5, r.e12)
            mid_hi, mid_lo = max(r.e8, r.e9), min(r.e8, r.e9)
            # Confirmed Reversal: a completed 10M candle closes through the WHOLE 5/12 zone from the opposite side
            brk = "Bullish" if r.close > zt else "Bearish" if r.close < zb else None
            flipped = False
            if brk:
                if side and side != brk:
                    rev.append(_ev(i, r, sym, f"Confirmed {brk} Reversal", f"10M candle CLOSED completely "
                                   f"{'above' if brk == 'Bullish' else 'below'} the 5/12 EMA zone"))
                    flipped = True
                side = brk
            # 10M Pullback Held (active trend): the candle pulls back into the 5/12 zone and holds, meaning it closes
            # on the trend side of the 8/9 reference OR never breaks through the opposite side of the 5/12 zone.
            if side == "Bullish" and r.e5 > r.e12:
                cond = (r.low <= zt * (1 + tol) and r.close >= zb
                        and (r.close >= mid_lo or r.low >= zb * (1 - tol)))
            elif side == "Bearish" and r.e5 < r.e12:
                cond = (r.high >= zb * (1 - tol) and r.close <= zt
                        and (r.close <= mid_hi or r.high <= zt * (1 + tol)))
            else:
                cond = False
            if cond and not in_pb and not flipped:
                pbs.append(_ev(i, r, sym, f"{side} 10M Pullback Held",
                               f"10M candle pulled back into the 5/12 EMA zone and held "
                               f"({'above' if side == 'Bullish' else 'below'} the 8/9 EMA / no break of the opposite side)"))
            in_pb = cond
            # BUY / SELL / EXIT: 1H 34/50 trend + 10M candle
            ev = None
            if pos == 1 and r.close < r.e12:
                ev, pos = ("EXIT LONG", f"10M close {r.close:.2f} below EMA12 {r.e12:.2f}"), 0
            elif pos == -1 and r.close > r.e12:
                ev, pos = ("EXIT SHORT", f"10M close {r.close:.2f} above EMA12 {r.e12:.2f}"), 0
            elif pos == 0 and r.bull and r.e5 > r.e12 and r.low <= r.e5 * (1 + tol) and r.close > r.e12 \
                    and r.low >= r.e12 * (1 - tol) and r.close > r.open and r.close > r.e5:
                ev, pos = ("BUY", "1H bullish; 10M 5/12 cloud held as support; bullish candle closed"), 1
            elif pos == 0 and r.bear and r.e5 < r.e12 and r.high >= r.e5 * (1 - tol) and r.close < r.e12 \
                    and r.high <= r.e12 * (1 + tol) and r.close < r.open and r.close < r.e5:
                ev, pos = ("SELL", "1H bearish; 10M 5/12 cloud held as resistance; bearish candle closed"), -1
            if ev:
                events.append(_ev(i, r, sym, ev[0], ev[1]))
        if r.n >= NEED_CLOUD:   # 180/200 cloud touch on completed 10M candles
            zt2, zb2 = max(r.e180, r.e200), min(r.e180, r.e200)
            hit = r.low <= zt2 * (1 + ZONE_PROX) and r.high >= zb2 * (1 - ZONE_PROX)
            if hit and not in_zone:
                zone.append(_ev(i, r, sym, "180/200 Cloud Touch",
                                f"10M candle touched/approached the EMA 180/200 cloud ({zb2:.2f}-{zt2:.2f})"))
            in_zone = hit

    # Reversal Trigger: the FORMING 10M candle (latest live price) is already beyond the 5/12 zone, coming from the
    # opposite side. EMAs are updated provisionally with the forming close (same recurrence as pandas ewm adjust=False).
    trig, f_start = [], None
    L0 = m.iloc[-1]
    if m10f is not None and len(m10f) and n10 >= NEED_ZONE and side:
        f = m10f.iloc[-1]
        f_start = m10f.index[-1]
        if f_start >= L0["end"]:
            a5, a12 = 2 / 6, 2 / 13
            e5f, e12f = a5 * f.close + (1 - a5) * L0.e5, a12 * f.close + (1 - a12) * L0.e12
            ztf, zbf = max(e5f, e12f), min(e5f, e12f)
            fr = pd.Series({"close": f.close, "start": f_start})
            if side == "Bearish" and f.close > ztf:
                trig.append(_ev(0, fr, sym, "Bullish Reversal Trigger",
                                f"Forming 10M candle broke ABOVE the 5/12 EMA zone from underneath ({ztf:.2f}); not yet closed"))
            elif side == "Bullish" and f.close < zbf:
                trig.append(_ev(0, fr, sym, "Bearish Reversal Trigger",
                                f"Forming 10M candle broke BELOW the 5/12 EMA zone from above ({zbf:.2f}); not yet closed"))
        else:
            f_start = None

    L, last = m.iloc[-1], len(m) - 1
    side_of = lambda sg: "Bullish" if "Bullish" in sg else "Bearish"
    reversal = next((f"{side_of(e['signal'])} Confirmed" for e in rev if e["i"] == last), "—")
    if trig:
        reversal = f"{side_of(trig[0]['signal'])} Trigger"
    pullback = next((e["signal"].split()[0] for e in pbs if e["i"] == last), "—")
    trend = "Warm-up" if not ok1.iloc[-1] else "Bullish" if L.bull else "Bearish" if L.bear else "Mixed"
    # ---- cloud trend snapshot (what the dashboard shows at the open) ----
    c10 = "Bullish" if L.e5 > L.e12 else "Bearish"
    c200 = "—" if n10 < NEED_CLOUD else "Bullish" if L.e180 > L.e200 else "Bearish"
    lastpx = float(px if px is not None else L.close)
    zpos = "—"
    if n10 >= NEED_ZONE:
        zpos = "Above" if lastpx > max(L.e5, L.e12) else "Below" if lastpx < min(L.e5, L.e12) else "Inside"
    if n10 < NEED_CLOUD or not ok1.iloc[-1]:
        overall = "Warm-up"
    elif trend == c10 == c200:
        overall = trend          # 1H 34/50, 10M 5/12 and 10M 180/200 all point the same way
    else:
        overall = "Mixed"
    last_ev = events[-1] if events else None
    is_new = bool(last_ev and last_ev["i"] == last)
    if is_new:
        signal = "EXIT" if last_ev["signal"].startswith("EXIT") else last_ev["signal"]
        status = "NEW " + last_ev["signal"] if last_ev["signal"] in ("BUY", "SELL") else last_ev["signal"]
    else:
        signal, status = "WAIT", {1: "LONG ACTIVE", -1: "SHORT ACTIVE", 0: "WAIT"}[pos]

    session = (f_start if f_start is not None else m["start"].iloc[-1]).date()
    allsig = rev + trig + pbs + zone
    today = lambda e: pd.Timestamp(e["bar_ts"]).date() == session
    ev_out = [{k: v for k, v in e.items() if k != "i"} for e in events + allsig if today(e)]
    new_sigs = [(e["signal"], e["bar_ts"]) for e in allsig if today(e)]
    recent = any(pd.Timestamp(e["bar_ts"]) >= m["start"].iloc[-1] for e in allsig)
    pb_now = ("Bullish Pullback Held" if in_pb and side == "Bullish"
              else "Bearish Pullback Held" if in_pb and side == "Bearish" else "No setup")
    row = {
        "Symbol": sym, "Last Price": _num(px if px is not None else L.close), "1H Trend": trend,
        "1H EMA 34": _num(L.e34), "1H EMA 50": _num(L.e50),
        "1H Cloud": "—" if pd.isna(L.e34) else ("Bullish" if L.e34 > L.e50 else "Bearish"),
        "10M EMA 5": _num(L.e5), "10M EMA 8": _num(L.e8), "10M EMA 9": _num(L.e9), "10M EMA 12": _num(L.e12),
        "10M Cloud": "Bullish" if L.e5 > L.e12 else "Bearish",
        "Setup": pb_now, "Reversal": reversal, "Pullback": pullback,
        "180/200": "Touching" if in_zone else "—", "Signal": signal, "Status": status,
        "Signal Time": last_ev["bar_ts"][:16].replace("T", " ") if last_ev else "—",
        "New": is_new or recent, "NewSigs": new_sigs, "Candle": L["start"].strftime("%Y-%m-%d %H:%M"),
        "Warm-up": warm, "180/200 Cloud": c200, "vs 5/12": zpos, "Overall": overall,
    }
    return row, ev_out


# ---------- SQLite signal history ----------
def _conn(path=None):
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY, symbol TEXT, signal TEXT, price REAL, bar_ts TEXT,
        reason TEXT, created_at TEXT, UNIQUE(symbol, signal, bar_ts))""")
    return c


def save_events(events, db_path=None) -> list:
    """Insert new signals (UNIQUE prevents duplicates), PRINT each new one to the terminal, return the new ones."""
    new = []
    with _conn(db_path) as c:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for e in events:
            cur = c.execute("INSERT OR IGNORE INTO signals(symbol,signal,price,bar_ts,reason,created_at) VALUES(?,?,?,?,?,?)",
                            (e["symbol"], e["signal"], e["price"], e["bar_ts"], e["reason"], now))
            if cur.rowcount == 1:
                new.append(e)
    for e in new:
        print(f"[SIGNAL] {e['bar_ts'][:16].replace('T', ' ')} ET | {e['symbol']:<6} | {e['signal']:<26} | "
              f"{e['price']:>10.2f} | {e['reason']}", flush=True)
    return new


def load_history(limit=2000, db_path=None) -> pd.DataFrame:
    with _conn(db_path) as c:
        return pd.read_sql_query(
            "SELECT symbol AS Symbol, signal AS Signal, price AS Price, bar_ts AS 'Candle Time (ET)', "
            "reason AS Reason, created_at AS 'Recorded (UTC)' FROM signals ORDER BY bar_ts DESC, id DESC LIMIT ?",
            c, params=(limit,))
