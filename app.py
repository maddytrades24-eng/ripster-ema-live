"""MTF Ripster Cloud - Live Watchlist Signals Dashboard (Streamlit)."""
import html
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

import databento_feed as feed
import signal_engine as eng
import theme as th

st.set_page_config(page_title="MTF Ripster Cloud", layout="wide", page_icon="📈")
WL = Path(__file__).parent / "watchlists" / "watchlist.csv"
SECTORS = ["Neo Cloud", "Memory", "Semiconductor", "Fintech", "Bank", "Cyber", "Cloud", "Bitcoin", "Space", "Optical"]
UNASSIGNED = "Unassigned"
# PUBLIC_MODE=1 (set on the hosted site): every visitor uses their OWN Databento key and has private watchlist/history/theme.
PUBLIC = os.getenv("PUBLIC_MODE", "0") == "1"
SID = st.session_state.setdefault("sid", uuid.uuid4().hex[:12])
DB = Path(tempfile.gettempdir()) / f"mtf_{SID}.db" if PUBLIC else None
STARTER = {"NBIS": "Neo Cloud",
           "CRWV": "Neo Cloud",
           "IREN": "Neo Cloud",
           "ORCL": "Neo Cloud",
           "DLR": "Neo Cloud",
           "APLD": "Neo Cloud",
           "CORZ": "Neo Cloud",
           "SNDK": "Memory",
           "MU": "Memory",
           "SKHY": "Memory",
           "WDC": "Memory",
           "STX": "Memory",
           "TSM": "Semiconductor",
           "AVGO": "Semiconductor",
           "SMH": "Semiconductor",
           "INTC": "Semiconductor",
           "AMD": "Semiconductor",
           "SOXL": "Semiconductor",
           "QCOM": "Semiconductor",
           "CDNS": "Semiconductor",
           "LRCX": "Semiconductor",
           "AEHR": "Semiconductor",
           "AFRM": "Fintech",
           "TOST": "Fintech",
           "TEM": "Fintech",
           "XYZ": "Fintech",
           "PYPL": "Fintech",
           "NU": "Fintech",
           "GS": "Bank",
           "SCHW": "Bank",
           "KRE": "Bank",
           "USB": "Bank",
           "NC": "Bank",
           "C": "Bank",
           "WFC": "Bank",
           "JPM": "Bank",
           "BAC": "Bank",
           "CRWD": "Cyber",
           "OKTA": "Cyber",
           "FTNT": "Cyber",
           "PANW": "Cyber",
           "S": "Cyber",
           "ZS": "Cyber",
           "NET": "Cyber",
           "TENB": "Cyber",
           "RBRK": "Cyber",
           "GTLB": "Cloud",
           "WDAY": "Cloud",
           "NOW": "Cloud",
           "TEAM": "Cloud",
           "DT": "Cloud",
           "MDB": "Cloud",
           "SNOW": "Cloud",
           "CRM": "Cloud",
           "HUBS": "Cloud",
           "PLTR": "Cloud",
           "DDOG": "Cloud",
           "COIN": "Bitcoin",
           "MSTR": "Bitcoin",
           "CRCL": "Bitcoin",
           "HOOD": "Bitcoin",
           "HUT": "Bitcoin",
           "ISIT": "Bitcoin",
           "BKSY": "Space",
           "ASTS": "Space",
           "RDW": "Space",
           "LUNR": "Space",
           "PL": "Space",
           "DXYZ": "Space",
           "RKLB": "Space",
           "SPCX": "Space",
           "AXTI": "Optical",
           "CRDO": "Optical",
           "LITE": "Optical",
           "POET": "Optical",
           "AAOI": "Optical",
           "GCW": "Optical",
           "FN": "Optical",
           "COHR": "Optical",
           "SPY": UNASSIGNED, "QQQ": UNASSIGNED}
SYM_RE = re.compile(r"^(?:[A-Za-z]+:)?([A-Za-z][A-Za-z0-9.\-]{0,9})$")


