"""Dual dark/light themes (Eng-School style palette: deep indigo, magenta + violet accents)."""
import json
from pathlib import Path

PATH = Path(__file__).parent / "data" / "theme.json"
MODE = Path(__file__).parent / "data" / "mode.txt"
CFG = Path(__file__).parent / ".streamlit" / "config.toml"

PRESETS = {
    "Eng Dark":  dict(bg="#171428", panel="#221E3A", text="#E6E4F5", accent="#E94B9B", accent2="#6C5DD3", bullish="#2ED8A3", bearish="#FF4D79"),
    "Eng Light": dict(bg="#F4F3FA", panel="#FBFBFE", text="#1B1A33", accent="#E0338A", accent2="#5B4BD1", bullish="#0E9F6E", bearish="#E11D48"),
    "Obsidian Neon": dict(bg="#07080A", panel="#101216", text="#E8EAED", accent="#00FF85", accent2="#00B35E", bullish="#00FF85", bearish="#FF1F4B"),
    "Light Crisp": dict(bg="#F3F5F8", panel="#FBFCFD", text="#0B0F14", accent="#0057D9", accent2="#3B7BFF", bullish="#008A3E", bearish="#C8102E"),
    "Slate Dark": dict(bg="#111827", panel="#1F2937", text="#E2E8F0", accent="#60A5FA", accent2="#818CF8", bullish="#34D399", bearish="#F87171"),
    "Midnight":   dict(bg="#0B1020", panel="#141B2D", text="#D6DEEB", accent="#7AA2F7", accent2="#9D7CD8", bullish="#4ADE9E", bearish="#FF6B81"),
    "Charcoal":   dict(bg="#1A1A1A", panel="#262626", text="#D4D4D4", accent="#E0A458", accent2="#C2863B", bullish="#6FCF97", bearish="#EB6B6B"),
    "Soft Light": dict(bg="#ECE9E1", panel="#F4F2EC", text="#2B2B2B", accent="#3B6FB6", accent2="#5A8FD6", bullish="#1B7F4B", bearish="#B3261E"),
}
FIELDS = {"bg": "Background", "panel": "Cards / sidebar", "text": "Text", "accent": "Accent (pink)",
          "bullish": "Bullish colour", "bearish": "Bearish colour"}
_SOFT = {"bg": "#ECE9E1", "panel": "#F4F2EC", "text": "#E6E6E6"}


def soften(t: dict) -> dict:
    """Never use pure white (dark-mode glare)."""
    out = dict(t)
    for k, safe in _SOFT.items():
        if out[k].lower() in ("#fff", "#ffffff"):
            out[k] = safe
    return out


def _lum(h: str) -> float:
    h = h.lstrip("#"); r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def ink(color: str) -> str:
    """Readable text colour on top of a filled accent."""
    return "#0B0B14" if _lum(color) > 0.55 else "#FFFFFF"


def load_mode() -> str:
    try:
        return MODE.read_text().strip() or "dark"
    except Exception:
        return "dark"


def save_mode(mode: str):
    if mode != load_mode():
        MODE.parent.mkdir(exist_ok=True); MODE.write_text(mode)


def mode_theme(t: dict, light: bool) -> dict:
    """Header Dark/Light switch; keeps your chosen theme when it already matches the mode."""
    is_light = _lum(t["bg"]) > 0.5
    if light and not is_light: return soften(PRESETS["Eng Light"])
    if not light and is_light: return soften(PRESETS["Eng Dark"])
    return t


def load() -> dict:
    try:
        d = json.loads(PATH.read_text())
        if d.get("preset") in list(PRESETS) + ["Custom"] and all(k in d.get("custom", {}) for k in FIELDS):
            return d
    except Exception:
        pass
    return {"preset": "Eng Dark", "custom": dict(PRESETS["Eng Dark"])}


def save(preset: str, custom: dict):
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps({"preset": preset, "custom": custom}))


def write_streamlit_config(t: dict):
    CFG.parent.mkdir(exist_ok=True)
    CFG.write_text(f'[theme]\nbase = "{"light" if _lum(t["bg"]) > 0.5 else "dark"}"\n'
                   f'primaryColor = "{t["accent"]}"\nbackgroundColor = "{t["bg"]}"\n'
                   f'secondaryBackgroundColor = "{t["panel"]}"\ntextColor = "{t["text"]}"\n')


def css(t: dict) -> str:
    s = """<style>
.block-container{max-width:100% !important;padding:1.2rem 1.4rem !important}
.stApp, [data-testid="stHeader"], [data-testid="stAppViewContainer"]{background:__BG__ !important;color:__TEXT__}
[data-testid="stSidebar"], [data-testid="stSidebar"] > div{background:__PANEL__ !important}
[data-testid="stSidebar"]{border-right:1px solid __BORDER__}
.stApp p, .stApp label, .stApp span, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
[data-testid="stMetricValue"], [data-testid="stMetricLabel"], .stCaption, [data-testid="stCaptionContainer"]{color:__TEXT__ !important}
h1{font-weight:800 !important;letter-spacing:-.01em}
h2, h3{font-weight:700 !important}
[data-testid="stMetric"]{background:__PANEL__;border:1px solid __BORDER__;border-radius:14px;padding:12px 16px}
[data-testid="stMetricValue"]{font-weight:800 !important;font-size:1.5rem}
[data-testid="stMetricLabel"]{font-weight:600;opacity:.8}
div[data-baseweb="input"], div[data-baseweb="select"] > div, div[data-baseweb="base-input"], textarea{background:__PANEL__ !important;color:__TEXT__ !important;border-color:__BORDER__ !important;border-radius:10px !important}
input, textarea{color:__TEXT__ !important}
div[data-baseweb="popover"] ul, div[data-baseweb="popover"] li, [role="listbox"]{background:__PANEL__ !important;color:__TEXT__ !important}
.stButton > button, .stDownloadButton > button{background:linear-gradient(135deg,__ACCENT__,__A2__);border:0;border-radius:10px;font-weight:700}
.stButton > button p, .stDownloadButton > button p{color:#fff !important}
.stButton > button:hover, .stDownloadButton > button:hover{filter:brightness(1.1)}
button[data-baseweb="tab"]{color:__TEXT__;font-weight:700}
[data-baseweb="tab-highlight"]{background:__ACCENT__ !important}
details, [data-testid="stFileUploaderDropzone"]{background:__PANEL__ !important;border-color:__BORDER__ !important;border-radius:12px !important}
[data-testid="stDataFrame"]{border:1px solid __BORDER__;border-radius:14px}
div[role="radiogroup"]{gap:8px;flex-wrap:wrap}
div[role="radiogroup"] > label{border:1px solid __BORDER__;border-radius:999px;padding:4px 14px;background:__PANEL__;font-weight:700;margin:0}
div[role="radiogroup"] > label:has(input:checked){background:linear-gradient(135deg,__ACCENT__,__A2__);border-color:transparent}
div[role="radiogroup"] > label:has(input:checked) *{color:#fff !important}
div[role="radiogroup"] > label > div:first-child{display:none}
</style>"""
    for k, v in {"__BG__": t["bg"], "__PANEL__": t["panel"], "__TEXT__": t["text"], "__ACCENT__": t["accent"],
                 "__A2__": t.get("accent2", "#6C5DD3"), "__BORDER__": t["text"] + "33"}.items():
        s = s.replace(k, v)
    return s
