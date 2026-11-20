"""Databento feed for the MTF Ripster dashboard - LIVE stream + PRE-MARKET HISTORICAL WARM-UP.

* Signals are driven by the Databento *Live* stream (`databento.Live`, schema `ohlcv-1m`).
* Before the open (or whenever the engine starts) the EMA/cloud warm-up is loaded in one shot from the Databento
  *Historical* API (`Engine.backfill`, schema `ohlcv-1m`, regular session only) into the same SQLite bar store, so
  the very first live bar already lands on fully-warmed 5/12, 34/50 and 180/200 EMAs. If the dashboard is started
  mid-session, the live subscription replays today's bars from 09:30 ET to close the gap.
* Every completed 1-minute bar (09:30-16:00 ET) is stored in SQLite. Candles (1M / 10M / 1H) are built from those bars.
* One Live connection per API key. Databento limits open connections, so the connection is held in a
  process-wide registry that survives Streamlit reruns and code reloads and is always closed before a new
  one is opened.
"""
from __future__ import annotations

import atexit
import builtins
import hashlib
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import time as dtime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

DATASET = os.getenv("DATABENTO_DATASET", "EQUS.MINI").strip().strip("\"'")
PUBLIC = os.getenv("PUBLIC_MODE", "0") == "1"
KEEP_DAYS = int(os.getenv("LIVE_KEEP_DAYS", "21"))      # calendar days of live bars kept for EMA warm-up
IDLE_STOP_S = int(os.getenv("LIVE_IDLE_STOP_S", "1200"))  # close the stream if no dashboard has asked for data
WARM_DAYS = int(os.getenv("WARMUP_DAYS", "20"))           # calendar days of history pulled for the pre-market warm-up
WARM_MIN_BARS = int(os.getenv("WARMUP_MIN_BARS", "3500"))  # ~9 sessions of 1M bars: below this a symbol gets a full pull
WARM_BATCH = int(os.getenv("WARMUP_BATCH", "50"))          # symbols per Historical request
NY = "America/New_York"
OHLCV = ["open", "high", "low", "close", "volume"]
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
UNSUPPORTED = {
    "SPX": "SPX is an index, not a US equity. Use SPY or a separately licensed index feed.",
    "NDX": "NDX is an index, not a US equity. Use QQQ or a separately licensed index feed.",
}
PERMANENT = ("authentication failed", "401", "403", "not authorized", "license", "entitle",
             "unknown dataset", "invalid dataset", "no such dataset")

# ---- process-wide registry (survives module reloads, so a reload can never leak a connection) ----
_G = getattr(builtins, "__mtf_live_state__", None)
if _G is None:
    _G = {"lock": threading.RLock(), "engines": {}}
    builtins.__mtf_live_state__ = _G
_LOCK, _ENGINES = _G["lock"], _G["engines"]


def _safe(exc) -> str:
    msg = re.sub(r"db-[A-Za-z0-9_\-]{12,}", "***", str(exc))
    key = os.getenv("DATABENTO_API_KEY", "")
    return (msg.replace(key, "***") if key else msg)[:400]