# ---------- watchlists (symbol, source, sector) ----------
def load_wl() -> pd.DataFrame:
    if PUBLIC:
        if "wl" not in st.session_state:
            st.session_state["wl"] = pd.DataFrame({"symbol": list(STARTER), "source": "custom", "sector": list(STARTER.values())})
        return st.session_state["wl"].copy()
    if WL.exists():
        df = pd.read_csv(WL)
        if "sector" not in df:  # older file without sectors
            df["sector"] = df["symbol"].map(STARTER).fillna(UNASSIGNED)
        df["sector"] = df["sector"].fillna(UNASSIGNED)
        return df
    return pd.DataFrame({"symbol": list(STARTER), "source": "custom", "sector": list(STARTER.values())})


def save_wl(df):
    if PUBLIC:
        st.session_state["wl"] = df.drop_duplicates(["symbol", "sector"])
        return
    WL.parent.mkdir(exist_ok=True)
    df.drop_duplicates(["symbol", "sector"]).to_csv(WL, index=False)


def clean(tokens):
    out = []
    for t in tokens:
        t = str(t).strip()
        m = SYM_RE.match(t)
        if m and not t.startswith("#") and m.group(1).upper() not in ("SYMBOL", "TICKER", "NAN"):
            out.append(m.group(1).upper())
    return out


def parse_tv(raw: bytes):
    text = raw.decode("utf-8", errors="ignore")
    try:
        df = pd.read_csv(pd.io.common.StringIO(text))
        col = next((c for c in df.columns if str(c).strip().lower() in ("symbol", "ticker")), None)
        if col:
            return clean(df[col].dropna())
    except Exception:
        pass
    return clean(re.split(r"[,\n\r\t ]+", text))  # TradingView "###SECTION,NASDAQ:NVDA,..." export


# ---------- sound alerts (Web Audio API) ----------
def voice_text(sym, sg):
    """Spoken announcement = ticker + signal type, e.g. "NVDA confirmed bullish reversal"."""
    if sg.startswith("Confirmed"): return f"{sym} {sg.lower()}"                       # NVDA confirmed bullish reversal
    if "Reversal Trigger" in sg: return f"{sym} {sg.lower()}"                          # NVDA bullish reversal trigger
    if "Pullback Held" in sg: return f"{sym} {sg.lower().replace('10m', '10 minute')}"  # NVDA bullish 10 minute pullback held
    return f"{sym} touching 180 200 EMA cloud"


def sound_component(items, tones, voice, nonce):
    """Web Audio tones + Web Speech announcements. items = [{"s": signal, "say": text}]."""
    js = """<script>
try{const P=window.parent;const AC=P.AudioContext||P.webkitAudioContext;
if(!P.__mtfAC){P.__mtfAC=new AC();}
const ctx=P.__mtfAC;
if(!P.__mtfUnlock){P.__mtfUnlock=1;P.document.addEventListener('click',()=>{ctx.resume();
 try{P.speechSynthesis.speak(new P.SpeechSynthesisUtterance(''));}catch(e){}});}
const T={"Bullish Reversal Trigger":{w:"sine",d:.1,n:[[659,0],[880,.11]]},
"Confirmed Bullish Reversal":{w:"sine",d:.14,n:[[523,0],[659,.12],[784,.24],[1047,.36]]},
"Bearish Reversal Trigger":{w:"sawtooth",d:.1,n:[[622,0],[466,.11]]},
"Confirmed Bearish Reversal":{w:"sawtooth",d:.14,n:[[784,0],[622,.12],[466,.24],[330,.36]]},
"Bullish 10M Pullback Held":{w:"triangle",d:.12,n:[[880,0],[880,.2]]},
"Bearish 10M Pullback Held":{w:"square",d:.12,n:[[220,0],[220,.2]]},
"180/200 Cloud Touch":{w:"triangle",d:.22,n:[[330,0],[330,.3],[494,.6]]}};
function tone(f,t,d,w){const o=ctx.createOscillator(),g=ctx.createGain(),n=ctx.currentTime+t;
o.type=w;o.frequency.value=f;g.gain.setValueAtTime(.0001,n);g.gain.exponentialRampToValueAtTime(.18,n+.02);
g.gain.exponentialRampToValueAtTime(.0001,n+d);o.connect(g);g.connect(ctx.destination);o.start(n);o.stop(n+d+.05);}
const S=__ITEMS__, TONES=__TONES__, VOICE=__VOICE__;
if(S.length){
 if(VOICE){try{const sp=P.speechSynthesis;S.forEach(x=>{sp.speak(new P.SpeechSynthesisUtterance(x.say));});}catch(e){}}
 ctx.resume().then(()=>{if(TONES){[...new Set(S.map(x=>x.s))].forEach(s=>{const c=T[s];if(!c)return;c.n.forEach(([f,t])=>tone(f,t,c.d,c.w));});}});
}
}catch(e){}
</script><!--__NONCE__-->"""
    html_ = (js.replace("__ITEMS__", json.dumps(items)).replace("__TONES__", json.dumps(bool(tones)))
             .replace("__VOICE__", json.dumps(bool(voice))).replace("__NONCE__", str(nonce)))
    if hasattr(st, "iframe"):  # newer Streamlit
        st.iframe(html_, height=1)
    else:
        components.html(html_, height=0)


def show_df(obj, **kw):
    try:
        st.dataframe(obj, width="stretch", **kw)
    except TypeError:  # older Streamlit
        st.dataframe(obj, use_container_width=True, **kw)


def _cls(x):
    x = str(x)
    if x == "BUY": return "buy"
    if x == "SELL": return "sell"
    if x.startswith("EXIT"): return "exit"
    if x == "Touching" or x.startswith("180/200"): return "touch"
    if "Bullish" in x or x.startswith(("LONG", "NEW BUY")): return "bull"
    if "Bearish" in x or x.startswith(("SHORT", "NEW SELL")): return "bear"
    return ""


def _html_table(t, T, max_h=560, widths=None, left=()):
    """Full-width (100%) fixed-layout table: all columns fit on screen, no horizontal scrolling."""
    bull, bear, a2 = T["bullish"], T["bearish"], T.get("accent2", "#6C5DD3")
    css = (f"<style>.sigwrap{{width:100%;max-height:{max_h}px;overflow-y:auto;overflow-x:hidden;border:1px solid {T['text']}22;border-radius:14px}}"
           f".sig{{width:100%;table-layout:fixed;border-collapse:collapse;font-size:clamp(9px,.78vw,13px)}}"
           f".sig th{{position:sticky;top:0;z-index:1;background:{T['panel']};color:{T['text']};font-weight:800;padding:8px 2px;"
           f"border-bottom:2px solid {T['accent']};overflow-wrap:anywhere}}"
           f".sig td{{background:{T['panel']};color:{T['text']};padding:6px 3px;border-bottom:1px solid {T['text']}18;"
           f"text-align:center;font-weight:600;overflow-wrap:anywhere}}.sig tr:nth-child(even) td{{background:{T['bg']}}}"
           f".sig td.l{{text-align:left;padding-left:8px}}"
           f".sig .bull{{color:{bull};font-weight:800}}.sig .bear{{color:{bear};font-weight:800}}"
           f".sig .touch{{color:{T['accent']};font-weight:800}}"
           f".sig .buy{{background:{bull} !important;color:{th.ink(bull)};font-weight:800}}"
           f".sig .sell{{background:{bear} !important;color:{th.ink(bear)};font-weight:800}}"
           f".sig .exit{{background:{a2} !important;color:#fff;font-weight:800}}</style>")
    cg = "<colgroup>" + "".join(f'<col style="width:{w}%">' for w in widths) + "</colgroup>" if widths else ""
    head = "".join(f"<th>{html.escape(c)}</th>" for c in t.columns)
    body = "".join("<tr>" + "".join(
        f'<td class="{_cls(x)}{" l" if c in left else ""}">{html.escape(str(x))}</td>'
        for c, x in zip(t.columns, row)) + "</tr>" for row in t.itertuples(index=False))
    st.markdown(f'{css}<div class="sigwrap"><table class="sig">{cg}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>',
                unsafe_allow_html=True)