def _kid(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _valid_key(key) -> bool:
    return bool(key) and "your-key-here" not in str(key).lower()


def _db_path(dataset: str, api_key: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9]+", "_", dataset)
    if PUBLIC:  # hosted site: private, temporary store per visitor key
        return Path(tempfile.gettempdir()) / f"mtf_live_{_kid(api_key)}_{name}.db"
    return Path(__file__).parent / "data" / f"live_{name}.db"


class Engine:
    """One live connection + the stored 1-minute bars for its symbols."""

    def __init__(self, api_key, dataset, symbols, db_path, autostart=True):
        self.api_key, self.dataset = api_key, dataset
        self.symbols = tuple(sorted(set(symbols)))
        self.lock = threading.RLock()
        self.mem = {s: {} for s in self.symbols}       # ts_ns -> (o, h, l, c, v)
        self.ids, self._cache = {}, {}
        self.status, self.error = "starting", ""
        self.last_bar, self.last_msg, self.t_error = None, 0.0, 0.0
        self.last_access = time.time()
        self.stop_evt, self.client, self.thread = threading.Event(), None, None
        self.replay_ok = True
        self.warm = {"state": "idle", "bars": 0, "error": "", "symbols": len(self.symbols)}
        self.db = self._open_db(db_path)
        self._load()
        if autostart:
            self.thread = threading.Thread(target=self._run, daemon=True, name=f"databento-live-{_kid(api_key)}")
            self.thread.start()
            threading.Thread(target=self._warm_safe, daemon=True, name=f"databento-warmup-{_kid(api_key)}").start()
        else:
            self.status = "offline"

    # ---- storage ----
    def _open_db(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        db.execute("CREATE TABLE IF NOT EXISTS bars(sym TEXT NOT NULL, ts INTEGER NOT NULL, o REAL, h REAL, "
                   "l REAL, c REAL, v REAL, PRIMARY KEY(sym, ts))")
        db.commit()
        return db

    def _load(self):
        self.db.execute("DELETE FROM bars WHERE ts < ?", (time.time_ns() - KEEP_DAYS * 86_400 * 10**9,))
        self.db.commit()
        q = ",".join("?" * len(self.symbols))
        for sym, ts, o, h, l, c, v in self.db.execute(
                f"SELECT sym,ts,o,h,l,c,v FROM bars WHERE sym IN ({q}) ORDER BY ts", self.symbols):
            self.mem[sym][ts] = (o, h, l, c, v)

    def ingest(self, sym, ts_ns, o, h, l, c, v) -> bool:
        """Store one COMPLETED 1-minute bar (regular session only). Idempotent."""
        ts = pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(NY)
        if not (dtime(9, 30) <= ts.time() < dtime(16, 0)):
            return False
        with self.lock:
            if sym not in self.mem:
                return False
            self.mem[sym][ts_ns] = (o, h, l, c, v)
            try:
                self.db.execute("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?)", (sym, ts_ns, o, h, l, c, v))
                self.db.commit()
            except Exception as exc:
                self.error = _safe(exc)
            self.last_bar = ts
        return True

    def frame(self, sym):
        """Regular-session 1-minute bars, New York time, index 'start'."""
        with self.lock:
            d = self.mem.get(sym)
            if not d:
                return None
            key = (len(d), max(d))
            hit = self._cache.get(sym)
            if hit and hit[0] == key:
                return hit[1]
            items = sorted(d.items())
        idx = pd.to_datetime([k for k, _ in items], unit="ns", utc=True).tz_convert(NY)
        idx.name = "start"
        df = pd.DataFrame([v for _, v in items], index=idx, columns=OHLCV)
        self._cache[sym] = (key, df)
        return df

    # ---- pre-market historical warm-up ----
    def _warm_safe(self):
        try:
            self.backfill()
        except Exception as exc:
            self.warm.update(state="error", error=_safe(exc))
            print(f"[WARMUP] failed: {self.warm['error']} - falling back to live accumulation", flush=True)

    def backfill(self):
        """Load regular-session 1M history from the Databento Historical API into the bar store (idempotent).

        Symbols with fewer than WARM_MIN_BARS stored bars get the last WARM_DAYS days; symbols that already have
        enough only get a top-up from their last stored day. `end` is the newest data Databento has published
        (see _history_end); the live stream (with replay) covers anything after it."""
        import databento as db
        self.warm.update(state="running", bars=0, error="")
        now = pd.Timestamp.now(tz="UTC")
        full_start = (now - pd.Timedelta(days=WARM_DAYS)).normalize()
        need_full, topup, last_ts = [], [], []
        with self.lock:
            for sym in self.symbols:
                d = self.mem.get(sym) or {}
                if len(d) < WARM_MIN_BARS:
                    need_full.append(sym)
                else:
                    topup.append(sym)
                    last_ts.append(pd.Timestamp(max(d), unit="ns", tz="UTC"))
        jobs = []
        if need_full:
            jobs.append((need_full, full_start))
        if topup:
            jobs.append((topup, max(min(last_ts).normalize(), full_start)))
        client, total, bad = db.Historical(self.api_key), 0, []
        end = self._history_end(client, now)
        for syms, start in jobs:
            if start >= end:
                continue
            for i in range(0, len(syms), WARM_BATCH):
                if self.stop_evt.is_set():
                    return
                batch = syms[i:i + WARM_BATCH]
                try:
                    total += self._pull(client, batch, start, end)
                except Exception as exc:
                    # one bad/unknown ticker must not sink the batch: retry the batch symbol by symbol
                    print(f"[WARMUP] batch failed ({_safe(exc)}); retrying symbol by symbol", flush=True)
                    for sym in batch:
                        try:
                            total += self._pull(client, [sym], start, end)
                        except Exception as exc1:
                            bad.append(sym)
                            print(f"[WARMUP] {sym}: no history ({_safe(exc1)})", flush=True)
                self.warm["bars"] = total
        self.warm.update(state="done", error=("no history for: " + ", ".join(bad)) if bad else "")
        print(f"[WARMUP] {total:,} historical 1M bars loaded for {len(self.symbols) - len(bad)} of {len(self.symbols)} symbols "
              f"({self.dataset}); EMAs are pre-warmed", flush=True)

    def _pull(self, client, syms, start, end) -> int:
        df = client.timeseries.get_range(dataset=self.dataset, schema="ohlcv-1m", symbols=syms,
                                         stype_in="raw_symbol", start=start, end=end).to_df()
        return self._store_history(df)

    def _history_end(self, client, now):
        """Newest ohlcv-1m timestamp Databento has published (Historical requires an explicit `end` when `start`
        carries a time). Falls back to today's 00:00 UTC (a date boundary, always published) if the lookup fails."""
        try:
            rng = client.metadata.get_dataset_range(dataset=self.dataset)
            end = (rng.get("schema", {}).get("ohlcv-1m") or rng)["end"]
            end = pd.Timestamp(end)
            end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
            return min(end, now.floor("min"))
        except Exception as exc:
            print(f"[WARMUP] dataset range lookup failed ({_safe(exc)}); using yesterday's close as the end", flush=True)
            return now.normalize()

    def _store_history(self, df) -> int:
        """Insert historical 1M bars (regular session only, never overwriting live bars). Returns rows added."""
        if df is None or len(df) == 0:
            return 0
        df = df.reset_index()
        tcol = "ts_event" if "ts_event" in df else df.columns[0]
        ts = pd.DatetimeIndex(df[tcol])
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        local = ts.tz_convert(NY)
        mins = local.hour * 60 + local.minute
        ok = (mins >= 570) & (mins < 960) & (df[["open", "high", "low", "close"]].min(axis=1) > 0).to_numpy()
        ns = ts.as_unit("ns").asi8
        added, rows = 0, []
        with self.lock:
            for sym, t, o, h, l, c, v, good in zip(df["symbol"].astype(str).str.upper(), ns, df["open"], df["high"],
                                                   df["low"], df["close"], df["volume"], ok):
                d = self.mem.get(sym)
                if not good or d is None or int(t) in d:
                    continue
                d[int(t)] = (float(o), float(h), float(l), float(c), float(v))
                rows.append((sym, int(t), float(o), float(h), float(l), float(c), float(v)))
                added += 1
            if rows:
                self.db.executemany("INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?,?,?)", rows)
                self.db.commit()
        return added

    def _replay_start(self):
        """Mid-session start: ask the live gateway to replay today's bars from 09:30 ET so there is no gap."""
        if not self.replay_ok:
            return None
        now = pd.Timestamp.now(tz=NY)
        open_ = now.normalize() + pd.Timedelta(hours=9, minutes=30)
        if now.weekday() < 5 and now >= open_ + pd.Timedelta(minutes=2):
            return open_.tz_convert("UTC")
        return None

    # ---- stream ----
    def touch(self):
        self.last_access = time.time()

    @property
    def dead(self):
        if self.status == "stopped":
            return True
        return self.status == "error" and time.time() - self.t_error > 120

    def info(self):
        with self.lock:
            n = sum(len(d) for d in self.mem.values())
        return {"enabled": True, "status": self.status, "bars": n, "last_bar": self.last_bar,
                "error": self.error, "msg_age": (time.time() - self.last_msg) if self.last_msg else None,
                "symbols": len(self.symbols), "warm": dict(self.warm)}

    def _live(self):
        self.status = "live"
        self.error = ""

    def _on_record(self, rec):
        try:
            import databento as db
            self.last_msg = time.time()
            if isinstance(rec, db.SymbolMappingMsg):
                self.ids[rec.instrument_id] = rec.stype_in_symbol
                self._live(); return
            if isinstance(rec, db.SystemMsg):      # heartbeats keep the status live while the market is quiet
                self._live(); return
            if isinstance(rec, db.ErrorMsg):
                self.error = _safe(getattr(rec, "err", "Databento gateway error")); return
            if not isinstance(rec, db.OHLCVMsg):
                return
            sym = self.ids.get(rec.instrument_id) or getattr(self.client, "symbology_map", {}).get(rec.instrument_id)
            if sym is None or rec.close >= 2**62 or min(rec.open, rec.high, rec.low, rec.close) <= 0:
                return
            k = 1e-9
            if self.ingest(sym, int(rec.ts_event), rec.open * k, rec.high * k, rec.low * k, rec.close * k, float(rec.volume)):
                self._live()
        except Exception as exc:
            self.error = _safe(exc)

    def _on_exception(self, exc):
        self.error = _safe(exc)

    def _run(self):
        import databento as db
        backoff = 3
        while not self.stop_evt.is_set():
            client, t0, self.error, extra = None, time.time(), "", {}
            try:
                self.status = "connecting" if self.status in ("starting", "connecting") else "reconnecting"
                try:
                    client = db.Live(key=self.api_key, reconnect_policy="reconnect")
                except TypeError:
                    client = db.Live(key=self.api_key)
                self.client = client
                client.add_callback(self._on_record, self._on_exception)
                replay = self._replay_start()
                extra = {"start": replay} if replay is not None else {}
                client.subscribe(dataset=self.dataset, schema="ohlcv-1m", symbols=list(self.symbols),
                                 stype_in="raw_symbol", **extra)
                client.start()
                client.block_for_close()
            except Exception as exc:
                self.error = _safe(exc)
            finally:
                if client is not None:
                    try:
                        client.stop()
                    except Exception:
                        pass
            if self.stop_evt.is_set():
                break
            msg = self.error.lower()
            if extra and time.time() - t0 < 15 and "connection limit" not in msg:
                self.replay_ok = False       # gateway refused the replay start: reconnect plain, backfill covers history
            if "connection limit" in msg:
                self.status = "blocked"
                self.error = ("Databento says your open-connection limit is reached. Close other notebooks / terminals / "
                              "dashboards using this key, then press 'Reload connection'.")
                self.stop_evt.wait(45); continue
            if any(t in msg for t in PERMANENT):
                self.status, self.t_error = "error", time.time()
                return
            backoff = 3 if time.time() - t0 > 60 else min(backoff * 2, 60)
            self.status = "reconnecting"
            self.stop_evt.wait(backoff)
        self.status = "stopped"

    def stop(self):
        self.stop_evt.set()
        c = self.client
        if c is not None:
            try:
                c.stop()
            except Exception:
                pass
        t = self.thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(8)
        self.status = "stopped"
        try:
            self.db.close()
        except Exception:
            pass


def _stop_all():
    with _LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for e in engines:
        try:
            e.stop()
        except Exception:
            pass


_stop_all()                  # this module was (re)loaded: close any stream left by the previous copy
atexit.register(_stop_all)   # and close the stream when the app process exits


def _reap():
    now = time.time()
    for k in [k for k, e in _ENGINES.items() if now - e.last_access > IDLE_STOP_S]:
        threading.Thread(target=_ENGINES.pop(k).stop, daemon=True).start()


def ensure(symbols, api_key, dataset):
    """Return the single live engine for this key, (re)starting it only when really needed."""
    symbols, kid = tuple(sorted(set(symbols))), _kid(api_key)
    with _LOCK:
        _reap()
        eng = _ENGINES.get(kid)
        if eng is not None and (eng.dataset != dataset or eng.dead or not set(symbols) <= set(eng.symbols)):
            keep = set(eng.symbols) if eng.dataset == dataset else set()
            eng.stop()                        # close the old connection BEFORE opening the new one
            _ENGINES.pop(kid, None)
            symbols, eng = tuple(sorted(set(symbols) | keep)), None
        if eng is None:
            eng = Engine(api_key, dataset, symbols, _db_path(dataset, api_key),
                         autostart=os.getenv("MTF_NO_CONNECT") != "1")
            _ENGINES[kid] = eng
    eng.touch()
    return eng


def stop_live(api_key=None):
    with _LOCK:
        keys = [_kid(api_key)] if api_key else list(_ENGINES)
        engines = [_ENGINES.pop(k) for k in keys if k in _ENGINES]
    for e in engines:
        e.stop()


# ---------------- candles ----------------
def _bars(df, minutes) -> pd.DataFrame:
    """All candles (completed AND still-forming) anchored to 09:30 ET, with an 'end' column."""
    bars = df.resample(f"{minutes}min", origin="start_day", offset="9h30min").agg(AGG).dropna(subset=["close"])
    width = pd.Timedelta(minutes=minutes)
    bars["end"] = pd.DatetimeIndex(
        [min(t + width, t.normalize() + pd.Timedelta(hours=16)) for t in bars.index]).astype(bars.index.dtype)
    return bars


def _split(df, minutes, latest):
    """-> (completed candles, the single still-forming candle or an empty frame).

    The forming candle is built from the completed 1-minute bars received so far inside the open window, so its
    close is the latest live price and it updates every time a new 1M bar arrives."""
    if df is None or df.empty:
        e = pd.DataFrame(columns=OHLCV + ["end"])
        return e, e.copy()
    bars = _bars(df, minutes)
    done = bars["end"] <= latest + pd.Timedelta(minutes=1)
    return bars[done].sort_index(), bars[~done].sort_index().tail(1)


def _resample(df, minutes, latest) -> pd.DataFrame:
    """Completed candles only (unfinished candle discarded)."""
    return _split(df, minutes, latest)[0]


def _one_min(df) -> pd.DataFrame:
    r = df[df.index >= df.index[-1].normalize() - pd.Timedelta(days=5)].copy()
    r["end"] = r.index + pd.Timedelta(minutes=1)
    return r


_CANDLE_CACHE = {}


def build_candles(frames: dict):
    """{sym: 1m frame} -> ({sym: {"m1","m10","m10f","h1"}}, latest 1m bar start).
    m10 = completed 10M candles, m10f = the forming 10M candle (0 or 1 row)."""
    frames = {s: f for s, f in frames.items() if f is not None and len(f)}
    if not frames:
        return {}, None
    latest = max(f.index[-1] for f in frames.values())
    out = {}
    for s, f in frames.items():
        key = (len(f), f.index[-1], latest)          # unchanged symbol + unchanged clock -> reuse last build
        hit = _CANDLE_CACHE.get(s)
        if hit and hit[0] == key:
            out[s] = hit[1]
            continue
        m10, m10f = _split(f, 10, latest)
        out[s] = {"m1": _one_min(f), "m10": m10, "m10f": m10f, "h1": _resample(f, 60, latest)}
        _CANDLE_CACHE[s] = (key, out[s])
    return out, latest


def fetch_intraday(symbols, api_key=None, dataset=None, live=True):
    """Dashboard entry point: starts/keeps the live stream and returns candles built from live bars only."""
    dataset = (dataset or DATASET).strip()
    symbols = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    errors = {s: UNSUPPORTED[s] for s in symbols if s in UNSUPPORTED}
    supported = [s for s in symbols if s not in UNSUPPORTED]
    res = {"bars": {}, "errors": errors, "latest": None, "end": None,
           "live": {"enabled": bool(live), "status": "off", "bars": 0, "last_bar": None, "error": "",
                    "msg_age": None, "symbols": 0, "warm": {"state": "idle", "bars": 0, "error": "", "symbols": 0}}}
    key = api_key or os.getenv("DATABENTO_API_KEY", "")
    if not supported:
        return res
    if not _valid_key(key):
        errors.update({s: "No Databento API key provided." for s in supported})
        return res
    try:
        eng = ensure(supported, key, dataset) if live else _ENGINES.get(_kid(key))
    except Exception as exc:
        res["live"].update(status="error", error=_safe(exc)); eng = None
    if eng is None:
        errors.update({s: "Live stream is not running." for s in supported})
        return res
    res["live"].update(eng.info())
    bars, latest = build_candles({s: eng.frame(s) for s in supported})
    res["bars"], res["latest"] = bars, latest
    res["end"] = pd.Timestamp.now(tz="UTC").floor("min")
    for s in supported:
        if s not in bars:
            errors[s] = "Waiting for the first live 1-minute bar (market closed, or the stream is still connecting)."
    return res


fetch_intraday_raw = fetch_intraday  # alias used by earlier versions of the dashboard