def render_table(v, T):
    t = pd.DataFrame({
        "Symbol": v.Symbol, "Sector": v.Sector, "Price": v["Last Price"], "1H Trend": v["1H Trend"],
        "1H 34/50": v["1H EMA 34"].astype(str) + " / " + v["1H EMA 50"].astype(str),
        "10M Cloud": v["10M Cloud"],
        "10M 5/12": v["10M EMA 5"].astype(str) + " / " + v["10M EMA 12"].astype(str),
        "10M 8/9": v["10M EMA 8"].astype(str) + " / " + v["10M EMA 9"].astype(str),
        "Setup": v.Setup, "Reversal": v.Reversal, "Pullback": v.Pullback,
        "10M 180/200": v["180/200"], "Signal": v.Signal, "Status": v.Status, "Time": v["Signal Time"].str[-5:], "Warm-up": v["Warm-up"]})
    _html_table(t, T)


def render_cloud(d, T):
    """Cloud trend board: overall trend per symbol from the pre-warmed 1H 34/50, 10M 5/12 and 10M 180/200 clouds."""
    order = {"Bullish": 0, "Bearish": 1, "Mixed": 2, "Warm-up": 3}
    d = d.assign(_o=d["Overall"].map(order).fillna(4)).sort_values(["_o", "Symbol"])
    t = pd.DataFrame({"Symbol": d.Symbol, "Sector": d.Sector, "Price": d["Last Price"], "Overall": d.Overall,
                      "1H 34/50": d["1H Cloud"], "1H Price vs Cloud": d["1H Trend"], "10M 5/12": d["10M Cloud"],
                      "10M 180/200": d["180/200 Cloud"], "Price vs 5/12": d["vs 5/12"], "Warm-up": d["Warm-up"]})
    _html_table(t, T, max_h=360)


def render_log(h, T):
    """Dashboard twin of the terminal log: every new signal, newest first."""
    t = pd.DataFrame({"Time (ET)": h["Candle Time (ET)"].str[11:16], "Symbol": h["Symbol"], "Signal": h["Signal"],
                      "Price": h["Price"].round(2), "Reason": h["Reason"]})
    _html_table(t, T, max_h=320, widths=[8, 9, 21, 9, 53], left=("Reason",))


wl = load_wl()
with st.sidebar:
    st.header("Watchlist")
    sec_in = st.selectbox("Sector (for add / import)", SECTORS)
    up = st.file_uploader("Import TradingView watchlist (CSV/TXT)", type=["csv", "txt"])
    if up and st.button("Import symbols"):
        syms = parse_tv(up.getvalue())
        keep = wl[~((wl.source == "tradingview") & (wl.sector == sec_in))]
        wl = pd.concat([keep, pd.DataFrame({"symbol": syms, "source": "tradingview", "sector": sec_in})])
        save_wl(wl); st.success(f"Imported {len(syms)} symbols into {sec_in}"); st.rerun()
    new = st.text_input("Add tickers (comma separated)")
    if st.button("Add") and new:
        wl = pd.concat([wl, pd.DataFrame({"symbol": clean(new.split(",")), "source": "custom", "sector": sec_in})])
        save_wl(wl); st.rerun()
    labels = [f"{r.symbol} · {r.sector}" for r in wl.itertuples()]
    rm = st.multiselect("Remove", labels)
    if rm and st.button("Remove selected"):
        save_wl(wl[[l not in rm for l in labels]]); st.rerun()
    all_syms = list(dict.fromkeys(wl.symbol))  # combined, de-duplicated
    selected = st.multiselect("Symbols to scan", all_syms, default=all_syms)
    st.header("Refresh")
    refresh = st.slider("Auto-refresh (seconds)", 5, 600, 5, 5)
    st.caption("Data: Databento live stream, pre-warmed from Databento history before the open")
    st.header("Databento")
    env_key = os.getenv("DATABENTO_API_KEY", "")
    env_key = "" if env_key.endswith("your-key-here") else env_key
    user_key = st.text_input("Your Databento API key", type="password", placeholder="db-...",
                             help="Used only in this browser session; never stored or logged.") if (PUBLIC or not env_key) else ""
    api_key = user_key.strip() if PUBLIC else (user_key.strip() or env_key)
    dataset = st.text_input("Dataset", value=feed.DATASET, help="A US equities dataset your key has LIVE access to, e.g. EQUS.MINI")
    live_on = True
    if st.button("🔌 Reload connection", help="Closes the live stream and opens a fresh one (use after changing key/dataset "
                 "or if Databento reports an open-connection limit)."):
        feed.stop_live(api_key or None); st.rerun()
    live_box = st.empty()
    warm_box = st.empty()
    st.header("Theme")
    saved = th.load()
    names = list(th.PRESETS) + ["Custom"]
    preset = st.selectbox("Theme", names, index=names.index(saved["preset"]))
    base = saved["custom"] if preset == "Custom" else th.PRESETS[preset]
    T = dict(base)
    if preset == "Custom":
        for k, lab in th.FIELDS.items():
            T[k] = st.color_picker(lab, base[k], key=f"theme_{k}")
        st.caption("Pure white is automatically softened.")
    T = th.soften(T)
    if not PUBLIC and st.button("Save theme"):
        th.save(preset, T if preset == "Custom" else saved["custom"])
        th.write_streamlit_config(T)
        st.success("Saved. Refresh the browser if table headers keep the old colours.")

st_autorefresh(interval=refresh * 1000, key="auto")
h1c, h2c, h3c, h4c, h5c = st.columns([4, 1.4, 1.4, 1.5, 1])
h1c.title("📈 MTF Ripster Cloud — Signals")
sound_on = h2c.toggle("🔊 Sound", value=True, key="sound")
voice_on = h3c.toggle("🗣 Voice", value=True, key="voice")
light = h4c.toggle("☀️ Light mode", value=th.load_mode() == "light", key="light")
test = h5c.button("🔔 Test")
if not PUBLIC:
    th.save_mode("light" if light else "dark")
T = th.mode_theme(T, light)
st.markdown(th.css(T), unsafe_allow_html=True)


if not api_key:
    st.info("👈 Paste your **Databento API key** in the sidebar to start scanning. It stays in your browser session only "
            "(never stored or logged). No key? Create one at databento.com.")
    st.stop()
if not selected:
    st.info("Add or select symbols in the sidebar."); st.stop()

syms = tuple(selected)
intr = feed.fetch_intraday(list(syms), api_key, dataset)   # live stream + bars accumulated from it
errors = dict(intr["errors"])
lv = intr.get("live", {})
live_ok = lv.get("status") == "live"
_age = lv.get("msg_age")
if live_ok:
    lb = lv.get("last_bar")
    live_box.success(f"🟢 LIVE stream connected · {lv.get('bars', 0):,} live bars stored"
                     + (f" · last bar {lb.strftime('%H:%M')} ET" if lb is not None else " · waiting for first bar")
                     + (f" · heartbeat {_age:,.0f}s ago" if _age is not None else ""))
elif lv.get("status") in ("starting", "connecting", "reconnecting"):
    live_box.info(f"🟡 Live stream {lv.get('status')}…")
elif lv.get("status") == "blocked":
    live_box.error("🔴 " + (lv.get("error") or "Databento open-connection limit reached."))
else:
    live_box.warning("🔴 Live stream not connected" + (f": {lv['error']}" if lv.get("error") else "."))
wm = lv.get("warm") or {}
if wm.get("state") == "running":
    warm_box.info(f"⏳ Pre-warming indicators from history… {wm.get('bars', 0):,} bars loaded")
elif wm.get("state") == "done":
    warm_box.success(f"✓ Pre-warmed from history · {wm.get('bars', 0):,} bars loaded this session"
                     + (f" · {wm['error']}" if wm.get("error") else ""))
elif wm.get("state") == "error":
    warm_box.warning("History warm-up failed, so indicators fall back to live accumulation: " + (wm.get("error") or "unknown error"))
rows, events = [], []
for s, b in intr["bars"].items():
    try:
        row, ev = eng.analyze(s, b["m10"], b["h1"], None, m1=b.get("m1"), m10f=b.get("m10f"))
        rows.append(row); events += ev
    except Exception as e:
        errors[s] = str(e)[:200]
def _save_events(evs):
    try:
        return eng.save_events(evs, DB)
    except TypeError:  # older signal_engine.py without the db_path argument
        return eng.save_events(evs)


def _load_history(n):
    try:
        return eng.load_history(n, DB)
    except TypeError:
        return eng.load_history(n)


new_events = _save_events(events) if events else []  # also prints each new signal to the terminal
df = pd.DataFrame(rows)
sec_map = wl.groupby("symbol")["sector"].agg(", ".join).to_dict()
if len(df):
    df.insert(1, "Sector", df["Symbol"].map(sec_map))

# ---- new signals -> toast + tones + spoken announcement (silent on the first load of a session) ----
cur = {(r["Symbol"], sg, ts) for r in rows for sg, ts in r["NewSigs"]}
seen = st.session_state.get("seen")
fresh = sorted(k for k in cur if seen is not None and k not in seen)
st.session_state["seen"] = (seen or set()) | cur
for sym, sg, _ in fresh[:10]:
    st.toast(f"{sym}: {sg}")
if test:
    items = [{"s": sg, "say": voice_text(t, sg)} for t, sg in [
        ("AAPL", "Bullish Reversal Trigger"), ("AAPL", "Confirmed Bullish Reversal"),
        ("TSLA", "Bearish Reversal Trigger"), ("TSLA", "Confirmed Bearish Reversal"),
        ("NVDA", "Bullish 10M Pullback Held"), ("SPY", "180/200 Cloud Touch")]]
else:
    items = [{"s": sg, "say": voice_text(sym, sg)} for sym, sg, _ in fresh[:12]]
sound_component(items if (sound_on or voice_on or test) else [], sound_on or test, voice_on or test,
                f"{time.time()}" if items else "idle")

tab1, tab2 = st.tabs(["Scanner", "Signal history"])
with tab1:
    c = st.columns(9)
    cnt = lambda col, v: int((df[col] == v).sum()) if len(df) else 0
    has = lambda col, v: int(df[col].str.contains(v, regex=False).sum()) if len(df) else 0
    c[0].metric("Scanned", len(df)); c[1].metric("BUY", cnt("Signal", "BUY"))
    c[2].metric("SELL", cnt("Signal", "SELL")); c[3].metric("EXIT", cnt("Signal", "EXIT"))
    c[4].metric("WAIT", cnt("Signal", "WAIT"))
    c[5].metric("Active L / S", f"{cnt('Status', 'LONG ACTIVE')} / {cnt('Status', 'SHORT ACTIVE')}")
    c[6].metric("Reversals", f"{has('Reversal', 'Bullish')} ▲ / {has('Reversal', 'Bearish')} ▼")
    c[7].metric("Pullbacks Held", f"{cnt('Pullback', 'Bullish')} ▲ / {cnt('Pullback', 'Bearish')} ▼")
    c[8].metric("180/200 touch", cnt("180/200", "Touching"))

    latest = intr["latest"]
    if latest is not None:
        lag = (pd.Timestamp.now(tz="UTC") - latest.tz_convert("UTC")).total_seconds() / 60
        st.caption(f"Latest live 1m bar: **{latest.strftime('%Y-%m-%d %H:%M ET')}** (≈{lag:,.0f} min ago) · "
                   "Live stream + pre-market history · Reversal Trigger = forming 10M candle (updates each 1M bar) · Confirmed Reversal / Pullback Held / "
                   "BUY·SELL·EXIT / 180-200 = completed 10M candles.")
        if (df["Warm-up"] != "Ready ✓").any() if len(df) else False:
            nw = df[df["Warm-up"] != "Ready ✓"]
            st.info(f"⏳ **Still warming up ({len(nw)}):** " + ", ".join(f"{a} ({b})" for a, b in zip(nw["Symbol"], nw["Warm-up"]))
                    + ". Everything else is fully warm from history. A symbol stays here when Databento has too little history "
                    "for it (new listing, or ticker missing from the feed); it unlocks as live bars accumulate.")
    if new_events:
        st.toast(f"{len(new_events)} new signal(s) recorded")

    if len(df):
        et = pd.Timestamp.now(tz="America/New_York")
        is_open = et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)
        st.subheader("Cloud trend" + (" · market open" if is_open else " · pre-market (clouds pre-warmed from the last completed candles)"))
        ct = df["Overall"].value_counts()
        k = st.columns(4)
        k[0].metric("Bullish", int(ct.get("Bullish", 0))); k[1].metric("Bearish", int(ct.get("Bearish", 0)))
        k[2].metric("Mixed", int(ct.get("Mixed", 0))); k[3].metric("Warming up", int(ct.get("Warm-up", 0)))
        st.caption("Overall = Bullish/Bearish only when 1H 34/50, 10M 5/12 and 10M 180/200 all agree; otherwise Mixed. "
                   "Price vs 5/12 = where the live price sits relative to the 10M 5/12 zone.")
        render_cloud(df, T)

    present = [s for s in SECTORS + [UNASSIGNED] if (wl.sector == s).any()]
    sector_view = st.radio("Sector", ["All"] + SECTORS + ([UNASSIGNED] if UNASSIGNED in present else []),
                           horizontal=True, label_visibility="collapsed")
    f1, f2, f3 = st.columns([2, 2, 1])
    flt = f1.selectbox("Filter", ["All", "BUY only", "SELL only", "EXIT LONG", "EXIT SHORT", "WAIT",
                                  "Bullish 1H cloud", "Bearish 1H cloud", "Bullish reversal trigger", "Confirmed bullish reversal",
                                  "Bearish reversal trigger", "Confirmed bearish reversal", "Bullish 10M pullback held",
                                  "Bearish 10M pullback held", "180/200 touching"])
    q = f2.text_input("Search ticker").upper().strip()
    only_new = f3.checkbox("New signals only")
    v = df.copy()
    if sector_view != "All":
        members = set(wl[wl.sector == sector_view].symbol)
        if not members:
            st.info(f"No tickers in {sector_view} yet — add some from the sidebar.")
        v = v[v.Symbol.isin(members)] if len(v) else v
    if len(v):
        masks = {"BUY only": v.Signal == "BUY", "SELL only": v.Signal == "SELL",
                 "EXIT LONG": v.Status == "EXIT LONG", "EXIT SHORT": v.Status == "EXIT SHORT",
                 "WAIT": v.Signal == "WAIT", "Bullish 1H cloud": v["1H Trend"] == "Bullish",
                 "Bearish 1H cloud": v["1H Trend"] == "Bearish",
                 "Bullish reversal trigger": v.Reversal == "Bullish Trigger",
                 "Confirmed bullish reversal": v.Reversal == "Bullish Confirmed",
                 "Bearish reversal trigger": v.Reversal == "Bearish Trigger",
                 "Confirmed bearish reversal": v.Reversal == "Bearish Confirmed",
                 "Bullish 10M pullback held": v.Pullback == "Bullish", "Bearish 10M pullback held": v.Pullback == "Bearish",
                 "180/200 touching": v["180/200"] == "Touching"}
        if flt in masks: v = v[masks[flt]]
        if q: v = v[v.Symbol.str.contains(q)]
        if only_new: v = v[v.New]
        render_table(v, T)
    if not len(df):
        st.info("⏳ Waiting for the first completed live 1-minute bars. They arrive once the stream is connected and the US market "
                "is open (Mon–Fri 9:30–16:00 ET). Bars from earlier sessions are kept on disk, so this only happens on a fresh install.")
    st.subheader("Live signal log")
    st.caption("Every new signal is also printed in the server terminal as `[SIGNAL] ...`.")
    log = _load_history(30)
    if len(log):
        render_log(log, T)
    else:
        st.info("No signals recorded yet.")
    if errors:
        with st.expander(f"⚠️ {len(errors)} symbol(s) with errors / no data"):
            for s, e in errors.items():
                st.write(f"**{s}** — {e}")

with tab2:
    hist = _load_history(2000)
    st.download_button("Download history CSV", hist.to_csv(index=False), "signal_history.csv", "text/csv")
    show_df(hist, hide_index=True)

st.divider()
st.caption("Educational tool only — not financial advice. Signals are built from the Databento live stream and can be wrong or late. "
           "Market data is fetched with your own Databento key and subject to Databento's terms.")