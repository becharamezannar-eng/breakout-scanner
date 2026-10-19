#!/usr/bin/env python3
"""
Monday Breakout Scanner
=======================
Scans the S&P/TSX Composite, S&P/TSX 60, S&P 500, Dow Jones Industrial Average,
Nasdaq Composite and Russell 2000 for volume-confirmed breakouts out of tight
consolidations, with priority on bottoming breakouts.

Rules coded in (all tunable in CFG below):
  STRUCTURE   tight range / coil of 3-40 candles under a flat-ish top
              (>= 2 candles touching resistance, base depth limited),
              last close of the base pressing into the upper half of the range
  BREAKOUT    candle CLOSES above resistance (not just a wick)
              wide-range candle (range >= 1.1x ATR14)
              volume >= 1.5x its 50-day average (>= 2x scores higher)
              small upper wick (<= 30% of the candle's range) and close in top 40%
  HOLD        if the breakout was the prior session, the next candle must close
              above resistance with no low more than 1% below it
  AVOID       low volume, long upper wick, late entries (> 6% above resistance
              or > 30% above the 50-day), no consolidation (random spike)
  PATTERNS    Bottoming:    double bottom / rounded bottom / flat base after a drop
              Continuation: bull flag / ascending triangle / flat-top breakout
  PRESSURE    higher lows into resistance, rising volume in the base,
              range contraction, number of times resistance was tested (120 days)

Usage
-----
  pip install yfinance pandas numpy matplotlib requests lxml
  python breakout_scanner.py                 # full scan, all six indexes
  python breakout_scanner.py --open          # ...and open the HTML report
  python breakout_scanner.py --indexes sp500,tsx60
  python breakout_scanner.py --tickers my_list.txt
  python breakout_scanner.py --selftest      # offline check of the detection logic

Run before Monday's open: it uses the last COMPLETED daily candle (Friday's),
and drops a still-forming candle if run during market hours.

Output: breakouts_YYYY-MM-DD.csv and breakouts_YYYY-MM-DD.html in --outdir.

This is a technical screen, not investment advice. Always check the chart.
"""

import argparse
import base64
import datetime as dt
import html as html_mod
import io
import re
import os
import sys
import time
import warnings
import webbrowser
from io import StringIO
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# --------------------------------------------------------------------------
# Tunable rules
# --------------------------------------------------------------------------
CFG = dict(
    min_price=5.0,            # skip penny stocks
    min_dollar_vol=5e6,       # avg 50-day $ volume (day-trading liquidity)
    min_dollar_vol_ca=3e6,    # TSX floor in CAD: Canadian names trade less, and Canada is the priority market
    catchup_days=3,           # also catch breakouts up to 3 sessions old that were missed (if they held every day)
    radar_dist=0.03,          # "On the radar": close within 3% under a flat resistance, not broken out yet
    radar_max=25,             # radar names kept per market (Canada and US separately)
    min_vol_ratio=1.5,        # breakout volume vs 50-day average (hard floor)
    strong_vol_ratio=2.0,     # "ideal" volume spike
    max_wick_frac=0.30,       # upper wick / candle range (rejection filter)
    min_close_pos=0.60,       # close location within candle range (0=low,1=high)
    min_range_atr=1.1,        # wide-range candle: range vs ATR14
    min_base=3,               # tight range: at least 3 candles
    max_base=60,              # up to ~12 weeks (well-developed swing bases)
    tight_short=0.06,         # max base depth for bases <= 10 candles
    tight_long=0.20,          # max base depth for a 60-candle base (40 candles ~ 14%)
    touch_tol=0.015,          # high within 1.5% of resistance = a touch
    min_touches=2,
    breakout_buffer=0.002,    # close must clear resistance by 0.2%...
    min_clear_atr=0.10,       # ...and by at least 0.1 ATR (no trivial penetration)
    max_ext_above_res=0.06,   # > 6% above resistance = late entry, rejected
    max_ext_sma50=0.30,       # > 30% above 50-day = extended, rejected
    hold_tol=0.01,            # next candle low may dip at most 1% under resistance
    bottom_drawdown=0.20,     # decline needed before a "bottoming" base
    slippage=0.001,           # 0.1% per side, applied to entry and exit in R:R and backtest
    min_rr=2.0,               # minimum reward:risk for a Buy
    max_atr_pct=0.08,         # daily ATR above 8% of price = erratic
    earnings_block_days=5,    # no new Buy with earnings inside this many sessions
    # Scorecard thresholds, set from the backtest (Oct 2026, 1,316 trades, 2022-2026):
    # score 70+ with R:R 2+ averaged +0.19R/trade (PF 1.40, 355 trades); under 65 lost money.
    score_buy=70,             # checklist scorecard: >= 70 Buy candidate (checklist proposed 80)
    score_watch=60,           #                      60-69 watch, < 60 rejected (checklist proposed 65)
    max_warnings=4,           # 4+ false-breakout warnings blocks a Buy
)

# Position sizing - set these to your own account
ACCOUNT = dict(
    size=25_000,              # account value
    risk_pct=0.01,            # risk per trade (1% = $250 on $25k)
    max_position_pct=0.25,    # never put more than 25% of the account in one stock
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) breakout-scanner"}
ET = ZoneInfo("America/Toronto")

INDEX_NAMES = {
    "tsx": "S&P/TSX Composite",
    "tsx60": "S&P/TSX 60",
    "sp500": "S&P 500",
    "djia": "DJIA",
    "nasdaq": "Nasdaq Composite",
    "r2000": "Russell 2000",
}

# --------------------------------------------------------------------------
# Index constituents
# --------------------------------------------------------------------------
def _wiki_tables(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(StringIO(r.text))
    for t in tables:
        t.columns = [c[-1] if isinstance(c, tuple) else str(c) for c in t.columns]
    return tables


def _pick_col(tables, names, lo, hi):
    for t in tables:
        for n in names:
            if n in t.columns and lo <= len(t) <= hi:
                return t[n].dropna().astype(str).str.strip().tolist()
    raise ValueError("constituent table not found")


def _us(sym):
    return sym.replace(".", "-").replace(" ", "").upper()


def _tsx(sym):
    sym = sym.split(":")[-1].strip().upper()
    return sym.replace(".", "-") + ".TO"


def get_sp500():
    t = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    return [_us(s) for s in _pick_col(t, ["Symbol"], 400, 600)]


DJIA_FALLBACK = ["AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
                 "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
                 "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT"]


def get_djia():
    try:
        t = _wiki_tables("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
        for tab in t:
            col = next((c for c in tab.columns if "symbol" in str(c).lower()), None)
            if col and 25 <= len(tab) <= 35:
                syms = [_us(x.split(":")[-1]) for x in tab[col].dropna().astype(str)]
                if len(syms) >= 28:
                    return syms
    except Exception:
        pass
    DIAG.append("DJIA: Wikipedia table unavailable, used built-in 30-stock list")
    return list(DJIA_FALLBACK)


def get_tsx():
    t = _wiki_tables("https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index")
    return [_tsx(s) for s in _pick_col(t, ["Ticker", "Symbol"], 150, 320)]


def get_tsx60():
    t = _wiki_tables("https://en.wikipedia.org/wiki/S%26P/TSX_60")
    return [_tsx(s) for s in _pick_col(t, ["Symbol", "Ticker"], 50, 70)]


def get_nasdaq():
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), sep="|", dtype=str, keep_default_na=False)
    df = df[~df["Symbol"].str.startswith("File Creation")]
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
    bad = df["Security Name"].astype(str).str.contains(
        r"Warrant|\bUnits?\b|\bRights?\b|Preferred|Notes due|Debenture", case=False, regex=True)
    df = df[~bad]
    syms = df["Symbol"].astype(str)
    syms = syms[~syms.str.contains(r"[\$\.\^]", regex=True)]
    return [_us(s) for s in syms]


def _ishares_r2000():
    url = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
           "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund")
    hdr = dict(UA, Accept="text/csv,*/*", Referer="https://www.ishares.com/us/products/239710/")
    r = requests.get(url, headers=hdr, timeout=60)
    r.raise_for_status()
    lines = r.text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip('\ufeff"').startswith("Ticker")), None)
    if start is None:
        raise ValueError(f"iShares returned no holdings table (HTTP {r.status_code}, {len(r.text)} bytes)")
    df = pd.read_csv(StringIO("\n".join(lines[start:])), on_bad_lines="skip", dtype=str)
    df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    syms = df["Ticker"].astype(str).str.strip()
    syms = syms[syms.str.match(r"^[A-Z][A-Z0-9 .\-]*$")]
    out = [_us(x) for x in syms]
    if len(out) < 1500:
        raise ValueError(f"only {len(out)} holdings parsed")
    return out


def _nyse_listed():
    """NYSE / NYSE American common stocks - fallback universe for Russell 2000 names
    not already covered by the Nasdaq Composite."""
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), sep="|", dtype=str, keep_default_na=False)
    df = df[~df["ACT Symbol"].str.startswith("File Creation")]
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N") & df["Exchange"].isin(["N", "A"])]
    bad = df["Security Name"].str.contains(
        r"Warrant|\bUnits?\b|\bRights?\b|Preferred|Notes due|Debenture|Depositary Shares", case=False, regex=True)
    syms = df.loc[~bad, "ACT Symbol"]
    syms = syms[~syms.str.contains(r"[\$\.\^]", regex=True)]
    return [_us(x) for x in syms]


def get_r2000():
    try:
        return _ishares_r2000()
    except Exception as e:
        DIAG.append(f"Russell 2000: iShares holdings unavailable ({str(e)[:100]}); "
                    "using NYSE-listed stocks + Nasdaq Composite as the small-cap universe")
        INDEX_NAMES["r2000"] = "NYSE-listed (R2000 proxy)"
        return _nyse_listed()


FETCHERS = dict(tsx=get_tsx, tsx60=get_tsx60, sp500=get_sp500,
                djia=get_djia, nasdaq=get_nasdaq, r2000=get_r2000)


DIAG = []   # diagnostics shown at the bottom of the report
MARKET = []  # market regime rows for the report banner


def build_universe(index_keys):
    membership = {}
    for k in index_keys:
        try:
            syms = FETCHERS[k]()
            print(f"  {INDEX_NAMES[k]:<20} {len(syms):>5} tickers")
            DIAG.append(f"{INDEX_NAMES[k]}: {len(syms)} tickers")
        except Exception as e:
            print(f"  {INDEX_NAMES[k]:<20} FAILED ({e}) - skipped")
            DIAG.append(f"{INDEX_NAMES[k]}: FAILED - {str(e)[:150]}")
            continue
        for s in syms:
            membership.setdefault(s, set()).add(INDEX_NAMES[k])   # label read after fetch
    return membership

# --------------------------------------------------------------------------
# Price data
# --------------------------------------------------------------------------
def download(tickers, period="2y", chunk=150):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(batch, period=period, interval="1d", group_by="ticker",
                                 auto_adjust=True, threads=True, progress=False)
                break
            except Exception:
                time.sleep(5 * (attempt + 1))
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            have = set(df.columns.get_level_values(0))
            for t in batch:
                if t in have:
                    d = df[t].dropna(how="all")
                    if len(d):
                        out[t] = d
        else:
            out[batch[0]] = df.dropna(how="all")
        print(f"\r  downloaded {min(i + chunk, len(tickers))}/{len(tickers)}", end="", flush=True)
    print()
    return out


VOL_CURVE = [(0, 0.0), (30, 0.17), (60, 0.26), (90, 0.33), (150, 0.44), (210, 0.53),
             (270, 0.63), (330, 0.76), (360, 0.85), (390, 1.0)]   # typical share of daily volume by minute of session


def market_open_now():
    now = dt.datetime.now(ET)
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 0)


def session_fraction(now=None):
    now = now or dt.datetime.now(ET)
    m = min(390, max(1, now.hour * 60 + now.minute - 570))
    xs, ys = zip(*VOL_CURVE)
    return float(max(0.08, np.interp(m, xs, ys)))


LIVE = {"on": False}


def drop_live_bar(d):
    """Remove today's still-forming candle when run during market hours."""
    now = dt.datetime.now(ET)
    if len(d) and pd.Timestamp(d.index[-1]).date() == now.date() \
            and now.weekday() < 5 and (now.hour, now.minute) < (16, 15):
        return d.iloc[:-1]
    return d


def prep(d):
    d = d[["Open", "High", "Low", "Close", "Volume"]].astype(float).dropna().copy()
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    d["ATR14"] = tr.rolling(14).mean()
    d["SMA20"] = d["Close"].rolling(20).mean()
    d["SMA50"] = d["Close"].rolling(50).mean()
    d["SMA200"] = d["Close"].rolling(200).mean()
    d["AvgVol50"] = d["Volume"].rolling(50).mean().shift(1)   # excludes the current bar
    d["SMA150"] = d["Close"].rolling(150).mean()
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    d["VWAP20"] = (tp * d["Volume"]).rolling(20).sum() / d["Volume"].rolling(20).sum()
    delta = d["Close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    macd = d["Close"].ewm(span=12, adjust=False).mean() - d["Close"].ewm(span=26, adjust=False).mean()
    d["MACDH"] = macd - macd.ewm(span=9, adjust=False).mean()

    # ---- Added from the books (see BOOK_RULES.md) ----------------------------------
    C, H, L, V = d["Close"], d["High"], d["Low"], d["Volume"]
    # Weekly-chart filter computed on daily bars so the backtest never peeks inside an
    # unfinished week: 10-week ~ 50-day, 30-week ~ 150-day, weekly MACD(12,26,9) ~ daily (60,130,45).
    # [Murphy ch8-10: weekly signals filter daily ones; Douglas ch11: trade with the higher time frame]
    wmacd = C.ewm(span=60, adjust=False).mean() - C.ewm(span=130, adjust=False).mean()
    d["WMACDH"] = wmacd - wmacd.ewm(span=45, adjust=False).mean()
    # On-balance volume [Murphy ch7]
    d["OBV"] = (np.sign(C.diff()).fillna(0) * V).cumsum()
    # Wilder ADX / DMI, 14 [Murphy ch15]
    up, dn = H.diff(), -L.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    atr_w = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["PDI"] = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    d["MDI"] = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    dx = 100 * (d["PDI"] - d["MDI"]).abs() / (d["PDI"] + d["MDI"]).replace(0, np.nan)
    d["ADX"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    # Bollinger band width (squeeze) [Murphy ch9]
    d["BBW"] = 4 * C.rolling(20).std() / d["SMA20"]
    # STARC+ band: 6-day MA + 2 x ATR(15) [Murphy App. A]
    d["STARCU"] = C.rolling(6).mean() + 2 * tr.rolling(15).mean()
    # Ichimoku Kinko Hyo; the cloud at bar t was computed 26 bars earlier (no look-ahead) [Allemann ch12]
    tenkan = (H.rolling(9).max() + L.rolling(9).min()) / 2
    kijun = (H.rolling(26).max() + L.rolling(26).min()) / 2
    d["TENKAN"], d["KIJUN"] = tenkan, kijun
    d["SENKOUA"] = ((tenkan + kijun) / 2).shift(26)
    d["SENKOUB"] = ((H.rolling(52).max() + L.rolling(52).min()) / 2).shift(26)
    d["SENKOUA_FWD"] = (tenkan + kijun) / 2                       # cloud being built 26 bars ahead
    d["SENKOUB_FWD"] = (H.rolling(52).max() + L.rolling(52).min()) / 2
    return d

# --------------------------------------------------------------------------
# Pattern helpers
# --------------------------------------------------------------------------
def _slope(y):
    return np.polyfit(np.arange(len(y)), y, 1)[0] if len(y) >= 2 else 0.0


def _swing_lows(L, start, end, k=5):
    out = []
    for i in range(max(start, k), min(end, len(L) - k)):
        if L[i] == L[i - k:i + k + 1].min():
            out.append(i)
    return out


def detect_double_bottom(H, L, w0, b, R):
    lows = _swing_lows(L, w0, b - 5)       # b-5 so the 5-bar window never looks past the breakout
    if len(lows) < 2:
        return False
    i1, i2 = sorted(sorted(lows, key=lambda i: L[i])[:2])
    # [Murphy ch5] valid double tops/bottoms "should have at least a month between the two troughs"
    if i2 - i1 < 20 or abs(L[i1] - L[i2]) / min(L[i1], L[i2]) > 0.04:
        return False
    neckline = H[i1:i2 + 1].max()
    return R >= neckline * 0.97


def detect_rounded_bottom(C, start, b):
    y = C[start:b]
    if len(y) < 40:
        return False
    x = np.linspace(0, 1, len(y))
    a, b1, c0 = np.polyfit(x, y, 2)
    if a <= 0:
        return False
    xv = -b1 / (2 * a)
    fit = a * x ** 2 + b1 * x + c0
    ss_res = ((y - fit) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return 0.25 <= xv <= 0.75 and r2 >= 0.6


def detect_bull_flag(H, L, s, Lb, R, base_low, V=None, avgv=None):
    """Bull flag / pennant [Murphy ch6]: an almost straight-line pole on heavy volume, a 1-3 week pause
    (4-15 bars) that is flat or slopes against the trend, volume drying up noticeably in the flag.
    Returns (is_flag, note) - note explains a volume shortfall."""
    if Lb > 15 or Lb < 4:
        return False, ""
    ps = max(0, s - 20)
    if s - ps < 3:
        return False, ""
    i_low = ps + int(np.argmin(L[ps:s]))
    pole_top = max(H[i_low:s + 1].max(), R)
    pole = pole_top - L[i_low]
    if pole <= 0:
        return False, ""
    gain = pole / L[i_low]
    retrace = (pole_top - base_low) / pole
    if not (gain >= 0.15 and retrace <= 0.5 and _slope(H[s:s + Lb]) <= 0.002 * R):
        return False, ""
    note = ""
    if V is not None and avgv:
        pole_v = V[i_low:s].mean() if s > i_low else np.nan
        flag_v = V[s:s + Lb].mean()
        if np.isfinite(pole_v) and pole_v < avgv:
            note = "Flag pole was not on heavy volume"
        elif np.isfinite(pole_v) and flag_v > 0.8 * pole_v:
            note = "Volume did not dry up in the flag"
    return True, note


def resistance_tests(H, b, R, lookback=120, k=3, tol=0.02):
    w0 = max(k, b - lookback)
    n, last = 0, -99
    for i in range(w0, b - k):
        if H[i] == H[i - k:i + k + 1].max() and abs(H[i] - R) / R <= tol and i - last > k:
            n += 1
            last = i
    return n

# --------------------------------------------------------------------------
# Core breakout check
# --------------------------------------------------------------------------
def arrays(d):
    """Column arrays for fast indexing (computed once per ticker)."""
    return {c: d[c].to_numpy(dtype=float) for c in d.columns}


def base_limit(Lb):
    if Lb <= 10:
        return CFG["tight_short"]
    return CFG["tight_short"] + (CFG["tight_long"] - CFG["tight_short"]) * (Lb - 10) / (CFG["max_base"] - 10)


def _swings(X, lo, hi, k, kind):
    out = []
    for i in range(max(lo, k), min(hi, len(X) - k)):
        w = X[i - k:i + k + 1]
        if (kind == "high" and X[i] == w.max()) or (kind == "low" and X[i] == w.min()):
            out.append(i)
    return out


def _avwap(A, i0, b):
    tp = (A["High"][i0:b + 1] + A["Low"][i0:b + 1] + A["Close"][i0:b + 1]) / 3
    v = A["Volume"][i0:b + 1]
    return float((tp * v).sum() / v.sum()) if v.sum() > 0 else np.nan


def next_resistance(A, s, b, c, lookback=500, k=5):
    """Nearest prior swing high above today's close (overhead resistance)."""
    H = A["High"]
    highs = [H[i] for i in _swings(H, b - lookback, s - k, k, "high") if H[i] > c * 1.005]
    return min(highs) if highs else np.nan


def supply_zone(A, b, c, target, lookback=250, bins=40):
    """Heavy-volume price area between the entry and the target (volume profile)."""
    w0 = max(0, b - lookback)
    H, L, C, V = A["High"][w0:b], A["Low"][w0:b], A["Close"][w0:b], A["Volume"][w0:b]
    if len(V) < 50 or target <= c * 1.01 or H.max() <= c * 1.01:
        return np.nan
    hist, edges = np.histogram((H + L + C) / 3, bins=bins, range=(L.min(), H.max()), weights=V)
    avg = hist.mean()
    for i in range(bins):
        mid = (edges[i] + edges[i + 1]) / 2
        if c * 1.01 < mid <= target and hist[i] > 1.5 * avg:
            return float(mid)
    return np.nan


def round_step(p):
    """Psychological round-number spacing by price level [Murphy ch4: 10, 20, 25, 50, 75, 100, 1000s]."""
    return 1.0 if p < 10 else 5.0 if p < 50 else 25.0 if p < 200 else 50.0 if p < 1000 else 100.0


def round_above(p):
    st = round_step(p)
    return float(np.floor(p / st) * st + st)


def below_round(stop):
    """[Murphy ch4] avoid stops right at round numbers: if a round level sits just under the stop
    (within 1%), place the stop 0.5% below that level instead."""
    st = round_step(stop)
    lvl = np.floor(stop / st) * st
    if lvl > 0 and stop - lvl <= stop * 0.01:
        return round(lvl * 0.995, 2)
    return stop


def trade_plan(R, low, atr, entry, next_res):
    stop = below_round(min(max(low, R - TRADE["stop_atr"] * atr), R * 0.985))
    slip = CFG["slippage"]
    e = entry * (1 + slip)
    min_target = (e + CFG["min_rr"] * max(e - stop, e * 0.005)) / (1 - slip)   # 2R after costs
    target = max(R + (R - low), min_target)                          # measured move, at least 2R
    capped = bool(np.isfinite(next_res) and next_res < target)
    if capped:
        target = next_res                                            # sell into the next resistance
    t = target * (1 - slip)
    rr = (t - e) / (e - stop) if e > stop else 0.0
    return round(stop, 2), round(target + 0.005, 2) if not capped else round(target, 2), round(rr, 2), capped


def check_breakout(d, b, off, A=None, min_dv=None):
    """Hard rules from the original brief. Returns None if any one fails.
    b = breakout bar, off = sessions since (0 fresh, 1 the normal hold day, 2-3 caught late)."""
    A = A or arrays(d)
    O, H, L, C, V = A["Open"], A["High"], A["Low"], A["Close"], A["Volume"]
    atr, avgv, sma50, sma200 = A["ATR14"][b - 1], A["AvgVol50"][b], A["SMA50"][b], A["SMA200"][b]
    if not np.isfinite([atr, avgv, sma50, sma200]).all() or avgv <= 0 or atr <= 0:
        return None
    c = C[b]
    if c < CFG["min_price"] or avgv * c < (min_dv or CFG["min_dollar_vol"]):
        return None

    # --- breakout candle: volume spike, wide range, strong close, small upper wick
    vol_ratio = V[b] / avgv
    rng = H[b] - L[b]
    if vol_ratio < CFG["min_vol_ratio"] or rng <= 0:
        return None
    wick = (H[b] - max(O[b], C[b])) / rng
    close_pos = (C[b] - L[b]) / rng
    range_atr = rng / atr
    if wick > CFG["max_wick_frac"] or close_pos < CFG["min_close_pos"] or range_atr < CFG["min_range_atr"]:
        return None

    # --- structure: longest tight base ending the candle before the breakout
    base = None
    for Lb in range(CFG["max_base"], CFG["min_base"] - 1, -1):
        s = b - Lb
        if s < 1:
            continue
        hi, lo = H[s:b], L[s:b]
        R, low = hi.max(), lo.min()
        depth = (R - low) / R
        if depth > base_limit(Lb):
            continue
        touches = int((hi >= R * (1 - CFG["touch_tol"])).sum())
        if touches < CFG["min_touches"]:
            continue
        if C[b - 1] < low + 0.5 * (R - low):        # pressure: coiling near the top
            continue
        base = (s, Lb, R, low, depth, touches)
        break
    if base is None:
        return None                                  # no consolidation = random spike
    s, Lb, R, low, depth, touches = base

    clear_atr = (c - R) / atr
    if c <= R * (1 + CFG["breakout_buffer"]) or clear_atr < CFG["min_clear_atr"]:
        return None                                  # must CLOSE decisively above resistance
    ext = (c - R) / R
    if ext > CFG["max_ext_above_res"] or c > sma50 * (1 + CFG["max_ext_sma50"]):
        return None                                  # late entry

    # --- no immediate rejection
    follow_vol = np.nan
    if off >= 1:
        for n in range(b + 1, b + off + 1):          # must hold the level on every session since
            if C[n] < R or L[n] < R * (1 - CFG["hold_tol"]):
                return None
        n = b + 1
        if C[n] < C[b] and V[n] > V[b]:
            return None                              # [Murphy ch5] bull trap: selling on heavier volume
        status = "Confirmed (held next day)" if off == 1 else f"Caught late: broke out {off} sessions ago and held"
        follow_vol = V[n] / avgv
    else:
        status = "Fresh - confirm hold next session"

    # --- pressure build-up inside the base
    lo, hi, vb = L[s:b], H[s:b], V[s:b]
    third = max(1, Lb // 3)
    higher_lows = Lb >= 5 and _slope(lo) > 0 and lo[-third:].min() > lo[:third].min()
    vol_rising = Lb >= 4 and _slope(vb / avgv) > 0
    atr_start = A["ATR14"][s - 1]
    contraction = bool(np.isfinite(atr_start) and atr_start > 0 and (hi - lo).mean() / atr_start < 0.8)
    tests = resistance_tests(H, b, R)
    avgv_s = A["AvgVol50"][s]
    dryup = float(vb.mean() / avgv_s) if np.isfinite(avgv_s) and avgv_s > 0 else np.nan
    chg = np.diff(C[s - 1:b])
    up_v, dn_v = vb[chg > 0].sum(), vb[chg < 0].sum()
    updown = float(up_v / dn_v) if dn_v > 0 else 3.0

    # --- context & pattern
    sma50_prev = A["SMA50"][b - 10]
    uptrend = c > sma50 > sma200 and sma50 > sma50_prev
    w0 = max(0, b - 220)
    ih = w0 + int(np.argmax(H[w0:b]))
    il = ih + int(np.argmin(L[ih:b])) if ih < b else ih
    drawdown = (H[ih] - L[il]) / H[ih]
    sma200_il, sma50_il = A["SMA200"][il], A["SMA50"][il]
    bottoming = (drawdown >= CFG["bottom_drawdown"] and b - il >= 15 and
                 ((np.isfinite(sma200_il) and L[il] < sma200_il) or
                  (np.isfinite(sma50_il) and np.isfinite(sma200_il) and sma50_il < sma200_il)))

    flags = []
    if bottoming:
        category = "Bottoming"
        if detect_double_bottom(H, L, w0, b, R):
            pattern = "Double bottom -> breakout"
        elif detect_rounded_bottom(C, ih, b):
            pattern = "Rounded bottom -> breakout"
        elif higher_lows and touches >= 3 and Lb >= 8:
            pattern = "Ascending triangle off a low"
        else:
            pattern = "Flat base after drop -> breakout"
    elif uptrend:
        category = "Continuation"
        is_flag, flag_note = detect_bull_flag(H, L, s, Lb, R, low, V, avgv)
        if is_flag:
            pattern = "Bull flag"
            if flag_note:
                flags.append(flag_note)
        elif higher_lows and touches >= 3 and Lb >= 8:
            pattern = "Ascending triangle"
        else:
            pattern = "Flat-top breakout"
    else:
        category = "Other"
        pattern = "Ascending triangle" if (higher_lows and touches >= 3 and Lb >= 8) else "Flat-top breakout"
        flags.append("No clear trend or bottom behind it")

    r0 = max(0, s - 60)
    runup = (low - L[r0:s].min()) / L[r0:s].min() if s > r0 else 0
    late_runup = runup > 0.5 and pattern != "Bull flag"
    if late_runup:
        flags.append("Big run-up before the base (late)")
    # [Murphy ch4] a breakaway gap out of a base on heavy volume is bullish and usually not filled;
    # [Hougaard] gap-up days favour continuation. Only a gap on weak volume is treated as a warning.
    gap = O[b] > H[b - 1] * 1.02
    gap_low = H[b - 1] if gap else np.nan       # a close back below this fills the gap = weakness
    if gap and vol_ratio < CFG["strong_vol_ratio"]:
        flags.append("Gap-up on under 2x volume (not a clean breakaway gap)")

    # --- trend structure, anchored VWAPs, momentum
    sw_lo = _swings(L, b - 60, b - 2, 3, "low")
    hl_struct = len(sw_lo) >= 2 and L[sw_lo[-1]] > L[sw_lo[-2]]
    structure = bool(hl_struct and A["SMA20"][b] > A["SMA50"][b])
    i52 = max(0, b - 252)
    i_low52 = i52 + int(np.argmin(L[i52:b]))
    avwap_base = _avwap(A, s, b)
    avwap_low = _avwap(A, i_low52, b)
    ext_vwap = (c - A["VWAP20"][b]) / atr if np.isfinite(A["VWAP20"][b]) else np.nan
    at_52w_high = c >= H[i52:b].max()

    # --- signals added from the books (BOOK_RULES.md) ---------------------------------
    # [Murphy ch9] Donchian 4-week rule: a real breakout is a new 20-day closing high
    new20 = bool(c >= C[max(0, b - 20):b].max())
    # [Murphy ch7] OBV should confirm: at a 50-day high = confirmation; well below it = divergence
    obv = A["OBV"]
    o_hi, o_lo = obv[b - 50:b].max(), obv[b - 50:b].min()
    obv_confirm = bool(obv[b] >= o_hi)
    obv_div = bool(not obv_confirm and o_hi > o_lo and (o_hi - obv[b]) > 0.15 * (o_hi - o_lo))
    # [Murphy ch10] bearish RSI divergence: price above a prior RSI>70 peak while RSI is lower
    rsi = A["RSI14"]
    j0 = max(0, b - 60)
    seg = rsi[j0:b - 4]
    rsi_div = False
    if len(seg) and np.isfinite(seg).any() and np.nanmax(seg) > 70:
        j = j0 + int(np.nanargmax(seg))
        rsi_div = bool(c > C[j] and rsi[b] < rsi[j] - 5)
    # [Murphy ch15] ADX turning up from below 20 = new trend; falling from above 40 = trend tiring
    adx_now, adx_prev = A["ADX"][b], A["ADX"][b - 5]
    adx_turn_up = bool(np.isfinite(adx_prev) and adx_prev < 20 and adx_now > adx_prev)
    adx_tiring = bool(np.isfinite(adx_prev) and adx_prev > 40 and adx_now < adx_prev)
    di_bull = bool(A["PDI"][b] > A["MDI"][b])
    # [Murphy ch9] Bollinger squeeze: band width at the end of the base in its lowest 20% of 6 months
    bbw = A["BBW"]
    wbw = bbw[max(0, b - 126):b]
    wbw = wbw[np.isfinite(wbw)]
    squeeze = bool(np.isfinite(bbw[b - 1]) and len(wbw) >= 60 and bbw[b - 1] <= np.percentile(wbw, 20))
    # [Murphy ch8-10, Douglas ch11, Gann rule 16 via Allemann] weekly trend must agree
    weekly_up = bool(c > A["SMA50"][b] > A["SMA150"][b])
    weekly_macd_up = bool(A["WMACDH"][b] > 0 or A["WMACDH"][b] > A["WMACDH"][b - 5])
    weekly_ok = bool(weekly_up or (category == "Bottoming" and weekly_macd_up))
    # [Allemann ch12] Ichimoku: above the cloud, Tenkan over Kijun, bullish cloud being built ahead
    sa, sb = A["SENKOUA"][b], A["SENKOUB"][b]
    above_cloud = bool(np.isfinite(sa) and np.isfinite(sb) and c > max(sa, sb))
    tk_bull = bool(A["TENKAN"][b] > A["KIJUN"][b])
    cloud_bull = bool(A["SENKOUA_FWD"][b] > A["SENKOUB_FWD"][b])
    # [Murphy ch6] rising wedge (bearish) and broadening formation (bearish) inside the base
    rising_wedge = broadening = late_triangle = False
    if Lb >= 10:
        sh, sl = _slope(H[s:b]), _slope(L[s:b])
        rising_wedge = bool(sh > 0 and sl > 0 and sl > 1.2 * sh)
        broadening = bool(sh > 0 and sl < 0 and vol_rising)
        # [Murphy ch6] triangles should break out 2/3-3/4 of the way to the apex; later loses potency
        if "triangle" in pattern.lower() and sl > 0:
            lo_fit = np.polyfit(np.arange(Lb), L[s:b], 1)
            x_apex = (R - lo_fit[1]) / lo_fit[0] if lo_fit[0] > 0 else np.inf
            late_triangle = bool(np.isfinite(x_apex) and x_apex > 0 and Lb / x_apex > 0.85)
    for cond, txt in [(rising_wedge, "Rising wedge base (bearish shape)"),
                      (broadening, "Broadening base with rising volume (unstable)"),
                      (late_triangle, "Late in the triangle (past 3/4 of the way to the apex)")]:
        if cond:
            flags.append(txt)

    # --- overhead resistance, trade plan (from the latest close: breakout day, or the hold day)
    entry = C[b + off]
    nres = next_resistance(A, s, b, entry)
    stop, target, rr, capped = trade_plan(R, low, atr, entry, nres)
    supply = supply_zone(A, b, entry, target)
    room = (not np.isfinite(nres)) or nres >= entry + CFG["min_rr"] * (entry - stop)
    # [Murphy App. A] above the STARC+ band = high-risk time to buy (chasing)
    above_starc = bool(np.isfinite(A["STARCU"][b + off]) and entry > A["STARCU"][b + off])
    # [Murphy ch4] round numbers act as resistance; flag one just overhead
    rnd = round_above(entry)
    round_overhead = bool(rnd <= entry * 1.03)

    return dict(
        Category=category, Pattern=pattern, Status=status, Held=off >= 1, LateDays=off if off >= 2 else 0,
        BreakoutDate=pd.Timestamp(d.index[b]).date().isoformat(),
        BaseStart=pd.Timestamp(d.index[s]).date().isoformat(),
        Close=round(entry, 2), Resistance=round(R, 2),
        PctAboveRes=round((entry - R) / R * 100, 2),
        VolX=round(vol_ratio, 2), FollowVolX=round(follow_vol, 2) if np.isfinite(follow_vol) else np.nan,
        RangeATR=round(range_atr, 2), ClearATR=round(clear_atr, 2), ClosePos=round(close_pos, 2),
        UpperWickPct=round(wick * 100, 1), BaseLen=Lb, BaseDepthPct=round(depth * 100, 1),
        BaseTightness=round(1 - depth / base_limit(Lb), 2),
        Touches=touches, ResTests120d=tests, HigherLows=bool(higher_lows),
        VolRisingInBase=bool(vol_rising), RangeContraction=contraction,
        VolDryUp=round(dryup, 2) if np.isfinite(dryup) else np.nan, UpDownVol=round(updown, 2),
        Structure=structure, AboveAVWAPBase=bool(c > avwap_base), AboveAVWAP52wLow=bool(c > avwap_low),
        ExtVWAP20ATR=round(ext_vwap, 2) if np.isfinite(ext_vwap) else np.nan,
        RSI=round(A["RSI14"][b], 1), MACDH=round(A["MACDH"][b], 3),
        At52wHigh=bool(at_52w_high), NextRes=round(nres, 2) if np.isfinite(nres) else np.nan,
        SupplyZone=round(supply, 2) if np.isfinite(supply) else np.nan, Room=bool(room),
        Stop=stop, Target=target, RR=rr, TargetCapped=capped,
        BaseLow=round(low, 2), ATR=round(atr, 2), ATRPct=round(atr / entry * 100, 2),
        DollarVolM=round(avgv * entry / 1e6, 1), LateRunup=bool(late_runup), Gap=bool(gap),
        GapLow=round(gap_low, 2) if np.isfinite(gap_low) else np.nan,
        New20dHigh=new20, OBVConfirm=obv_confirm, OBVDiv=obv_div, RSIDiv=rsi_div,
        ADX=round(adx_now, 1) if np.isfinite(adx_now) else np.nan, ADXTurnUp=adx_turn_up, ADXTiring=adx_tiring,
        DIBull=di_bull, Squeeze=squeeze, WeeklyUp=weekly_up, WeeklyOK=weekly_ok,
        AboveCloud=above_cloud, TKBull=tk_bull, CloudBull=cloud_bull,
        RisingWedge=rising_wedge, Broadening=broadening, LateTriangle=late_triangle,
        AboveStarc=above_starc, RoundOverhead=round_overhead, RoundLevel=rnd,
        Clear1Pct=bool((c - R) / R >= 0.01),
        Flags="; ".join(flags), _b=b, _s=s,
    )


def analyze(d, A=None, min_dv=None):
    """Yesterday's breakout that held today, else today's fresh one, else a missed one from 2-3 sessions ago
    that has held every day since (so a skipped scan or a busy evening doesn't lose a breakout)."""
    if len(d) < 215:
        return None
    A = A or arrays(d)
    n = len(d) - 1
    r = check_breakout(d, n - 1, 1, A, min_dv) or check_breakout(d, n, 0, A, min_dv)
    for off in range(2, CFG["catchup_days"] + 1):
        if r:
            break
        r = check_breakout(d, n - off, off, A, min_dv)
    return r


RADAR_PREV = {}   # ticker -> (first seen, trigger) from earlier scans, to tell a breakout it was flagged in advance


def radar_merge(radar, path, save=True):
    """Keep the first date each name appeared on the radar, rank (Canada first, then readiness) and cap per market.
    radar.csv also remembers names from the last 10 days so a breakout can say how early it was flagged."""
    today = max([pd.Timestamp(f["Date"]).date() for f in radar] or [dt.datetime.now(ET).date()])   # session date
    old = {}
    if path and os.path.exists(path):
        try:
            for x in pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records"):
                old[x["Ticker"]] = x
        except Exception:
            old = {}
    RADAR_PREV.clear()
    for t, x in old.items():
        try:
            if (today - pd.Timestamp(x["LastSeen"]).date()).days <= 10:
                RADAR_PREV[t] = (x["FirstSeen"], _num(x.get("Trigger")))
        except Exception:
            pass
    for f in radar:
        prev = old.get(f["Ticker"])
        recent = prev and (today - pd.Timestamp(prev["LastSeen"]).date()).days <= 7
        f["FirstSeen"] = prev["FirstSeen"] if recent else today.isoformat()
        f["LastSeen"] = today.isoformat()
    radar.sort(key=lambda f: (f["Region"] != "CA", -f["Readiness"], f["DistPct"]))
    kept, n = [], {"CA": 0, "US": 0}
    for f in radar:
        if n[f["Region"]] < CFG["radar_max"]:
            kept.append(f)
            n[f["Region"]] += 1
    if save and path:
        rows = {f["Ticker"]: dict(Ticker=f["Ticker"], FirstSeen=f["FirstSeen"], LastSeen=f["LastSeen"],
                                  Trigger=f["Trigger"]) for f in radar}
        for t, x in old.items():               # remember recent names that dropped off (broke out, or faded)
            if t not in rows and t in RADAR_PREV:
                rows[t] = x
        pd.DataFrame(list(rows.values()), columns=["Ticker", "FirstSeen", "LastSeen", "Trigger"]).to_csv(path, index=False)
    return kept


def check_forming(d, A, min_dv=None):
    """'On the radar': a tight base whose close sits just under a flat top that hasn't broken yet.
    Gives the trigger price days ahead, so a breakout like Loblaw's (16 Oct 2026: it climbed inside its
    base for days, then broke out) is on the list before the breakout candle, not only after it."""
    if len(d) < 215:
        return None
    O, H, L, C, V = A["Open"], A["High"], A["Low"], A["Close"], A["Volume"]
    n = len(d) - 1
    atr, avgv, sma50, sma200 = A["ATR14"][n], A["AvgVol50"][n], A["SMA50"][n], A["SMA200"][n]
    if not np.isfinite([atr, avgv, sma50, sma200]).all() or avgv <= 0 or atr <= 0:
        return None
    c = C[n]
    if c < CFG["min_price"] or avgv * c < (min_dv or CFG["min_dollar_vol"]) or c < sma50:
        return None
    if atr / c > CFG["max_atr_pct"]:
        return None
    for Lb in range(CFG["max_base"], 4, -1):       # longest qualifying base ending yesterday (same as a breakout)
        s = n - Lb
        if s < 1:
            continue
        hi, lo = H[s:n], L[s:n + 1]
        R, low = hi.max(), lo.min()
        depth = (R - low) / R
        if depth > base_limit(Lb):
            continue
        touches = int((hi >= R * (1 - CFG["touch_tol"])).sum())
        if touches < CFG["min_touches"] or c < low + 0.66 * (R - low):
            continue
        trigger = max(R * (1 + CFG["breakout_buffer"]), R + CFG["min_clear_atr"] * atr)
        dist = trigger / c - 1
        if dist <= 0 or dist > CFG["radar_dist"]:
            return None                              # already through (the breakout scan handles it) or too far
        third = max(1, Lb // 3)
        higher_lows = _slope(lo) > 0 and lo[-third:].min() > lo[:third].min()
        last5 = (H[n - 4:n + 1] - L[n - 4:n + 1]).mean()
        tightening = bool(last5 < 0.8 * (hi - L[s:n]).mean())
        quiet = float(V[n - 4:n + 1].mean() / avgv)
        uptrend = bool(sma50 > sma200 and sma50 > A["SMA50"][n - 10])
        up5 = bool(c > C[n - 5])
        ready = (3 * (1 - dist / CFG["radar_dist"]) + 2 * higher_lows + 2 * tightening
                 + (1 if quiet < 1.0 else 0) + 2 * uptrend)
        return dict(Date=pd.Timestamp(d.index[n]).date().isoformat(), Trigger=round(trigger, 2), Resistance=round(R, 2), Close=round(c, 2), DistPct=round(dist * 100, 1),
                    BaseLen=Lb, BaseDepthPct=round(depth * 100, 1), Touches=touches, HigherLows=bool(higher_lows),
                    Tightening=tightening, QuietVol=round(quiet, 2), Uptrend=uptrend, Rising5d=up5,
                    VolNeeded=int(np.ceil(CFG["min_vol_ratio"] * avgv)), Readiness=round(ready, 1))
    return None

# --------------------------------------------------------------------------
# Market, sector, relative strength, earnings - and the 100-point scorecard
# --------------------------------------------------------------------------
BENCH = {"US": "SPY", "CA": "XIU.TO"}
SECTOR_ETF = {
    "US": {"Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV", "Energy": "XLE",
           "Industrials": "XLI", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Utilities": "XLU",
           "Real Estate": "XLRE", "Basic Materials": "XLB", "Communication Services": "XLC"},
    "CA": {"Energy": "XEG.TO", "Financial Services": "XFN.TO", "Basic Materials": "XMA.TO",
           "Technology": "XIT.TO", "Real Estate": "XRE.TO", "Utilities": "XUT.TO", "Consumer Defensive": "XST.TO"},
}
ALL_SECTOR_ETFS = sorted({v for m in SECTOR_ETF.values() for v in m.values()})


def region(t):
    return "CA" if str(t).endswith(".TO") else "US"


def breadth_series(data, tickers):
    """Share of stocks above their 50-day average, per date."""
    cols = {}
    for t in tickers:
        d = data.get(t)
        if d is None or len(d) < 60:
            continue
        cols[t] = (d["Close"] > d["SMA50"]).astype(float).where(d["SMA50"].notna())
    if not cols:
        return pd.Series(dtype=float)
    br = pd.DataFrame(cols).mean(axis=1)
    cnt = pd.DataFrame(cols).notna().sum(axis=1)
    return br[cnt >= max(20, 0.5 * cnt.max())]


def internals_series(data, tickers):
    """Market internals from the scanned universe [Murphy ch18]: new 52-week highs minus new lows
    (10-day average; Elder: "probably the best leading indicator"), share of dollar volume in
    advancing stocks (10-day), and a breadth thrust (share above the 50-day back above 30% after
    being below it [Murphy ch10])."""
    hi, lo, upv, dnv = {}, {}, {}, {}
    for t in tickers:
        d = data.get(t)
        if d is None or len(d) < 260:
            continue
        h252 = d["High"].rolling(252).max().shift(1)
        l252 = d["Low"].rolling(252).min().shift(1)
        hi[t] = (d["High"] > h252).astype(float).where(h252.notna())
        lo[t] = (d["Low"] < l252).astype(float).where(l252.notna())
        chg = d["Close"].diff()
        dv = d["Close"] * d["Volume"]
        upv[t] = dv.where(chg > 0, 0.0)
        dnv[t] = dv.where(chg < 0, 0.0)
    if not hi:
        return pd.DataFrame()
    NH, NL = pd.DataFrame(hi).sum(axis=1), pd.DataFrame(lo).sum(axis=1)
    cnt = pd.DataFrame(hi).notna().sum(axis=1)
    U, D = pd.DataFrame(upv).sum(axis=1), pd.DataFrame(dnv).sum(axis=1)
    out = pd.DataFrame({"NH": NH, "NL": NL, "NHNL10": (NH - NL).rolling(10).mean(),
                        "UpVol10": U.rolling(10).sum() / (U + D).rolling(10).sum()})
    return out[cnt >= max(20, 0.5 * cnt.max())]


def stress_series(num, den):
    """[Øygard ch5] markets priced the Icelandic crisis (CDS spreads, bank shares halving) before the
    experts did. Stress = ratio below its 50-day average AND at/near a 3-month low."""
    if num is None or den is None:
        return pd.Series(dtype=bool)
    r = (num["Close"] / den["Close"].reindex(num.index).ffill()).dropna()
    return (r < r.rolling(50).mean()) & (r <= r.rolling(63).min() * 1.01)


def market_state(bench, breadth, date, internals=None, credit=None, banks=None):
    """Trend, breadth, internals, distribution days and credit stress for a benchmark as of a date."""
    out = dict(Trend="Unknown", Breadth=np.nan, DistDays=0, Points=5.0, Pressure=False,
               NHNL10=np.nan, UpVol10=np.nan, Thrust=False, Credit=False, Banks=False)
    if bench is None or len(bench) < 210:
        return out
    bd = bench.loc[:date]
    if len(bd) < 210:
        return out
    c, s50, s200 = bd["Close"].iloc[-1], bd["SMA50"].iloc[-1], bd["SMA200"].iloc[-1]
    trend = "Uptrend" if c > s50 > s200 else ("Downtrend" if c < s50 < s200 else "Mixed")
    last = bd.iloc[-26:]
    ret = last["Close"].pct_change()
    dist = int(((ret <= -0.002) & (last["Volume"] > last["Volume"].shift(1))).iloc[1:].sum())
    brs = breadth.loc[:date] if breadth is not None else pd.Series(dtype=float)
    br = brs.iloc[-1] if len(brs) else np.nan
    thrust = bool(len(brs) >= 15 and brs.iloc[-15:].min() < 0.30 and br > 0.30)

    def last_of(series):
        if series is None or not len(series):
            return np.nan
        x = series.loc[:date]
        return x.iloc[-1] if len(x) else np.nan

    nhnl = last_of(internals["NHNL10"]) if internals is not None and len(internals) else np.nan
    upv = last_of(internals["UpVol10"]) if internals is not None and len(internals) else np.nan
    cr = last_of(credit)
    bk = last_of(banks)
    credit_stress = bool(cr) if isinstance(cr, (bool, np.bool_)) else False
    bank_stress = bool(bk) if isinstance(bk, (bool, np.bool_)) else False

    pts = {"Uptrend": 4, "Mixed": 2.5 if c > s50 else 1, "Downtrend": 0}[trend]
    pts += (3 if br >= 0.6 else 2 if br >= 0.5 else 1 if br >= 0.4 else 0) if np.isfinite(br) else 1.5
    pts += (2 if nhnl > 0 else 0) if np.isfinite(nhnl) else 1
    pts += (1 if upv >= 0.55 else 0) if np.isfinite(upv) else 0.5
    pts += 2 if thrust else 0
    pts -= (2 if dist >= 6 else 0) + (2 if credit_stress else 0) + (1 if bank_stress else 0)
    pressure = (trend == "Downtrend" or dist >= 6 or (np.isfinite(br) and br < 0.4) or credit_stress
                or (np.isfinite(nhnl) and nhnl < 0 and trend != "Uptrend"))
    return dict(Trend=trend, Breadth=br, DistDays=dist, Points=float(np.clip(pts, 0, 10)), Pressure=bool(pressure),
                NHNL10=nhnl, UpVol10=upv, Thrust=thrust, Credit=credit_stress, Banks=bank_stress)


def _ret(x, b, n):
    return x[b] / x[b - n] - 1 if b - n >= 0 and np.isfinite(x[b - n]) and x[b - n] > 0 else np.nan


def enrich(r, d, A, ctx):
    """Add market, sector, relative strength, earnings, scorecard, warnings, checklist and sizing."""
    b = r["_b"]
    reg = region(r.get("Ticker", ""))
    date = d.index[b]
    bench = ctx.get("bench", {}).get(reg)
    C = A["Close"]

    # relative strength vs the benchmark
    rs5 = rs20 = rs63 = np.nan
    rs_high = leader = False
    rs_line_up = None
    if bench is not None:
        if "REL" not in A:
            bc = bench["Close"].reindex(d.index).ffill().to_numpy(dtype=float)
            A["BENCH"], A["REL"] = bc, C / bc
        bc, rel = A["BENCH"], A["REL"]
        rs5, rs20, rs63 = [(_ret(C, b, n) - _ret(bc, b, n)) * 100 for n in (5, 20, 63)]
        if b >= 50 and np.isfinite(rel[b - 50:b]).all():
            rs_high = bool(rel[b] >= rel[b - 50:b].max())
        if b >= 20:
            leader = bool(C[b] >= C[b - 20:b].max() and bc[b] < np.nanmax(bc[b - 20:b]) * 0.99)
        # [Murphy ch17] avoid stocks whose relative-strength line is still falling
        if b >= 50 and np.isfinite(rel[b - 50:b + 1]).all():
            rs_line_up = bool(rel[b] > rel[b - 50:b].mean())

    # sector strength
    sector, sec_rs = ctx.get("sector", {}).get(r.get("Ticker")), np.nan
    sec_d = ctx.get("sector_data", {}).get(SECTOR_ETF.get(reg, {}).get(sector)) if sector else None
    if sec_d is not None and bench is not None:
        sc = sec_d["Close"].reindex(d.index).ffill().to_numpy(dtype=float)
        sec_rs = (_ret(sc, b, 20) - _ret(A["BENCH"], b, 20)) * 100
    sec_ok = None if not np.isfinite(sec_rs) else bool(sec_rs > 0)

    mkt = market_state(bench, ctx.get("breadth", {}).get(reg), date, ctx.get("internals", {}).get(reg),
                       ctx.get("credit"), ctx.get("banks", {}).get(reg))

    # earnings
    earn = ctx.get("earnings", {}).get(r.get("Ticker"))
    earn_days = np.nan
    if earn is not None:
        earn_days = int(np.busday_count(pd.Timestamp(date).date(), pd.Timestamp(earn).date()))

    # ---------- 100-point scorecard (category weights from your checklist) ----------
    # Book-derived signals are folded into the existing categories; each category keeps its maximum.
    v = r["VolX"]
    p_vol = 0 if v < 1.5 else 10 + 10 * (v - 1.5) if v < 2 else min(20, 15 + 5 * (v - 2))
    p_vol = min(20, 0.9 * p_vol + 2 * r["OBVConfirm"])                  # [Murphy ch7] OBV confirms
    p_price = (4 * np.clip((r["ClosePos"] - 0.6) / 0.4, 0, 1)
               + 3 * (1 - r["UpperWickPct"] / 100 / CFG["max_wick_frac"])
               + 2 * np.clip((r["RangeATR"] - 1.1) / 0.9, 0, 1) + 1 * np.clip(r["ClearATR"] / 0.5, 0, 1)
               + 1 * r["Clear1Pct"]                                    # [Murphy ch4] 1% price filter
               + 1 * r["New20dHigh"]                                   # [Murphy ch9] 4-week rule
               + (2 if r["Held"] else 0)                               # [Murphy ch4] two-day rule
               + (1 if r["Gap"] and v >= CFG["strong_vol_ratio"] else 0))  # [Murphy ch4] breakaway gap
    p_price = min(15, p_price)
    dry = r["VolDryUp"]
    p_base = (1.5 * max(0, r["BaseTightness"]) + 1.5 * min(1, (r["Touches"] + r["ResTests120d"]) / 5)
              + 1.5 * r["RangeContraction"]
              + (1.5 if np.isfinite(dry) and dry < 0.85 else 0.75 if np.isfinite(dry) and dry < 1 else 0)
              + (1 if r["UpDownVol"] >= 1.2 else 0) + (1 if r["Category"] == "Bottoming" else 0)
              # [Murphy ch9] Bollinger squeeze is reported but not scored: tested, +0.21R vs +0.24R without
              + (1 if r["BaseLen"] >= 15 else 0))                       # [Murphy ch5, ch11] bigger base
    p_base = min(10, p_base)
    p_rs = ((4 * np.clip(rs20 / 10, 0, 1) if np.isfinite(rs20) else 2)
            + (3 * np.clip(rs63 / 15, 0, 1) if np.isfinite(rs63) else 1.5)
            + (3 if rs_high else 0) + (1.5 if sec_ok is None else 3 if sec_ok else 0)
            + (1 if rs_line_up is None else 2 if rs_line_up else 0))    # [Murphy ch17] rising RS line
    if leader:
        p_rs += 2
    p_rs = min(15, p_rs)
    p_mkt = mkt["Points"]
    ev = r["ExtVWAP20ATR"]
    p_vwap = (2 * r["AboveAVWAPBase"] + 1 * r["AboveAVWAP52wLow"]
              + (1 if np.isfinite(ev) and ev <= 2.5 else 0) + 2 * r["Structure"]
              + 2 * r["WeeklyOK"]                                       # [Murphy ch8-10; Douglas ch11]
              # [Murphy ch15] +DI over -DI and ADX not rolling over from 40+. The "ADX turning up from
              # below 20" bonus was tested and removed: in the 2022-26 backtest those breakouts averaged
              # +0.06R vs +0.29R (trendless stocks broke out poorly).
              + (1 if r["DIBull"] and not r["ADXTiring"] else 0)
              + (1 if r["AboveCloud"] and r["TKBull"] else 0))          # [Allemann ch12] Ichimoku
    p_vwap = min(10, p_vwap)
    rr = r["RR"]
    p_rr = 10 if rr >= 3 else 7 + 3 * (rr - 2) if rr >= 2 else 3 if rr >= 1.5 else 0
    p_rr += 5 if r["Room"] else 0
    p_rr -= (3 if np.isfinite(r["SupplyZone"]) else 0) + (1 if r["RoundOverhead"] else 0)  # [Murphy ch4]
    p_rr = max(0, p_rr)
    dv = r["DollarVolM"]
    p_liq = 5 if dv >= 50 else 4 if dv >= 20 else 3 if dv >= 10 else 2
    p_liq -= (1 if r["ATRPct"] / 100 > CFG["max_atr_pct"] else 0) + (1 if r["Close"] < 10 else 0)
    p_liq = max(0, p_liq)
    parts = dict(Volume=(p_vol, 20), Price=(p_price, 15), Base=(p_base, 10), RS=(p_rs, 15),
                 Market=(p_mkt, 10), VWAP=(p_vwap, 10), Reward=(p_rr, 15), Liquidity=(p_liq, 5))
    score = sum(x for x, _ in parts.values())

    # ---------- false-breakout warning signs ----------
    warn = []
    if v < CFG["strong_vol_ratio"]:
        warn.append(f"Volume only {v}x (under 2x)")
    if r["Category"] == "Bottoming" and v < CFG["strong_vol_ratio"]:
        warn.append("Bottom breakout without a big volume surge (essential at bottoms)")   # [Murphy ch5]
    if r["UpperWickPct"] > 20:
        warn.append(f"Upper wick {r['UpperWickPct']}% of the candle")
    if r["ClosePos"] < 0.7:
        warn.append("Closed off the high of the day")
    if not r["New20dHigh"]:
        warn.append("Not a new 20-day closing high")                                      # [Murphy ch9]
    if r["OBVDiv"]:
        warn.append("On-balance volume not confirming the new high")                      # [Murphy ch7]
    if r["RSIDiv"]:
        warn.append("Bearish RSI divergence (price higher, RSI lower than its last peak)") # [Murphy ch10]
    if not r["WeeklyOK"]:
        warn.append("Weekly trend not up")                                                # [Murphy ch10]
    if r["ADXTiring"]:
        warn.append(f"ADX falling from above 40 (trend tiring)")                          # [Murphy ch15]
    if np.isfinite(rs20) and rs20 < 0:
        warn.append(f"Lagging the market over 20 days ({rs20:+.1f} pts)")
    if rs_line_up is False:
        warn.append("Relative-strength line below its 50-day average")                    # [Murphy ch17]
    if sec_ok is False:
        warn.append(f"Sector ({sector}) lagging the market")
    if r["PctAboveRes"] > TRADE["extended"] * 100 or (np.isfinite(ev) and ev > 2.5):
        warn.append("Extended above the breakout level / 20-day VWAP")
    if r["AboveStarc"]:
        warn.append("Above the STARC+ band: chasing, high-risk entry")                    # [Murphy App. A]
    if not r["Room"] or rr < CFG["min_rr"]:
        warn.append(f"Resistance at {r['NextRes']} limits upside (R:R {rr})")
    if np.isfinite(r["SupplyZone"]):
        warn.append(f"Heavy-volume supply zone near {r['SupplyZone']}")
    if r["RoundOverhead"]:
        warn.append(f"Round-number resistance just above at {r['RoundLevel']:g}")         # [Murphy ch4]
    if np.isfinite(earn_days) and 0 <= earn_days <= CFG["earnings_block_days"]:
        warn.append(f"Earnings in {earn_days} sessions")
    if mkt["Pressure"]:
        extra = ", credit stress" if mkt["Credit"] else ""
        warn.append(f"Market under pressure ({mkt['Trend']}, {mkt['DistDays']} distribution days{extra})")
    if r["ATRPct"] / 100 > CFG["max_atr_pct"]:
        warn.append(f"Erratic: daily range {r['ATRPct']}% of price")
    if dv < 10:
        warn.append(f"Thin liquidity (${dv}M/day)")
    if r["LateRunup"]:
        warn.append("Big run-up before the base (late)")
    if r["Category"] == "Other":
        warn.append("No clear trend or bottom behind it")
    for f in str(r.get("Flags") or "").split("; "):                 # pattern-shape warnings [Murphy ch6]
        if f and f not in warn and not f.startswith("No clear trend") and not f.startswith("Big run-up"):
            warn.append(f)
    # seasonality [Krauth ch35]
    month = pd.Timestamp(date).month
    industry = ctx.get("industry", {}).get(r.get("Ticker")) or ""
    precious = any(k in industry for k in ("Gold", "Silver", "Precious"))
    if precious and month in (6, 7, 8):
        warn.append("Seasonally weak period for precious metals (Jun-Aug)")
    if month == 12 and pd.Timestamp(date).day >= 1 and dv < 50:
        ytd_i = max(0, b - pd.Timestamp(date).dayofyear * 5 // 7)
        if C[ytd_i] > 0 and C[b] / C[ytd_i] - 1 < -0.20:
            warn.append("December tax-loss selling season (stock down 20%+ this year)")

    # ---------- position size ----------
    risk_ps = r["Close"] - r["Stop"]
    risk_cash = ACCOUNT["size"] * ACCOUNT["risk_pct"]
    shares = int(min(risk_cash / risk_ps if risk_ps > 0 else 0,
                     ACCOUNT["size"] * ACCOUNT["max_position_pct"] / r["Close"]))

    earn_ok = not (np.isfinite(earn_days) and 0 <= earn_days <= CFG["earnings_block_days"])
    # ---------- the 13-point pre-trade checklist ----------
    checklist = [
        ("Market and sector conditions are supportive", mkt["Points"] >= 6 and sec_ok is not False),
        ("Stock shows relative strength", bool((np.isfinite(rs20) and rs20 > 0) or rs_high)),
        ("Resistance is clearly defined", r["Touches"] + r["ResTests120d"] >= 3),
        ("Breaks resistance on expanding relative volume", v >= CFG["min_vol_ratio"]),
        ("Closes convincingly above resistance", r["ClearATR"] >= 0.25 and r["ClosePos"] >= 0.7),
        ("No significant immediate rejection", True if r["Held"] else None),
        ("VWAP and momentum support the trade", bool(r["AboveAVWAPBase"] and r["MACDH"] > 0 and not r["RSIDiv"]
                                                     and r["WeeklyOK"])),
        ("Retest holds / entry meets the rules", (r["PctAboveRes"] <= TRADE["extended"] * 100) if r["Held"] else None),
        ("Enough upside before the next resistance", bool(r["Room"])),
        ("Stop and invalidation point defined", True),
        ("Position size reflects acceptable loss", shares > 0),
        ("Reward:risk meets the minimum (2:1)", rr >= CFG["min_rr"]),
        ("Liquidity, costs and catalysts acceptable", bool(dv >= 10 and earn_ok)),
    ]
    gate = bool(score >= CFG["score_buy"] and rr >= CFG["min_rr"] and earn_ok and len(warn) < CFG["max_warnings"])

    r.update(
        Score=round(score, 1), Grade="A" if score >= CFG["score_buy"] else "B" if score >= CFG["score_watch"] else "C",
        ScoreParts=" | ".join(f"{k} {x:.0f}/{m}" for k, (x, m) in parts.items()),
        Warnings="; ".join(warn), NWarn=len(warn), Gate=gate,
        RS5=round(rs5, 1) if np.isfinite(rs5) else np.nan, RS20=round(rs20, 1) if np.isfinite(rs20) else np.nan,
        RS63=round(rs63, 1) if np.isfinite(rs63) else np.nan, RSLineHigh=rs_high, Leader=leader,
        Sector=sector or "", SectorRS=round(sec_rs, 1) if np.isfinite(sec_rs) else np.nan,
        Market=f"{mkt['Trend']}, breadth {mkt['Breadth'] * 100:.0f}%, {mkt['DistDays']} dist days"
        if np.isfinite(mkt["Breadth"]) else mkt["Trend"],
        EarningsDate=pd.Timestamp(earn).date().isoformat() if earn is not None else "",
        EarnDays=earn_days, Shares=shares, RiskPerShare=round(risk_ps, 2),
        Checklist="".join("-" if ok is None else "1" if bool(ok) else "0" for _, ok in checklist),
        RSLineUp=rs_line_up, Industry=industry, Precious=precious,
        Signals=" · ".join(lbl for ok, lbl in [
            (r["WeeklyOK"], "weekly trend up"), (r["New20dHigh"], "new 20-day high"),
            (r["OBVConfirm"], "OBV confirms"), (rs_line_up, "RS line rising"), (rs_high, "RS line at new high"),
            (r["AboveCloud"] and r["TKBull"], "above Ichimoku cloud"), (r["Gap"] and v >= 2, "breakaway gap"),
            (r["Squeeze"], "Bollinger squeeze"), (r["DIBull"], "+DI over -DI"), (r["Clear1Pct"], "cleared level by 1%+")]
            if ok),
    )
    return r


CHECKLIST_LABELS = [
    ("Market", "Market and sector conditions are supportive"), ("Market", "Stock shows relative strength"),
    ("Breakout", "Resistance is clearly defined"), ("Breakout", "Breaks resistance on expanding relative volume"),
    ("Breakout", "Closes convincingly above resistance"), ("Breakout", "No significant immediate rejection"),
    ("Entry", "VWAP and momentum support the trade"), ("Entry", "Retest holds / entry meets the rules"),
    ("Entry", "Enough upside before the next resistance"), ("Risk", "Stop and invalidation point defined"),
    ("Risk", "Position size reflects acceptable loss"), ("Risk", "Reward:risk meets the minimum (2:1)"),
    ("Risk", "Liquidity, costs and catalysts acceptable"),
]


PROFILE_COLS = ["Ticker", "Sector", "Industry", "Name", "Country", "Website", "Summary"]
PROFILES = {}   # ticker -> company profile (name, sector, industry, country, website, business summary)


def _load_profiles(cache_path):
    """Company profiles cached in reports/sectors.csv (older files only have Ticker, Sector, Industry)."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            cdf = pd.read_csv(cache_path, dtype=str, keep_default_na=False)
            for rec in cdf.to_dict("records"):
                cache[rec.get("Ticker", "")] = {c: rec.get(c, "") or "" for c in PROFILE_COLS[1:]}
        except Exception:
            cache = {}
    return cache


_WEB = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36", "Accept": "application/json"}


def _retry(fn, waits=(2, 6, 15)):
    """Call fn(); on an error or an empty answer wait and try again. Returns None if every try fails."""
    for k in range(len(waits) + 1):
        try:
            out = fn()
            if out is not None and (not hasattr(out, "__len__") or len(out)):
                return out
        except Exception:
            pass
        if k < len(waits):
            time.sleep(waits[k])
    return None


def _profile_backup(t, p):
    """Fill a missing profile from Yahoo's search endpoint (name, sector, industry) and, for US stocks,
    Nasdaq's company profile (description)."""
    p = dict(p)
    try:
        js = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                          params={"q": t, "quotesCount": 3, "newsCount": 0}, headers=_WEB, timeout=15).json()
        hit = next((x for x in js.get("quotes", []) if x.get("symbol", "").upper() == t.upper()), None)
        if hit:
            p["Name"] = p.get("Name") or hit.get("longname") or hit.get("shortname") or ""
            p["Sector"] = p.get("Sector") or hit.get("sectorDisp") or hit.get("sector") or ""
            p["Industry"] = p.get("Industry") or hit.get("industryDisp") or hit.get("industry") or ""
    except Exception:
        pass
    if region(t) == "US" and not p.get("Summary"):
        try:
            js = requests.get(f"https://api.nasdaq.com/api/company/{t.replace('-', '.')}/company-profile",
                              headers=_WEB, timeout=15).json().get("data") or {}
            val = lambda k: ((js.get(k) or {}).get("value") or "").strip()
            p["Name"] = p.get("Name") or val("CompanyName")
            p["Summary"] = " ".join(val("CompanyDescription").split())
            p["Website"] = p.get("Website") or val("CompanyUrl")
            p["Country"] = p.get("Country") or ""
        except Exception:
            pass
    return p


def _nasdaq_earnings(t):
    """Next earnings date from Nasdaq (US stocks), used when Yahoo has none."""
    try:
        js = requests.get(f"https://api.nasdaq.com/api/analyst/{t.replace('-', '.')}/earnings-date",
                          headers=_WEB, timeout=15).json().get("data") or {}
        m = re.search(r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})", str(js.get("announcement") or ""))
        return pd.Timestamp(m.group(1).replace(".", "")).date() if m else None
    except Exception:
        return None


def fetch_sector_earnings(tickers, cache_path, profile_only=()):
    """Company profile (cached) and next earnings date for the candidates; profile only for `profile_only`
    (tickers already in the tracker). Profiles fill PROFILES so every card can say what the company does."""
    # Yahoo throttles these calls right after the 6,000-stock price download (the 16 Oct run got 0 of 30),
    # so each call is retried with pauses, and two backup sources fill what Yahoo won't give.
    import yfinance as yf
    cache = _load_profiles(cache_path)
    sectors, industries, earnings = {}, {}, {}
    today = dt.date.today()
    want = list(dict.fromkeys(list(tickers) + [t for t in profile_only if t not in tickers]))
    src = {"yahoo": 0, "backup": 0, "none": 0}
    fails = 0                                             # after 3 straight Yahoo failures, stop waiting on it
    for t in want:
        waits = (2, 6, 15) if fails < 3 else ()
        tk = yf.Ticker(t)
        p = cache.get(t) or {}
        if not p.get("Name") or not p.get("Summary"):     # fetch once, then reuse the cached profile
            info = _retry(lambda: tk.get_info() or {}, waits) or {}
            fails = 0 if info.get("longName") or info.get("shortName") else fails + 1
            q = {"Sector": info.get("sector", "") or "", "Industry": info.get("industry", "") or "",
                 "Name": info.get("longName") or info.get("shortName") or "",
                 "Country": info.get("country", "") or "", "Website": info.get("website", "") or "",
                 "Summary": " ".join(str(info.get("longBusinessSummary", "") or "").split())}
            if q["Name"] and q["Summary"]:
                src["yahoo"] += 1
            else:
                q = _profile_backup(t, q)
                src["backup" if q.get("Name") else "none"] += 1
            p = {c: q.get(c) or p.get(c, "") for c in PROFILE_COLS[1:]}
            cache[t] = p
        PROFILES[t] = p
        if t not in tickers:
            continue
        sectors[t], industries[t] = p.get("Sector") or None, p.get("Industry") or None
        cal = _retry(lambda: tk.calendar, (2, 6) if fails < 3 else ())
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        fut = sorted(pd.Timestamp(x).date() for x in dates if pd.Timestamp(x).date() >= today)
        if not fut and region(t) == "US":
            d = _nasdaq_earnings(t)
            fut = [d] if d and d >= today else []
        if fut:
            earnings[t] = fut[0]
    DIAG.append(f"Company profiles fetched: {src['yahoo']} from Yahoo, {src['backup']} from backup sources, "
                f"{src['none']} not found ({len(want) - sum(src.values())} already cached)")
    if cache_path:
        pd.DataFrame([{"Ticker": k, **{c: v.get(c, "") for c in PROFILE_COLS[1:]}} for k, v in sorted(cache.items())],
                     columns=PROFILE_COLS).to_csv(cache_path, index=False)
    return sectors, industries, earnings


def _short_summary(text, limit=320):
    """First one or two sentences of the business summary, cut at ~320 characters."""
    text = (text or "").strip()
    out = ""
    for i, s in enumerate(re.split(r"(?<=[a-z0-9\)]\.)\s+(?=[A-Z])", text)):
        if out and (i >= 2 or len(out) + len(s) + 1 > limit):
            break
        out = f"{out} {s}".strip()
    return out if out and len(out) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def company_html(ticker, full=True):
    """Who the company is: full name, industry, country and what it does (from Yahoo Finance's profile)."""
    p = PROFILES.get(ticker) or {}
    name = p.get("Name") or ""
    if not name and not p.get("Industry"):
        return ""
    e = html_mod.escape
    where = " · ".join(x for x in (p.get("Industry"), p.get("Sector"), p.get("Country")) if x)
    out = f'<div class="co"><p class="coname"><b>{e(name or ticker)}</b>{f" <span>{e(where)}</span>" if where else ""}</p>'
    summ = p.get("Summary") or ""
    if summ:
        short = _short_summary(summ)
        out += f'<p class="codesc">{e(short)}</p>'
        if full and len(short) < len(summ):
            site = p.get("Website") or ""
            link = (f' <a href="{e(site)}" target="_blank" rel="noopener">Company website</a>'
                    if site.startswith("http") else "")
            out += f'<details><summary>More about the company</summary><p class="codesc">{e(summ)}{link}</p></details>'
    return out + "</div>"


STRESS_TICKERS = ["HYG", "IEF", "KBE", "SI=F", "TIP"]   # credit, Treasuries, banks, silver, real-yield proxy


def build_context(data, tickers):
    """Benchmarks, breadth, internals, credit and bank stress shared by the scan and the backtest."""
    us = [t for t in tickers if region(t) == "US"]
    ca = [t for t in tickers if region(t) == "CA"]
    return dict(
        bench={k: data.get(v) for k, v in BENCH.items()},
        breadth={"US": breadth_series(data, us), "CA": breadth_series(data, ca)},
        internals={"US": internals_series(data, us), "CA": internals_series(data, ca)},
        credit=stress_series(data.get("HYG"), data.get("IEF")),                      # [Øygard ch5]
        banks={"US": stress_series(data.get("KBE"), data.get("SPY")),
               "CA": stress_series(data.get("XFN.TO"), data.get("XIU.TO"))},
        sector_data={e: data[e] for e in ALL_SECTOR_ETFS if e in data},
        sector={}, industry={}, earnings={})

# --------------------------------------------------------------------------
# Signal tracker: follows every setup from day to day and gives an action
# --------------------------------------------------------------------------
TRADE = dict(
    buy_zone=0.04,        # buy only within 4% above resistance (same as the "extended" cutoff)
    extended=0.04,        # > 4% above resistance after confirmation = don't chase
    extended_days=10,     # give up on an extended name that never pulls back
    watch_days=5,         # a Watch that never qualifies for a Buy is dropped after 5 sessions
    stop_atr=1.0,         # stop = max(base low, resistance - 1 ATR), at least 1.5% under resistance
    trail_atr=2.0,        # trailing stop after the target: highest close - 2 ATR
    time_stop_days=15,    # sell if no follow-through after 15 sessions
    fail_days=10,         # failed-breakout exit window after entry [Murphy ch5; Hougaard]
    time_stop_gain=0.02,
)

TRACK_COLS = ["Ticker", "Indexes", "Sector", "Category", "Pattern", "Score", "Grade", "ScoreParts", "Checklist",
              "Warnings", "NWarn", "Gate", "BreakoutDate", "BaseStart", "Resistance", "BaseLow", "ATR", "VolX",
              "BaseLen", "NextRes", "SupplyZone", "RR", "RS20", "Market", "EarningsDate", "Shares", "RiskPerShare",
              "GapLow", "Signals",
              "Flags", "Status", "StatusSince", "Stop", "Target", "EntryDate", "EntryPrice", "LastClose",
              "HighClose", "PnLPct", "LastDate", "Note", "Closed", "Source"]   # Source "manual" = your own buy
ACTIVE = {"WATCH", "BUY", "EXTENDED", "HOLD", "TAKE PROFIT", "TRAIL"}

STATUS_INFO = {
    "BUY":         ("Buy", "#16794a", "Breakout confirmed and it passes the scorecard (70+/100, reward:risk 2+, no earnings within 5 sessions, under 4 warnings). Entry plan below."),
    "WATCH":       ("Watch", "#1d5fbf", "Breakout not confirmed yet, or it held but doesn't pass the scorecard. Not a buy yet."),
    "EXTENDED":    ("Don't chase", "#a15c00", "Breakout held but price ran too far above the breakout level. Wait for a pullback."),
    "HOLD":        ("Hold", "#3d4a5c", "You own it (if you took the Buy). Keep the stop order in place."),
    "TAKE PROFIT": ("Take profit", "#7a3db8", "Target hit. Sell part, protect the rest."),
    "TRAIL":       ("Hold - trailing", "#3d4a5c", "Holding the remainder with a stop that rises with the price."),
    "SELL":        ("Sell", "#b42318", "Exit the position (or your stop order already did)."),
    "DROP":        ("Drop", "#6b6b6b", "Setup failed or never qualified before entry. Remove from watchlist."),
}


def _dates(d):
    return np.array([pd.Timestamp(i).date() for i in d.index])


def _num(x, default=np.nan):
    try:
        x = float(x)
        return x if np.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _set(row, status, date, note):
    row["Status"], row["StatusSince"], row["Note"] = status, date, note
    if status in ("SELL", "DROP"):
        row["Closed"] = True


def _gate(row, date, held_bonus=0.0):
    """Scorecard gate for a Buy. Returns (ok, reason)."""
    if pd.isna(row.get("Grade")) or row.get("Grade") in ("", None):
        return True, ""                                   # rows created before the scorecard existed
    score = _num(row["Score"], 0) + held_bonus
    if score < CFG["score_buy"]:
        return False, f"Scores {score:.0f}/100 (Buy needs {CFG['score_buy']})"
    if _num(row["NWarn"], 0) >= CFG["max_warnings"]:
        return False, f"{int(_num(row['NWarn'], 0))} warning signs"
    ed = row.get("EarningsDate")
    if isinstance(ed, str) and ed:
        days = int(np.busday_count(pd.Timestamp(date).date(), pd.Timestamp(ed).date()))
        if 0 <= days <= CFG["earnings_block_days"]:
            return False, f"Earnings in {days} sessions"
    return True, ""


def _start_buy(row, date, close, note):
    stop, target, rr, _ = trade_plan(row["Resistance"], row["BaseLow"], row["ATR"], close, _num(row.get("NextRes")))
    if pd.notna(row.get("Grade")) and row.get("Grade") not in ("", None) and rr < CFG["min_rr"]:
        _set(row, "WATCH", date, f"Held, but reward:risk only {rr} at {close:.2f} - next resistance too close")
        return
    row["EntryDate"], row["EntryPrice"] = date, round(close, 2)
    row["Stop"], row["Target"], row["RR"] = stop, target, rr
    row["HighClose"] = round(close, 2)
    risk = close - stop
    if risk > 0:
        row["Shares"] = int(min(ACCOUNT["size"] * ACCOUNT["risk_pct"] / risk,
                                ACCOUNT["size"] * ACCOUNT["max_position_pct"] / close))
        row["RiskPerShare"] = round(risk, 2)
    _set(row, "BUY", date, note)


def _qualify(row, date, c, note, held_bonus):
    R = row["Resistance"]
    ok, why = _gate(row, date, held_bonus)
    if not ok:
        _set(row, "WATCH", date, f"Held, but not a Buy: {why}")
        row["_watch_only"] = True
    elif c > R * (1 + TRADE["extended"]):
        _set(row, "EXTENDED", date, f"Held, but closed {100 * (c / R - 1):.1f}% above the breakout level")
    else:
        _start_buy(row, date, c, note)


def step_row(row, date, o, h, l, c, n_held):
    """Advance one tracked setup by one daily candle."""
    R, st = row["Resistance"], row["Status"]
    row["LastClose"], row["LastDate"] = round(c, 2), date
    if np.isfinite(_num(row.get("EntryPrice"))):
        row["HighClose"] = round(max(_num(row.get("HighClose"), c), c), 2)
        row["PnLPct"] = round((c / row["EntryPrice"] - 1) * 100, 2)

    if st == "WATCH":
        if c < R or l < R * (1 - CFG["hold_tol"]):
            _set(row, "DROP", date, f"Failed to hold the breakout: closed {c:.2f}, low {l:.2f} vs level {R:.2f}")
        elif str(row.get("Note", "")).startswith("Held, but not a Buy") or str(row.get("Note", "")).startswith("Held, but reward"):
            if n_held >= TRADE["watch_days"]:
                _set(row, "DROP", date, f"Never qualified for a Buy within {TRADE['watch_days']} sessions")
        else:
            _qualify(row, date, c, "Held above the breakout level the next day", held_bonus=2.0)
    elif st == "EXTENDED":
        if c < R * (1 - CFG["hold_tol"]):
            _set(row, "DROP", date, "Fell back below the breakout level")
        elif c <= R * (1 + TRADE["buy_zone"]) and l >= R * (1 - CFG["hold_tol"]):
            _start_buy(row, date, c, "Pulled back to the breakout level and held (retest)")
        elif n_held >= TRADE["extended_days"]:
            _set(row, "DROP", date, "Never pulled back - opportunity missed")
    elif st in ("BUY", "HOLD"):
        if n_held <= TRADE["fail_days"] and c < R * (1 - CFG["hold_tol"]):
            # [Murphy ch5] a decisive close back through the breakout level = failed pattern;
            # [Hougaard] a good breakout works right away. Backtest 2022-26: same expectancy, higher profit factor.
            row["PnLPct"] = round((c / row["EntryPrice"] - 1) * 100, 2)
            _set(row, "SELL", date, f"Failed breakout: closed back below {R * (1 - CFG['hold_tol']):.2f}")
            return
        if l <= row["Stop"]:
            fill = min(o, row["Stop"])
            row["PnLPct"] = round((fill / row["EntryPrice"] - 1) * 100, 2)
            _set(row, "SELL", date, f"Stop hit at {row['Stop']:.2f}" + (f" (gapped down, filled near {fill:.2f})" if o < row["Stop"] else ""))
        elif h >= row["Target"]:
            row["Stop"] = round(max(row["Stop"], row["EntryPrice"]), 2)
            _set(row, "TAKE PROFIT", date, f"Hit target {row['Target']:.2f}")
        elif n_held >= TRADE["time_stop_days"] and c < row["EntryPrice"] * (1 + TRADE["time_stop_gain"]):
            _set(row, "SELL", date, f"Time stop: no follow-through after {n_held} sessions")
        elif st == "BUY":
            _set(row, "HOLD", date, "Breakout working - stop unchanged")
        if row["Status"] == "HOLD" and np.isfinite(_num(row.get("GapLow"))) and c < _num(row.get("GapLow")):
            row["Note"] = f"Warning: closed back into the breakaway gap (below {_num(row.get('GapLow')):.2f}) - weakness"
    elif st in ("TAKE PROFIT", "TRAIL"):
        if l <= row["Stop"]:
            fill = min(o, row["Stop"])
            row["PnLPct"] = round((fill / row["EntryPrice"] - 1) * 100, 2)
            _set(row, "SELL", date, f"Trailing stop hit at {row['Stop']:.2f}")
        else:
            trail = row["HighClose"] - TRADE["trail_atr"] * row["ATR"]
            row["Stop"] = round(max(row["Stop"], trail), 2)
            if st == "TAKE PROFIT":
                _set(row, "TRAIL", date, "Riding the rest with a trailing stop")


def load_tracker(path):
    if os.path.exists(path):
        tr = pd.read_csv(path, dtype={"Ticker": str, "EarningsDate": str, "Grade": str})
        for col in TRACK_COLS:
            if col not in tr.columns:
                tr[col] = np.nan
        tr["Closed"] = tr["Closed"].fillna(False).astype(bool)
        tr = tr[TRACK_COLS].astype(object).where(tr[TRACK_COLS].notna(), np.nan)
        return tr.to_dict("records")
    return []


def update_tracker(rows, res, data, asof):
    """Advance open setups with every new candle, then add today's new setups (score 65+)."""
    for row in rows:
        if row["Closed"] or row["Ticker"] not in data:
            continue
        d = data[row["Ticker"]]
        dts = _dates(d)
        last = pd.Timestamp(row["LastDate"]).date()
        for i in np.where(dts > last)[0]:
            if row["Closed"]:
                break
            in_trade = row["Status"] not in ("WATCH", "EXTENDED") and isinstance(row["EntryDate"], str)
            since = pd.Timestamp(row["EntryDate"] if in_trade else row["StatusSince"]).date()
            n_held = int(((dts > since) & (dts <= dts[i])).sum())
            step_row(row, dts[i].isoformat(), *d[["Open", "High", "Low", "Close"]].iloc[i].tolist(), n_held)

    open_now = {r["Ticker"] for r in rows if not r["Closed"]}
    seen = {(r["Ticker"], str(r["BreakoutDate"])) for r in rows}     # a late catch must not revive a closed setup
    keep = [k for k in TRACK_COLS if k not in ("Status", "StatusSince", "Stop", "Target", "EntryDate", "EntryPrice",
                                               "LastClose", "HighClose", "PnLPct", "LastDate", "Note", "Closed")]
    for _, r in res.iterrows():
        if r["Ticker"] in open_now or _num(r.get("Score"), 0) < CFG["score_watch"]:
            continue
        if (r["Ticker"], str(r["BreakoutDate"])) in seen:
            continue
        row = {k: np.nan for k in TRACK_COLS}
        row.update({k: r[k] for k in keep if k in r})
        row["NextRes"] = _num(r.get("NextRes"))
        row.update(LastClose=r["Close"], LastDate=asof, Closed=False)
        if not r["Held"]:
            row["Stop"], row["Target"] = r["Stop"], r["Target"]
            _set(row, "WATCH", asof, f"Broke out on {r['VolX']}x volume - must hold tomorrow")
        else:
            late = int(_num(r.get("LateDays"), 0))
            _qualify(row, asof, r["Close"], f"Broke out {late} sessions ago (missed then) and has held every day since"
                     if late else "Broke out and held the next day", held_bonus=0.0)
        rows.append(row)
    for row in rows:
        row.pop("_watch_only", None)
    return rows

# --------------------------------------------------------------------------
# Market review (top of every report)
# --------------------------------------------------------------------------
REVIEW_INDEXES = [("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq-100 (QQQ)"), ("DIA", "Dow Jones (DIA)"),
                  ("IWM", "Russell 2000 (IWM)"), ("XIU.TO", "S&P/TSX 60 (XIU)")]   # tracked through their ETFs
REVIEW_MACRO = [("^VIX", "VIX (volatility)"), ("^TNX", "US 10-yr yield"), ("CAD=X", "USD/CAD"),
                ("CL=F", "Crude oil"), ("GC=F", "Gold"), ("SI=F", "Silver"), ("HYG", "High-yield bonds (HYG)")]
SECTOR_NAMES = {"XLK": "Technology", "XLF": "Financials", "XLV": "Health Care", "XLE": "Energy",
                "XLI": "Industrials", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
                "XLU": "Utilities", "XLRE": "Real Estate", "XLB": "Materials", "XLC": "Communication",
                "XEG.TO": "Energy", "XFN.TO": "Financials", "XMA.TO": "Materials", "XIT.TO": "Technology",
                "XRE.TO": "REITs", "XUT.TO": "Utilities", "XST.TO": "Consumer Staples"}
MACRO_TICKERS = [t for t, _ in REVIEW_MACRO]


def _chg(d, n):
    c = d["Close"].dropna()
    return (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n and c.iloc[-1 - n] > 0 else np.nan


def _ytd(d):
    c = d["Close"].dropna()
    prev = c[c.index.year < c.index[-1].year]
    return (c.iloc[-1] / prev.iloc[-1] - 1) * 100 if len(prev) else np.nan


def breadth_today(data, tickers):
    """Advancers/decliners, up-volume share, 52-week highs/lows, % above 50/200-day on the last bar."""
    adv = dec = nh = nl = a50 = a200 = n50 = n200 = 0
    upv = dnv = 0.0
    lasts = [data[t].index[-1] for t in tickers if data.get(t) is not None and len(data[t]) >= 60]
    last = pd.Series(lasts).mode().iloc[0] if lasts else None      # the session most stocks have
    for t in tickers:
        d = data.get(t)
        if d is None or len(d) < 60 or d.index[-1] != last:
            continue
        c, pc, v = d["Close"].iloc[-1], d["Close"].iloc[-2], d["Volume"].iloc[-1]
        if c > pc:
            adv += 1; upv += v * c
        elif c < pc:
            dec += 1; dnv += v * c
        w = d.iloc[-253:-1]
        if len(w) >= 200:
            nh += d["High"].iloc[-1] > w["High"].max()
            nl += d["Low"].iloc[-1] < w["Low"].min()
        if pd.notna(d["SMA50"].iloc[-1]):
            n50 += 1; a50 += c > d["SMA50"].iloc[-1]
        if pd.notna(d["SMA200"].iloc[-1]):
            n200 += 1; a200 += c > d["SMA200"].iloc[-1]
    tot = adv + dec
    return dict(adv=adv, dec=dec, ad=adv / tot if tot else np.nan, upvol=upv / (upv + dnv) if upv + dnv else np.nan,
                nh=int(nh), nl=int(nl), a50=a50 / n50 if n50 else np.nan, a200=a200 / n200 if n200 else np.nan, n=tot)


def market_regime(spy_state, br, vix):
    """Overall stance for breakout trading."""
    trend, dist = spy_state["Trend"], spy_state["DistDays"]
    a50 = br["a50"]
    if spy_state.get("Credit") and trend != "Uptrend":
        return ("Risk-off", "#b42318", "Credit markets are under stress while stocks are not trending up. "
                "Avoid new long entries; protect open positions.")
    if spy_state.get("Thrust") and trend != "Downtrend":
        return ("Constructive", "#1d5fbf", "Breadth thrust: participation just jumped back from oversold. "
                "New uptrends often start this way; normal size on the best setups.")
    if trend == "Downtrend" or (np.isfinite(vix) and vix >= 28) or (np.isfinite(a50) and a50 < 0.25 and trend != "Uptrend"):
        return ("Risk-off", "#b42318", "Breakouts fail often in this tape. Avoid new long entries; protect open positions.")
    if ((np.isfinite(a50) and a50 < 0.45) or dist >= 6 or (np.isfinite(vix) and vix >= 20) or trend == "Mixed"
            or spy_state.get("Credit") or (np.isfinite(spy_state.get("NHNL10", np.nan)) and spy_state["NHNL10"] < 0)):
        return ("Cautious", "#a15c00", "Narrow or shaky market. Trade only the strongest setups, at half size, and take profits sooner.")
    if np.isfinite(a50) and a50 >= 0.6 and dist <= 4 and (not np.isfinite(vix) or vix < 18):
        return ("Risk-on", "#16794a", "Broad, healthy advance. Breakouts have the best odds; normal position size.")
    return ("Constructive", "#1d5fbf", "Market supports breakouts, but be selective; normal size on the best setups.")


def market_review(data, tickers, n_setups=None, live=False, ctx=None):
    spy = data.get("SPY")
    us = [t for t in tickers if region(t) == "US"]
    ca = [t for t in tickers if region(t) == "CA"]
    br_us, br_ca = breadth_today(data, us), breadth_today(data, ca)
    ctx = ctx or build_context(data, tickers)
    st_us = market_state(spy, ctx["breadth"]["US"], spy.index[-1], ctx["internals"]["US"], ctx["credit"],
                         ctx["banks"]["US"]) if spy is not None else \
        dict(Trend="Unknown", Breadth=np.nan, DistDays=0, Pressure=False, Credit=False, Banks=False,
             NHNL10=np.nan, UpVol10=np.nan, Thrust=False)
    xiu = data.get("XIU.TO")
    st_ca = market_state(xiu, ctx["breadth"]["CA"], xiu.index[-1], ctx["internals"]["CA"], ctx["credit"],
                         ctx["banks"]["CA"]) if xiu is not None else None
    vixd = data.get("^VIX")
    vix = vixd["Close"].iloc[-1] if vixd is not None and len(vixd) else np.nan
    vix_chg = vixd["Close"].iloc[-1] - vixd["Close"].iloc[-2] if vixd is not None and len(vixd) > 1 else np.nan
    regime, rcolor, advice = market_regime(st_us, br_us, vix)

    # ---- index & macro table
    def row(t, name, pct=True):
        d = data.get(t)
        if d is None or len(d) < 25:
            return ""
        c = d["Close"].iloc[-1]
        cells = [_chg(d, 1), _chg(d, 5), _chg(d, 21), _ytd(d)]
        inv = t == "^VIX"
        tds = "".join(f'<td class="{"up" if (x >= 0) != inv else "dn"}">{x:+.1f}%</td>' if np.isfinite(x) else "<td>-</td>" for x in cells)
        if t == "^TNX":
            c_txt = f"{c:.2f}%"
            ch = (c - d["Close"].iloc[-2]) * 100
            tds = f'<td class="{"dn" if ch >= 0 else "up"}">{ch:+.0f} bp</td>' + "".join("<td>-</td>" for _ in range(3))
        else:
            c_txt = f"{c:.4f}" if t.endswith("=X") else _f(c)
        trend = ""
        if pct and pd.notna(d["SMA200"].iloc[-1]):
            s50, s200 = d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
            trend = "Uptrend" if c > s50 > s200 else "Downtrend" if c < s50 < s200 else "Mixed"
        return f'<tr><td class="tk">{name}</td><td>{c_txt}</td>{tds}<td class="{trend.lower()}">{trend}</td></tr>'

    idx_rows = "".join(row(t, n) for t, n in REVIEW_INDEXES)
    mac_rows = "".join(row(t, n, pct=False) for t, n in REVIEW_MACRO)
    table = (f'<div class="wrap"><table class="mr"><tr><th class="tk">Market (ETF price)</th><th>Last</th><th>1 day</th><th>1 week</th>'
             f'<th>1 month</th><th>YTD</th><th>Trend</th></tr>{idx_rows}'
             f'<tr class="sep"><td colspan="7">Volatility, rates, currency, commodities</td></tr>{mac_rows}</table></div>')

    # ---- sectors
    def sector_rank(etfs):
        out = []
        for e in etfs:
            d = data.get(e)
            if d is not None and len(d) > 22:
                out.append((SECTOR_NAMES.get(e, e), _chg(d, 5), _chg(d, 21)))
        return sorted([x for x in out if np.isfinite(x[2])], key=lambda x: -x[2])

    us_sec = sector_rank([e for e in SECTOR_ETF["US"].values()])
    ca_sec = sector_rank([e for e in SECTOR_ETF["CA"].values()])

    def sec_list(lst):
        if not lst:
            return '<p class="hint">No sector data.</p>'
        items = "".join(f'<div class="sr" title="{n}: {m:+.1f}% past month, {w:+.1f}% past week"><span>{n}</span><i><b class="{"up" if m >= 0 else "dn"}" '
                        f'style="width:{min(100, abs(m) * 8):.0f}%"></b></i><em class="{"up" if m >= 0 else "dn"}">{m:+.1f}%</em></div>'
                        for n, w, m in lst)
        return f'<div class="srs">{items}</div>'

    # ---- narrative
    def pct(x):
        return f"{x * 100:.0f}%" if np.isfinite(x) else "n/a"

    lines = []
    if spy is not None and len(spy) > 22:
        d1, d21 = _chg(spy, 1), _chg(spy, 21)
        above = "above" if spy["Close"].iloc[-1] > spy["SMA50"].iloc[-1] else "below"
        move = ("is flat so far today" if live else "was flat") if abs(d1) < 0.05 else \
            (f"is {'up' if d1 >= 0 else 'down'} {abs(d1):.1f}% so far today" if live else f"{'rose' if d1 >= 0 else 'fell'} {abs(d1):.1f}%")
        txt = (f"The S&P 500 {move}"
               f" and is {d21:+.1f}% over the past month, {above} its 50-day average ({st_us['Trend'].lower()}).")
        q, iwm = data.get("QQQ"), data.get("IWM")
        if q is not None and iwm is not None and len(q) > 22 and len(iwm) > 22:
            lead = "Large-cap tech" if _chg(q, 21) > _chg(iwm, 21) else "Small caps"
            txt += f" {lead} led over the month (Nasdaq-100 {_chg(q, 21):+.1f}%, Russell 2000 {_chg(iwm, 21):+.1f}%)."
        lines.append(("Indexes", txt))
    if np.isfinite(br_us["a50"]):
        health = ("broad" if br_us["a50"] >= 0.6 else "healthy" if br_us["a50"] >= 0.5 else
                  "narrow" if br_us["a50"] >= 0.3 else "very weak")
        txt = (f"Participation is {health}: {pct(br_us['a50'])} of US stocks are above their 50-day average and "
               f"{pct(br_us['a200'])} above their 200-day. {br_us['nh']} new 52-week highs vs {br_us['nl']} new lows; "
               f"{pct(br_us['ad'])} of stocks advanced, carrying {pct(br_us['upvol'])} of the dollar volume.")
        if np.isfinite(st_us.get("NHNL10", np.nan)):
            txt += (f" Over 10 days, new highs minus new lows average {st_us['NHNL10']:+.0f} a day"
                    f" and {pct(st_us['UpVol10'])} of dollar volume went into rising stocks.")
        if st_us["DistDays"] >= 5:
            txt += f" {st_us['DistDays']} distribution days in the last 25 sessions point to institutional selling."
        if st_us.get("Thrust"):
            txt += " Breadth thrust: the share of stocks above their 50-day just recovered from below 30%."
        lines.append(("Breadth", txt))
    if np.isfinite(vix):
        mood = "calm" if vix < 15 else "normal" if vix < 20 else "elevated" if vix < 28 else "stressed"
        txt = f"Volatility is {mood}: VIX {vix:.1f} ({vix_chg:+.1f})."
        tnx, cad = data.get("^TNX"), data.get("CAD=X")
        if tnx is not None and len(tnx) > 6:
            txt += f" The US 10-year yield is {tnx['Close'].iloc[-1]:.2f}% ({(tnx['Close'].iloc[-1] - tnx['Close'].iloc[-6]) * 100:+.0f} bp this week)."
        if cad is not None and len(cad) > 6:
            txt += f" USD/CAD {cad['Close'].iloc[-1]:.4f}."
        if st_us.get("Credit"):
            txt += " Credit stress: high-yield bonds are breaking down against Treasuries."
        elif "HYG" in data:
            txt += " Credit markets are calm (high-yield bonds holding up against Treasuries)."
        if st_us.get("Banks"):
            txt += " Bank stocks are breaking down relative to the market."
        lines.append(("Risk", txt))
    gold, silver, tip = data.get("GC=F"), data.get("SI=F"), data.get("TIP")
    if gold is not None and silver is not None and len(gold) > 25 and len(silver) > 25:
        gsr = (gold["Close"] / silver["Close"].reindex(gold.index).ffill()).dropna()
        if len(gsr) > 21:
            now_r, prev_r = gsr.iloc[-1], gsr.iloc[-21]
            mtxt = (f"Gold/silver ratio {now_r:.0f} ({'falling' if now_r < prev_r else 'rising'} over the month); "
                    + ("a ratio above 80 that turns down has marked silver-led metals rallies. " if now_r >= 80 and now_r < prev_r
                       else "silver is leading gold. " if now_r < prev_r else "gold is leading silver. "))
            if tip is not None and len(tip) > 21:
                tchg = _chg(tip, 21)
                mtxt += (f"Real yields are {'falling' if tchg > 0 else 'rising'} (TIPS {tchg:+.1f}% over the month), "
                         + ("a tailwind for precious metals." if tchg > 0 else "a headwind for precious metals."))
            mon = pd.Timestamp(spy.index[-1]).month if spy is not None else dt.date.today().month
            if mon in (6, 7, 8):
                mtxt += " Seasonally, June-August is the weakest stretch for metals."
            elif mon in (9, 10, 11, 12, 1, 2):
                mtxt += " Seasonally, September-February is metals' strongest stretch."
            lines.append(("Metals", mtxt))
    if len(us_sec) >= 4:
        lines.append(("Sectors", f"Leading over the month: {us_sec[0][0]} ({us_sec[0][2]:+.1f}%) and {us_sec[1][0]} ({us_sec[1][2]:+.1f}%). "
                                 f"Lagging: {us_sec[-1][0]} ({us_sec[-1][2]:+.1f}%) and {us_sec[-2][0]} ({us_sec[-2][2]:+.1f}%)."))
    if xiu is not None and len(xiu) > 22 and st_ca:
        lines.append(("Canada", f"The TSX 60 is {_chg(xiu, 1):+.1f}% on the day and {_chg(xiu, 21):+.1f}% over the month "
                                f"({st_ca['Trend'].lower()}); {pct(br_ca['a50'])} of TSX stocks are above their 50-day average"
                                + (f", with {ca_sec[0][0]} leading." if ca_sec else ".")))
    if n_setups is not None:
        lines.append(("Scanner", f"{n_setups} breakout{'s' if n_setups != 1 else ''} passed every hard rule"
                                 f"{' so far today' if live else ' today'}; see the scores below."))
    story = "".join(f"<li><b>{k}.</b> {v}</li>" for k, v in lines)

    nh10 = st_us.get("NHNL10", np.nan)
    tiles = [("US breadth", pct(br_us["a50"]), "above 50-day"),
             ("New highs − lows", f"{nh10:+.0f}" if np.isfinite(nh10) else f"{br_us['nh']} / {br_us['nl']}",
              "10-day average, US" if np.isfinite(nh10) else "52-week, US"),
             ("Distribution days", str(st_us["DistDays"]), "S&P 500, 25 sessions"
              + (" · credit stress" if st_us.get("Credit") else "")),
             ("VIX", f"{vix:.1f}" if np.isfinite(vix) else "-", f"{vix_chg:+.1f} today" if np.isfinite(vix_chg) else ""),
             ("Advancers", pct(br_us["ad"]), f"{br_us['adv']:,} up / {br_us['dec']:,} down"),
             ("Canada breadth", pct(br_ca["a50"]), "TSX above 50-day")]
    tiles_html = "".join(f'<div class="tile"><span>{a}</span><b>{b}</b><small>{c}</small></div>' for a, b, c in tiles)

    html = f"""<section class="review">
<div class="stance" style="--c:{rcolor}"><span>Market stance</span><b>{regime}</b><p>{advice}</p></div>
<h2>Market review</h2>
<ul class="story">{story}</ul>
<div class="tiles">{tiles_html}</div>
{table}
<details><summary>Sector performance, past month</summary><div class="secs"><div><h4>US sectors</h4>{sec_list(us_sec)}</div>
<div><h4>Canadian sectors</h4>{sec_list(ca_sec)}</div></div></details>
</section>"""
    MARKET_STANCE.clear()
    MARKET_STANCE.update(regime=regime, advice=advice)
    return html


MARKET_STANCE = {}

REVIEW_CSS = """
.review{margin:10px 0 6px}
.stance{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--c);border-radius:12px;padding:12px 14px}
.stance span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.stance b{font-size:22px;color:var(--c)} .stance p{margin:4px 0 0}
.story{font-size:15px;line-height:1.5;margin:8px 0 12px;padding:0;list-style:none}
.story li{margin:0 0 8px} .story b{color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.tile span{display:block;color:var(--muted);font-size:11px} .tile b{font-size:18px} .tile small{display:block;color:var(--muted);font-size:11px}
table.mr td,table.mr th{font-size:13px;padding:7px 8px} table.mr tr.sep td{color:var(--muted);font-size:11px;text-transform:uppercase;background:var(--bg)}
td.uptrend{color:var(--up)} td.downtrend{color:var(--dn)} td.mixed{color:#a15c00}
.secs{display:grid;grid-template-columns:1fr;gap:10px} h4{margin:10px 0 4px;font-size:13px;color:var(--muted)}
.sr{display:grid;grid-template-columns:150px 1fr 54px;gap:8px;align-items:center;font-size:13px;margin:4px 0}
.sr i{display:block;height:8px;background:var(--line);border-radius:4px;overflow:hidden} .sr i b{display:block;height:100%}
.sr i b.up{background:var(--up)} .sr i b.dn{background:var(--dn)} .sr em{font-style:normal;text-align:right}
@media (min-width:640px){.secs{grid-template-columns:1fr 1fr}}
@media (max-width:420px){.tiles{grid-template-columns:repeat(2,1fr)}}
"""

# --------------------------------------------------------------------------
# "Scan now" buttons: start a GitHub run from the page
# --------------------------------------------------------------------------
RUN_CSS = """
.lookbar{display:flex;gap:6px;margin:8px 0 4px} .lookbar input{flex:1;min-width:0;padding:9px 10px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px}
.lookbar button{padding:9px 14px;border-radius:9px;border:0;background:var(--ink);color:var(--bg);font-weight:600}
.runbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.runbar button{font:600 14px/1 -apple-system,"Segoe UI",Roboto,sans-serif;border:0;border-radius:10px;padding:11px 14px;cursor:pointer;
  background:#1d5fbf;color:#fff} .runbar button.alt{background:var(--card);color:var(--ink);border:1px solid var(--line)}
.runbar button:disabled{opacity:.6}
#runstatus{font-size:13px;color:var(--muted);width:100%}
#runsetup{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0;font-size:14px;display:none}
#runsetup input{width:100%;font:14px monospace;padding:9px;border:1px solid var(--line);border-radius:8px;margin:6px 0;background:var(--bg);color:var(--ink)}
#runsetup ol{padding-left:20px;margin:6px 0} #runsetup li{margin:4px 0}
#runsetup button{font:600 14px/1 -apple-system,"Segoe UI",Roboto,sans-serif;border:0;border-radius:10px;padding:10px 14px;background:#1d5fbf;color:#fff;cursor:pointer}
#runsetup button.alt{background:var(--bg);color:var(--ink);border:1px solid var(--line)}
"""

RUN_HTML = """
<div class="runbar">
  <button id="btn-scan" onclick="runNow('scan')">&#8635; Scan now</button>
  <button class="alt" id="btn-morning" onclick="runNow('morning')">Morning check now</button>
  <button class="alt" onclick="toggleSetup()">&#9881;</button>
  <div id="runstatus"></div>
</div>
<form class="lookbar" onsubmit="event.preventDefault(); lookUp();">
  <input id="lk" placeholder="Look up a ticker: SHOP.TO, NVDA, L.TO..." autocomplete="off" autocapitalize="characters" maxlength="15">
  <button type="submit">Analyze</button>
</form>
<div id="runsetup">
  <b>One-time setup for the Scan now button</b>
  <ol>
    <li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub &rarr; new fine-grained token</a>.</li>
    <li>Name it "scan button", set an expiry (up to 1 year).</li>
    <li>Repository access: <b>Only select repositories</b> &rarr; <b>breakout-scanner</b>.</li>
    <li>Permissions &rarr; Repository &rarr; <b>Actions: Read and write</b>. Nothing else.</li>
    <li>Generate, copy the token, paste it here and tap Save.</li>
  </ol>
  <input id="tok" type="password" placeholder="github_pat_..." autocomplete="off">
  <button onclick="saveTok()">Save on this device</button> <button class="alt" onclick="clearTok()">Remove</button>
  <p class="hint">The token stays in this browser only. It can do nothing except start and read runs of this one repository.
  Without it, the buttons open GitHub's Run page instead.</p>
</div>
<script>
(function(){
  const owner = location.hostname.split('.')[0];
  const repo = location.pathname.split('/').filter(Boolean)[0] || 'breakout-scanner';
  const api = `https://api.github.com/repos/${owner}/${repo}/actions`;
  const base = `/${repo}/`;
  const KEY = 'scanTok';
  const st = document.getElementById('runstatus');
  const get = () => { try { return localStorage.getItem(KEY) || ''; } catch(e) { return ''; } };
  window.toggleSetup = () => { const s = document.getElementById('runsetup'); s.style.display = s.style.display === 'block' ? 'none' : 'block'; };
  window.saveTok = () => { try { localStorage.setItem(KEY, document.getElementById('tok').value.trim()); } catch(e) {} toggleSetup(); st.textContent = 'Saved. Tap Scan now.'; };
  window.clearTok = () => { try { localStorage.removeItem(KEY); } catch(e) {} st.textContent = 'Token removed from this device.'; };
  const H = t => ({ 'Authorization': 'Bearer ' + t, 'Accept': 'application/vnd.github+json' });
  window.lookUp = () => {
    const k = (document.getElementById('lk').value || '').trim().toUpperCase().replace(/ +/g, '');
    if (!/^[A-Z0-9][A-Z0-9.=^-]{0,14}$/.test(k)) { st.textContent = 'Type a ticker, e.g. SHOP.TO or NVDA.'; return; }
    runNow('lookup', k);
  };
  window.runNow = async (mode, ticker) => {
    const t = get();
    if (!t) { window.open(`https://github.com/${owner}/${repo}/actions/workflows/scan.yml`, '_blank');
      st.innerHTML = 'On GitHub: tap <b>Run workflow</b>, pick <b>' + mode + '</b>' + (ticker ? ', type <b>' + ticker + '</b>' : '') + ', then <b>Run workflow</b>. Or set up one-tap runs with &#9881;.'; return; }
    document.querySelectorAll('.runbar button, .lookbar button').forEach(b => b.disabled = true);
    const started = new Date();
    st.textContent = 'Starting ' + (ticker ? 'lookup of ' + ticker : mode) + '...';
    const inputs = ticker ? { mode, ticker } : { mode };
    try {
      const r = await fetch(`${api}/workflows/scan.yml/dispatches`, { method: 'POST', headers: H(t),
        body: JSON.stringify({ ref: 'main', inputs }) });
      if (r.status !== 204) { throw new Error(r.status === 401 || r.status === 403 ? 'token rejected - check it in setup' : 'GitHub said ' + r.status); }
      let run = null;
      for (let i = 0; i < 160; i++) {
        await new Promise(res => setTimeout(res, i < 3 ? 5000 : 15000));
        const q = await fetch(`${api}/runs?event=workflow_dispatch&per_page=5`, { headers: H(t) });
        const j = await q.json();
        run = (j.workflow_runs || []).find(x => new Date(x.created_at) >= new Date(started - 60000));
        const mins = Math.round((new Date() - started) / 60000);
        if (!run) { st.textContent = 'Waiting for GitHub to start the run...'; continue; }
        if (run.status !== 'completed') { st.textContent = (run.status === 'queued' ? 'Queued (another run is finishing first)' : 'Running') + ' (' + mins + ' min). '
          + (ticker ? 'A lookup takes about 2-3 minutes.' : 'A full scan takes about 6-8 minutes.') + ' You can leave this page open.'; continue; }
        if (run.conclusion === 'success') {
          st.textContent = 'Done. Reloading...';
          const ny = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
          const mins = ny.getHours() * 60 + ny.getMinutes();
          const open = ny.getDay() > 0 && ny.getDay() < 6 && mins >= 570 && mins < 960;
          const page = mode === 'esg_lookup' ? 'esg/lookup/' + ticker + '.html' : mode === 'esg' ? 'esg/index.html' : ticker ? 'lookup/' + ticker + '.html' : mode === 'morning' ? 'morning.html' : (open ? 'live.html' : 'index.html');
          setTimeout(() => { location.href = base + page + '?t=' + Date.now(); }, 25000);
        } else { st.innerHTML = 'The run ended with "' + run.conclusion + '". <a href="' + run.html_url + '" target="_blank">See details</a>.'; }
        break;
      }
    } catch (e) { st.textContent = 'Could not start: ' + e.message; }
    document.querySelectorAll('.runbar button, .lookbar button').forEach(b => b.disabled = false);
  };
})();
</script>
"""

# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def chart_png(t, d, row, bars=120):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    w = d.iloc[-bars:]
    x = np.arange(len(w))
    dts = _dates(w)
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(7, 4.2), sharex=True,
                                  gridspec_kw=dict(height_ratios=[3, 1], hspace=0.05))
    up = (w["Close"] >= w["Open"]).to_numpy()
    col = np.where(up, "#1a9e5f", "#d1453b")
    ax.vlines(x, w["Low"], w["High"], color=col, lw=0.8)
    ax.bar(x, (w["Close"] - w["Open"]).abs().clip(lower=1e-9), bottom=w[["Open", "Close"]].min(axis=1),
           color=col, width=0.7)
    ax.plot(x, w["SMA50"], color="#4a6fd1", lw=1, label="50-day avg")
    ax.plot(x, w["SMA200"], color="#999", lw=1, label="200-day avg")
    ax.axhline(row["Resistance"], color="#e08a00", lw=1.2, ls="--", label=f"Breakout level {row['Resistance']:.2f}")
    if pd.notna(row.get("Stop")):
        ax.axhline(row["Stop"], color="#b42318", lw=1, ls=":", label=f"Stop {row['Stop']:.2f}")
    if pd.notna(row.get("Target")):
        ax.axhline(row["Target"], color="#16794a", lw=1, ls=":", label=f"Target {row['Target']:.2f}")
    try:
        bs, bo = pd.Timestamp(row["BaseStart"]).date(), pd.Timestamp(row["BreakoutDate"]).date()
        i0 = int(np.searchsorted(dts, bs)); i1 = int(np.searchsorted(dts, bo))
        ax.axvspan(i0 - 0.5, i1 - 0.5, color="#e08a00", alpha=0.12)
        ax.annotate("breakout", (i1, w["High"].iloc[min(i1, len(w) - 1)]), xytext=(0, 8),
                    textcoords="offset points", ha="center", fontsize=7, color="#a15c00")
    except Exception:
        pass
    ax.legend(fontsize=7, loc="upper left", frameon=False)
    axv.bar(x, w["Volume"], color=col, width=0.7)
    axv.plot(x, w["AvgVol50"], color="#333", lw=1)
    axv.set_yticks([])
    step = max(1, len(w) // 6)
    axv.set_xticks(x[::step])
    axv.set_xticklabels([dd.strftime("%b %d") for dd in dts[::step]], fontsize=7)
    ax.tick_params(labelsize=7)
    for a_ in (ax, axv):
        for sp in ("top", "right"):
            a_.spines[sp].set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def market_context(data):
    rows = []
    for t, name in [("SPY", "S&P 500"), ("DIA", "Dow"), ("QQQ", "Nasdaq-100"),
                    ("IWM", "Russell 2000"), ("XIU.TO", "TSX 60")]:
        if t not in data or len(data[t]) < 200:
            continue
        d = data[t]
        c, s50, s200 = d["Close"].iloc[-1], d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
        chg = (c / d["Close"].iloc[-2] - 1) * 100
        trend = "Uptrend" if c > s50 > s200 else ("Downtrend" if c < s50 < s200 else "Mixed")
        rows.append((name, round(c, 2), round(chg, 2), trend))
    return rows


def _f(x, nd=2):
    return "-" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:,.{nd}f}"


def _pct(a, b):
    return f"{(a / b - 1) * 100:+.1f}%"


def _size_line(r):
    sh, rps = _num(r.get("Shares")), _num(r.get("RiskPerShare"))
    if not np.isfinite(sh) or sh <= 0 or not np.isfinite(rps):
        return ""
    if r.get("Source") == "manual":
        return (f" Your position: <b>{f'{sh:,.4f}'.rstrip('0').rstrip('.')} shares at {_f(r['EntryPrice'])}</b> "
                f"(${sh * r['EntryPrice']:,.0f}; risking ${sh * rps:,.0f} to the original stop).")
    return (f" Size: <b>{int(sh)} shares</b> (risking {_f(rps)}/share = ${sh * rps:,.0f}, "
            f"{ACCOUNT['risk_pct'] * 100:.1f}% of a ${ACCOUNT['size']:,} account).")


def instruction(r):
    st, R = r["Status"], r["Resistance"]
    if st == "BUY":
        hi = R * (1 + TRADE["buy_zone"])
        return (f"Buy at tomorrow's open if it opens between <b>{_f(R)}</b> and <b>{_f(hi)}</b> "
                f"and trades above the day's VWAP (see the morning check). Skip if it gaps above {_f(hi)} or opens below {_f(R)}. "
                f"Place a stop order at <b>{_f(r['Stop'])}</b> ({_pct(r['Stop'], r['EntryPrice'])}); "
                f"target <b>{_f(r['Target'])}</b> ({_pct(r['Target'], r['EntryPrice'])}), reward:risk {_f(_num(r.get('RR')), 1)}."
                + _size_line(r))
    if st == "WATCH":
        note = str(r.get("Note", ""))
        if note.startswith("Held, but"):
            return f"{note}. Keep watching; it is dropped after {TRADE['watch_days']} sessions if it doesn't qualify."
        if LIVE["on"]:
            return (f"Breaking above <b>{_f(R)}</b> right now on a projected {r['VolX']}x normal volume. Not confirmed: "
                    f"it needs to close above {_f(R)} today, then hold tomorrow. Tonight's scan will pick it up if it does.")
        return (f"Broke above <b>{_f(R)}</b> today on {r['VolX']}x normal volume. Don't buy yet: tomorrow it must "
                f"close above {_f(R)} without trading below {_f(R * (1 - CFG['hold_tol']))}, and pass the scorecard.")
    if st == "EXTENDED":
        return (f"The breakout held, but price is {_pct(r['LastClose'], R)} above the breakout level. "
                f"Don't chase it. Buy only if it pulls back to <b>{_f(R)}-{_f(R * (1 + TRADE['buy_zone']))}</b> and holds (a retest).")
    if st == "HOLD":
        addon = _num(r.get("EntryPrice")) + _num(r.get("ATR"))
        add_txt = (f" Optional add-on (smaller than the first buy, only while winning): above {_f(addon)}."
                   if np.isfinite(addon) and _num(r.get("LastClose"), 0) < addon else "")
        note = f" <b>{r['Note']}</b>." if str(r.get("Note", "")).startswith("Warning") else ""
        return (f"In the trade since {r['EntryDate']} at {_f(r['EntryPrice'])} (<b>{r['PnLPct']:+.1f}%</b>). "
                f"Keep the stop order at <b>{_f(r['Stop'])}</b>; target <b>{_f(r['Target'])}</b>." + note + add_txt)
    if st == "TAKE PROFIT":
        return (f"Hit the {_f(r['Target'])} target (<b>{r['PnLPct']:+.1f}%</b>). Sell about half. "
                f"Move the stop on the rest to <b>{_f(r['Stop'])}</b> (your entry), so it can't turn into a loss.")
    if st == "TRAIL":
        return (f"Holding the remainder, <b>{r['PnLPct']:+.1f}%</b> since {r['EntryDate']}. "
                f"Move your stop order up to <b>{_f(r['Stop'])}</b>; it rises as the price does.")
    if st == "SELL":
        res = f" Result <b>{r['PnLPct']:+.1f}%</b> from {_f(r['EntryPrice'])}." if pd.notna(r.get("PnLPct")) else ""
        stop_hit = "stop" in str(r["Note"]).lower() and "time" not in str(r["Note"]).lower()
        lead = "Your stop order should have filled." if stop_hit else "Sell at tomorrow's open."
        return f"{lead} {r['Note']}.{res}"
    if st == "DROP":
        return f"Take it off the watchlist. {r['Note']}."
    return r.get("Note", "")


def _score_bars(parts):
    out = []
    for seg in str(parts or "").split(" | "):
        try:
            name, frac = seg.rsplit(" ", 1)
            got, mx = (float(x) for x in frac.split("/"))
        except ValueError:
            continue
        pct = 0 if mx == 0 else max(0, min(100, got / mx * 100))
        out.append(f'<div class="sb"><span>{name}</span><i><b style="width:{pct:.0f}%"></b></i><em>{got:.0f}/{mx:.0f}</em></div>')
    return "".join(out)


def _checklist_html(code):
    code = str(code or "")
    if len(code) != len(CHECKLIST_LABELS):
        return ""
    sym = {"1": ('<b class="ok">&#10003;</b>', ""), "0": ('<b class="no">&#10007;</b>', ""), "-": ('<b class="na">&middot;</b>', " (pending)")}
    rows_, last = [], None
    for (grp, label), ch in zip(CHECKLIST_LABELS, code):
        if grp != last:
            rows_.append(f'<li class="grp">{grp}</li>')
            last = grp
        mark, extra = sym.get(ch, sym["-"])
        rows_.append(f"<li>{mark}{label}{extra}</li>")
    return f'<ul class="ck">{"".join(rows_)}</ul><p class="hint">{code.count("1")}/13 checks passed</p>'


def card_html(r, chart=None, compact=False):
    label, color, _ = STATUS_INFO.get(r["Status"], (r["Status"], "#555", ""))
    new = r["StatusSince"] == r["LastDate"] and r["Status"] not in ("HOLD", "TRAIL")
    score = _num(r.get("Score"))
    grade = r.get("Grade") if isinstance(r.get("Grade"), str) else ""
    warns = [w for w in str(r.get("Warnings") or "").split("; ") if w and w != "nan"]
    if not warns:
        warns = [f for f in str(r.get("Flags") or "").split("; ") if f and f != "nan"]
    nres = _num(r.get("NextRes"))
    facts = [("Breakout level", _f(r["Resistance"])), ("Stop", _f(_num(r.get("Stop")))),
             ("Target", _f(_num(r.get("Target")))), ("Reward:risk", _f(_num(r.get("RR")), 1)),
             ("Volume", f"{r['VolX']}x avg"), ("Next resistance", _f(nres) if np.isfinite(nres) else "None (clear sky)"),
             ("vs market 20d", f"{_num(r.get('RS20')):+.1f} pts" if np.isfinite(_num(r.get("RS20"))) else "-"),
             ("Sector", r.get("Sector") if isinstance(r.get("Sector"), str) and r.get("Sector") else "-"),
             ("Earnings", r.get("EarningsDate") if isinstance(r.get("EarningsDate"), str) and r.get("EarningsDate") else "Not found")]
    facts_html = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in facts)
    warn_html = "".join(f'<span class="flag">{w}</span>' for w in warns)
    score_html = (f'<span class="score g{grade}">{score:.0f}<small>/100</small></span>' if np.isfinite(score) and grade else "")
    detail = ""
    if not compact and grade:
        detail = (f'<details><summary>Scorecard &amp; checklist</summary><div class="sbs">{_score_bars(r.get("ScoreParts"))}</div>'
                  f'{_checklist_html(r.get("Checklist"))}'
                  + (f'<p class="hint"><b>Confirming signals:</b> {r.get("Signals")}</p>'
                     if isinstance(r.get("Signals"), str) and r.get("Signals") else "")
                  + f'<p class="hint">Market at signal: {r.get("Market", "")}</p></details>')
    chart_html = (f'<details><summary>Chart</summary><img alt="{r["Ticker"]} chart" '
                  f'src="data:image/png;base64,{chart}"/></details>') if chart else ""
    return f"""<article class="card" id="t-{r['Ticker']}" style="--c:{color}">
  <header><div><h3>{r['Ticker']} {score_html}</h3><p class="sub">{r['Pattern']} &middot; {r['Indexes']}</p></div>
  <div class="right"><span class="badge">{label}{' &middot; new' if new else ''}</span><p class="px">{_f(r['LastClose'])}</p></div></header>
  {company_html(r['Ticker'], full=not compact)}
  <p class="do">{instruction(r)}</p>
  {'' if compact else f'<div class="facts">{facts_html}</div>'}
  {warn_html and f'<div class="flags"><span class="wl">{len(warns)} warning{"s" if len(warns) != 1 else ""}:</span>{warn_html}</div>'}
  {detail}{chart_html}
</article>"""


def load_holdings(path):
    """Your own positions (reports/holdings.csv): Ticker, Name, Shares, AvgCost, Currency, Type (trade/hold), Note,
    TrendTicker (optional: read the trend from another listing, e.g. a CDR's US shares)."""
    if not path or not os.path.exists(path):
        return []
    try:
        return pd.read_csv(path, dtype={"Ticker": str}, keep_default_na=False).to_dict("records")
    except Exception as e:
        DIAG.append(f"holdings.csv unreadable: {str(e)[:100]}")
        return []


def holding_status(d):
    """Trend read for a long-term holding (ETF or stock you keep, not a breakout trade)."""
    c, s50, s200 = d["Close"].iloc[-1], d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
    atr = d["ATR14"].iloc[-1]
    rising200 = len(d) > 220 and s200 > d["SMA200"].iloc[-21]
    # chandelier-style line: highest close of the last 22 sessions minus 3 ATR - a close under it ends the short-term uptrend
    line = d["Close"].iloc[-22:].max() - 3 * atr if np.isfinite(atr) else np.nan
    if not np.isfinite(s200):
        return "Not enough history", "#6b6b6b", "", line
    if c > s50 > s200 and rising200:
        return "Uptrend - hold", "#16794a", "Above its rising 50- and 200-day averages.", line
    if c > s200:
        return ("Pullback - watch", "#a15c00", "Below its 50-day average but above the 200-day: a normal dip "
                "unless it keeps falling.", line) if c < s50 else \
               ("Recovering - hold", "#3d4a5c", "Above both averages; the long-term trend is still turning up.", line)
    return ("Below 200-day - review", "#b42318", "Under its 200-day average: the long-term trend is down. "
            "Decide whether you still want to own it here.", line)


def portfolio_html(holdings, data, rows, usdcad=np.nan):
    """'Your portfolio' section: value, P&L and a trend read for each of your positions."""
    if not holdings:
        return ""
    e = html_mod.escape
    open_rows = {r["Ticker"]: r for r in rows if not r["Closed"]}
    cards, tot_cad, cost_cad = [], 0.0, 0.0
    for h in holdings:
        t = str(h["Ticker"]).strip()
        sh, cost, cur = _num(h.get("Shares")), _num(h.get("AvgCost")), (h.get("Currency") or "CAD").upper()
        p = PROFILES.get(t) or {}
        name = h.get("Name") or p.get("Name") or t
        d = data.get(t)
        if d is None or not len(d):
            cards.append(f'<article class="card" style="--c:#6b6b6b"><header><div><h3>{e(t)}</h3>'
                         f'<p class="sub">{e(name)}</p></div></header><p class="do">No price data today.</p></article>')
            continue
        c = float(d["Close"].iloc[-1])
        chg1 = _chg(d, 1)
        val, pnl = sh * c, (c / cost - 1) * 100 if cost > 0 else np.nan
        fx = usdcad if cur == "USD" else 1.0
        if np.isfinite(fx):
            tot_cad += val * fx
            cost_cad += sh * cost * fx
        if h.get("Type") == "trade" and t in open_rows:
            r = open_rows[t]
            label, color, _ = STATUS_INFO.get(r["Status"], (r["Status"], "#555", ""))
            what = (f'Managed as a breakout trade - see its card below. Stop <b>{_f(_num(r.get("Stop")))}</b>, '
                    f'target <b>{_f(_num(r.get("Target")))}</b>.')
            line = np.nan
        else:
            tt = str(h.get("TrendTicker") or "").strip()      # e.g. a CDR read through its US shares
            td = data.get(tt) if tt else None
            src = td if td is not None and len(td) > 220 else d
            label, color, what, line = holding_status(src)
            if src is not d:
                what = f"Trend read from {e(tt)} (this listing has too little history). " + what
            elif np.isfinite(line) and c < line:
                what += f' It has already closed under its short-term trend line (<b>{_f(line)}</b>).'
            elif np.isfinite(line):
                what += f' A close below <b>{_f(line)}</b> would end the short-term uptrend.'
        pnl_txt = f'<em class="{"up" if pnl >= 0 else "dn"}">{pnl:+.1f}%</em>' if np.isfinite(pnl) else "-"
        facts = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in [
            ("Shares", f"{sh:,.4f}".rstrip("0").rstrip(".")), ("Avg cost", f"{_f(cost)} {cur}"), ("Value", f"{val:,.0f} {cur}"),
            ("Total return", pnl_txt), ("Today", f"{chg1:+.2f}%" if np.isfinite(chg1) else "-"),
            ("50 / 200-day", f'{_f(d["SMA50"].iloc[-1])} / {_f(d["SMA200"].iloc[-1])}')])
        note = f'<p class="hint">{e(h["Note"])}</p>' if h.get("Note") else ""
        cards.append(f'<article class="card" style="--c:{color}"><header><div><h3>{e(t)}</h3>'
                     f'<p class="sub">{e(name)}</p></div><div class="right"><span class="badge">{label}</span>'
                     f'<p class="px">{_f(c)}</p></div></header><p class="do">{what}</p>'
                     f'<div class="facts">{facts}</div>{note}</article>')
    tot = (f'<p class="hint">Total about <b>${tot_cad:,.0f} CAD</b> '
           f'({(tot_cad / cost_cad - 1) * 100:+.1f}% vs cost; USD converted at {usdcad:.4f}).</p>'
           if cost_cad > 0 and np.isfinite(usdcad) else "")
    return (f'<section id="portfolio"><h2>Your portfolio</h2><p class="hint">Your own positions from <code>holdings.csv</code>. '
            f'ETFs and long-term holdings get a trend read (50- and 200-day averages); breakout trades follow the '
            f'tracker rules. Trend signals only, not advice.</p>{tot}{"".join(cards)}</section>')


def radar_html(radar):
    """'On the radar' table: names coiling just under resistance, with the price that would trigger a breakout."""
    if not radar:
        return ('<section><h2>On the radar</h2><p class="empty">No tight bases within 3% of breaking out today.</p></section>')
    e = html_mod.escape

    def row(f):
        p = PROFILES.get(f["Ticker"]) or {}
        notes = [x for x, ok in (("rising lows", f.get("HigherLows")), ("range tightening", f.get("Tightening")),
                                 ("quiet volume", _num(f.get("QuietVol"), 9) < 1.0), ("uptrend", f.get("Uptrend")),
                                 ("up this week", f.get("Rising5d"))) if ok]
        since = pd.Timestamp(f["FirstSeen"]).strftime("%b %d")
        return (f'<tr><td class="tk"><b>{f["Ticker"]}</b></td><td class="cn">{e(p.get("Name") or "-")}'
                f'<br><small class="hint">{e(p.get("Industry") or p.get("Sector") or "")}</small></td>'
                f'<td>{_f(f["Close"])}</td><td><b>{_f(f["Trigger"])}</b></td><td>{f["DistPct"]:.1f}%</td>'
                f'<td>{f["VolNeeded"]:,}</td><td>{f["BaseLen"]} days</td><td>{since}</td>'
                f'<td class="cn">{", ".join(notes) or "-"}</td></tr>')

    ca = [f for f in radar if f["Region"] == "CA"]
    us = [f for f in radar if f["Region"] != "CA"]
    head = ('<tr><th class="tk">Ticker</th><th>Company</th><th>Close</th><th>Trigger</th>'
            '<th>Away</th><th>Volume needed</th><th>Base</th><th>On radar since</th><th>Why it looks ready</th></tr>')
    tbl = lambda xs, name: (f'<h4>{name} ({len(xs)})</h4><div class="wrap"><table class="sum">{head}'
                            + "".join(row(f) for f in xs) + "</table></div>") if xs else ""
    return ('<section><h2>On the radar: close to breaking out</h2><p class="hint">Tight bases closing within 3% under a '
            'flat top, not broken out yet. <b>Not a buy</b>: it becomes a setup only if it closes above the <b>trigger</b> on '
            'the volume shown (1.5x its 50-day average), then holds the next day. Set a price alert at the trigger. '
            'Canada first, ranked by how ready the base looks.</p>'
            + tbl(ca, "Canada") + tbl(us, "United States") + "</section>")


def write_html(path, asof, rows, res, data, ctx, n_scanned, review_html="", title="Breakout Scan", subtitle=None,
               radar=None, portfolio=""):
    asof_txt = pd.Timestamp(asof).strftime("%a %b %d, %Y")
    act = [r for r in rows if not r["Closed"] or r["StatusSince"] == asof]
    order = {"SELL": 0, "TAKE PROFIT": 1, "BUY": 2, "WATCH": 3, "EXTENDED": 4, "HOLD": 5, "TRAIL": 6, "DROP": 7}
    act.sort(key=lambda r: (order.get(r["Status"], 9), region(r["Ticker"]) != "CA", -_num(r.get("Score"), 0)))  # Canada first

    def charts_for(r):
        try:
            return chart_png(r["Ticker"], data[r["Ticker"]], r) if r["Ticker"] in data else None
        except Exception:
            return None

    do_now = [r for r in act if r["Status"] in ("BUY", "SELL", "TAKE PROFIT")
              or (r["Status"] == "DROP" and r["StatusSince"] == asof)]
    watch = [r for r in act if r["Status"] in ("WATCH", "EXTENDED")]
    holding = [r for r in act if r["Status"] in ("HOLD", "TRAIL")]
    cutoff = (pd.Timestamp(asof) - pd.Timedelta(days=21)).date().isoformat()
    closed = [r for r in rows if r["Closed"] and r["StatusSince"] < asof and r["StatusSince"] >= cutoff]
    closed.sort(key=lambda r: (r["StatusSince"], region(r["Ticker"]) == "CA"), reverse=True)

    counts = {}
    for r in act:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    chips = "".join(f'<span class="chip" style="--c:{STATUS_INFO[s][1]}">{counts[s]} {STATUS_INFO[s][0]}</span>'
                    for s in order if s in counts) or '<span class="chip">Nothing active</span>'
    mkt = "".join(f'<div class="mk"><span>{n}</span><b>{_f(c)}</b><em class="{"up" if ch >= 0 else "dn"}">{ch:+.2f}%</em>'
                  f'<small class="{tr.lower()}">{tr}</small></div>' for n, c, ch, tr in ctx)

    def section(title, sub, items, compact=False, empty="None today."):
        body = "".join(card_html(r, charts_for(r), compact) for r in items) if items else f'<p class="empty">{empty}</p>'
        return f'<section><h2>{title}</h2><p class="hint">{sub}</p>{body}</section>'

    def pnl_cell(r):
        v = r.get("PnLPct")
        if r["Status"] == "DROP" or v is None or pd.isna(v):
            return "<td>-</td>"
        return f'<td class="{"up" if v >= 0 else "dn"}">{v:+.1f}%</td>'

    closed_html = "".join(
        f'<tr><td><b>{r["Ticker"]}</b></td><td>{STATUS_INFO[r["Status"]][0]}</td><td>{r["StatusSince"]}</td>'
        f'{pnl_cell(r)}<td>{r["Note"]}</td></tr>' for r in closed)
    closed_sec = (f'<section><h2>Closed in the last 3 weeks</h2><div class="wrap"><table><tr><th>Ticker</th><th>Result</th>'
                  f'<th>Date</th><th>P&amp;L</th><th>Why</th></tr>{closed_html}</table></div></section>') if closed else ""

    def sum_row(r):
        label, color, _ = STATUS_INFO.get(r["Status"], (r["Status"], "#555", ""))
        above = (r["LastClose"] / r["Resistance"] - 1) * 100
        base = "-" if pd.isna(r.get("BaseLen")) else f"{int(r['BaseLen'])} days"
        pnl = r.get("PnLPct")
        pnl_td = "<td>-</td>" if pnl is None or pd.isna(pnl) else f'<td class="{"up" if pnl >= 0 else "dn"}">{pnl:+.1f}%</td>'
        g = r.get("Grade") if isinstance(r.get("Grade"), str) else ""
        sc = f'<span class="score g{g}">{_num(r.get("Score")):.0f}</span>' if g else "-"
        nw = int(_num(r.get("NWarn"), 0)) if g else "-"
        ed = r.get("EarningsDate") if isinstance(r.get("EarningsDate"), str) and r.get("EarningsDate") else "-"
        p = PROFILES.get(r["Ticker"]) or {}
        co = html_mod.escape(p.get("Name") or "-")
        ind = html_mod.escape(p.get("Industry") or p.get("Sector") or (r.get("Sector") if isinstance(r.get("Sector"), str) else "") or "-")
        return (f'<tr><td class="tk"><a href="#t-{r["Ticker"]}">{r["Ticker"]}</a></td>'
                f'<td class="cn">{co}</td><td class="cn">{ind}</td>'
                f'<td><span class="chip" style="--c:{color}">{label}</span></td><td>{sc}</td>'
                f'<td>{r["Pattern"]}</td><td>{r["Category"]}</td><td>{_f(r["LastClose"])}</td>'
                f'<td>{_f(r["Resistance"])}</td><td>{above:+.1f}%</td><td>{r["VolX"]}x</td><td>{base}</td>'
                f'<td>{_f(_num(r.get("Stop")))}</td><td>{_f(_num(r.get("Target")))}</td><td>{_f(_num(r.get("RR")), 1)}</td>'
                f'<td>{nw}</td>{pnl_td}<td>{ed}</td><td>{r["BreakoutDate"]}</td><td>{r["Indexes"]}</td></tr>')

    summary = ("<section><h2>Summary</h2><p class=\"hint\">Every setup being tracked. Tap a ticker for its card and chart; "
               "swipe the table sideways for more columns.</p><div class=\"wrap\"><table class=\"sum\"><tr>"
               "<th class=\"tk\">Ticker</th><th>Company</th><th>Industry</th><th>Action</th><th>Score</th><th>Pattern</th><th>Type</th><th>Close</th>"
               "<th>Breakout level</th><th>Above level</th><th>Volume</th><th>Base</th><th>Stop</th><th>Target</th>"
               "<th>R:R</th><th>Warnings</th><th>P&amp;L</th><th>Earnings</th><th>Breakout date</th><th>Index</th></tr>"
               + "".join(sum_row(r) for r in act) + "</table></div></section>") if act else ""

    traded = [r for r in rows if r["Closed"] and r["Status"] == "SELL" and np.isfinite(_num(r.get("EntryPrice")))
              and r.get("Source") != "manual"]            # the edge monitor counts scanner Buys only
    traded.sort(key=lambda r: r["StatusSince"])
    last20 = traded[-20:]
    edge_html = ""
    if last20:
        rs_ = []
        for r in last20:
            rps, ep, pnl = _num(r.get("RiskPerShare")), _num(r.get("EntryPrice")), _num(r.get("PnLPct"))
            if np.isfinite(rps) and rps > 0 and np.isfinite(pnl):
                rs_.append(pnl / 100 * ep / rps)
        wins = sum(1 for r in last20 if _num(r.get("PnLPct"), 0) > 0)
        streak = 0
        for r in reversed(traded):
            if _num(r.get("PnLPct"), 0) > 0:
                streak += 1
            else:
                break
        avg_r = f"{np.mean(rs_):+.2f}R" if rs_ else "n/a"
        edge_html = (f'<section><h2>Live track record</h2><p class="hint">Last {len(last20)} closed trades '
                     f'(Douglas: judge an edge over at least 20): <b>{wins}/{len(last20)} winners</b>, average {avg_r}. '
                     f'Backtest expectation for the Buy gate: about 50% winners, +0.27R average.'
                     + (" Sample still under 20 trades - too small to judge." if len(last20) < 20 else
                        " Below expectation - the edge may be fading; consider smaller size." if rs_ and np.mean(rs_) < 0
                        else " In line with or better than expectation.")
                     + (f" {streak} winners in a row: don't raise position size now (Murphy ch16, Gann rule 13)."
                        if streak >= 4 else "") + "</p></section>")
    cols = ["Ticker", "Score", "Grade", "Category", "Pattern", "Status", "Close", "Resistance", "PctAboveRes", "VolX",
            "FollowVolX", "RangeATR", "ClearATR", "UpperWickPct", "BaseLen", "BaseDepthPct", "Touches", "ResTests120d",
            "VolDryUp", "UpDownVol", "RS20", "RS63", "RSLineHigh", "Sector", "SectorRS", "RSI", "ExtVWAP20ATR",
            "NextRes", "SupplyZone", "Stop", "Target", "RR", "DollarVolM", "EarningsDate", "NWarn", "ScoreParts"]
    raw_tbl = (res[[c for c in cols if c in res.columns]].to_html(index=False, border=0, na_rep="-")
               if len(res) else "<p>No new setups today.</p>")
    rejected = res[res["Score"] < CFG["score_watch"]] if len(res) and "Score" in res.columns else res.iloc[0:0]
    rej_html = "".join(f'<li><b>{x.Ticker}</b> {x.Score:.0f}/100 &middot; {x.Pattern} &middot; '
                       f'{(x.Warnings or "no warnings")}</li>' for x in rejected.itertuples()) if len(rejected) else ""
    rej_sec = (f'<details><summary>Rejected by the scorecard ({len(rejected)}): passed the breakout rules '
               f'but scored under {CFG["score_watch"]}</summary><ul class="hint">{rej_html}</ul></details>') if rej_html else ""
    regime = ""
    for name, m in MARKET:
        br = f"{m['Breadth'] * 100:.0f}% of stocks above their 50-day" if np.isfinite(m["Breadth"]) else "breadth n/a"
        cls = "dn" if m["Pressure"] else "up" if m["Trend"] == "Uptrend" else ""
        regime += (f'<div class="rg"><b>{name}</b> <span class="{cls}">{m["Trend"]}</span> &middot; {br} &middot; '
                   f'{m["DistDays"]} distribution days (25 sessions){" &middot; <b class=dn>under pressure</b>" if m["Pressure"] else ""}</div>')
    glossary = "".join(f'<li><span class="chip" style="--c:{c}">{lab}</span> {txt}</li>'
                       for lab, c, txt in STATUS_INFO.values())

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Breakout Scan {asof}</title>
<style>
:root{{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e6e6e3;--up:#16794a;--dn:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--card:#1f1f22;--ink:#ececee;--muted:#9a9aa0;--line:#2e2e33;--up:#4cc38a;--dn:#ff6b5e}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}
h1{{font-size:22px;margin:4px 0 2px}} h2{{font-size:17px;margin:26px 0 2px}} h3{{font-size:20px;margin:0}}
.hint,.sub,.empty{{color:var(--muted);font-size:13px;margin:2px 0 10px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}}
.chip{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;color:#fff;background:var(--c,#555)}}
.mkts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:10px 0}}
.mk{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:grid;grid-template-columns:1fr auto;font-size:13px}}
.mk span{{color:var(--muted);grid-column:1/3}} .mk em{{font-style:normal;text-align:right}} .mk small{{grid-column:1/3;color:var(--muted)}}
.mk small.uptrend{{color:var(--up)}} .mk small.downtrend{{color:var(--dn)}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}}
.card{{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:12px;padding:14px;margin:10px 0}}
.card header{{display:flex;justify-content:space-between;gap:10px}} .right{{text-align:right}}
.badge{{background:var(--c);color:#fff;font-weight:700;font-size:13px;padding:4px 10px;border-radius:8px;white-space:nowrap}}
.px{{font-size:18px;font-weight:600;margin:6px 0 0}}
.do{{margin:10px 0;font-size:15px}}
.co{{background:var(--bg);border-radius:8px;padding:8px 10px;margin:10px 0 0;font-size:13px}}
.co p{{margin:0}} .coname span{{color:var(--muted);font-size:12px;display:block}} .codesc{{margin-top:4px!important;line-height:1.45}}
.co details{{margin-top:4px}} .co a{{color:var(--ink)}}
td.cn{{white-space:normal;min-width:150px;max-width:220px}}
.facts{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;font-size:13px}}
.facts div{{background:var(--bg);border-radius:8px;padding:6px 8px}} .facts span{{display:block;color:var(--muted);font-size:11px}}
.flags{{margin-top:8px}} .flag{{display:inline-block;font-size:12px;color:#a15c00;background:#a15c0018;border-radius:6px;padding:2px 8px;margin:2px 4px 0 0}}
details{{margin-top:10px}} summary{{cursor:pointer;color:var(--muted);font-size:13px}} details img{{width:100%;border-radius:8px;margin-top:8px}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
table.sum td,table.sum th{{font-size:13px;padding:8px}} table.sum .chip{{font-size:11px;padding:2px 8px}}
.tk{{position:sticky;left:0;background:var(--card);z-index:1}} .tk a{{color:var(--ink);font-weight:700}} table{{border-collapse:collapse;font-size:12px;width:100%;background:var(--card)}}
th,td{{padding:6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}} th{{color:var(--muted);font-weight:600}}
ul.gl{{list-style:none;padding:0;font-size:13px}} ul.gl li{{margin:8px 0}}
footer{{color:var(--muted);font-size:12px;margin:30px 0 10px}}
/*EXTRA_CSS*/
.score{{display:inline-block;font-size:13px;font-weight:700;padding:1px 7px;border-radius:6px;vertical-align:middle;background:#8884;color:var(--ink)}}
.score small{{font-weight:400;opacity:.7}} .score.gA{{background:#16794a;color:#fff}} .score.gB{{background:#1d5fbf;color:#fff}} .score.gC{{background:#6b6b6b;color:#fff}}
.sbs{{margin:8px 0}} .sb{{display:grid;grid-template-columns:80px 1fr 44px;gap:8px;align-items:center;font-size:12px;margin:3px 0}}
.sb i{{display:block;height:8px;background:var(--line);border-radius:4px;overflow:hidden}} .sb i b{{display:block;height:100%;background:var(--c)}}
.sb em{{font-style:normal;color:var(--muted);text-align:right}}
ul.ck{{list-style:none;padding:0;margin:6px 0;font-size:13px}} ul.ck li{{margin:3px 0}} ul.ck li.grp{{color:var(--muted);font-size:11px;text-transform:uppercase;margin-top:8px}}
ul.ck b{{display:inline-block;width:20px}} .ok{{color:var(--up)}} .no{{color:var(--dn)}} .na{{color:var(--muted)}}
.wl{{font-size:12px;color:var(--muted);margin-right:4px}}
.rg{{font-size:13px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0}}
.links{{font-size:13px}} a{{color:#1d5fbf}}
</style></head><body><main>
<h1>{title}</h1>
<p class="hint">{subtitle or f"Based on the close of <b>{asof_txt}</b>"} &middot; {n_scanned:,} stocks scanned &middot; {len(res)} new setups</p>
<!--RUN-->
<!--MORNING--><!--/MORNING-->
<!--REVIEW-->
<p class="links"><a href="index.html">Latest scan</a> &middot; <a href="live.html">Live preview</a> &middot; <a href="morning.html">Morning check</a> &middot; <a href="backtest.html">Backtest</a> &middot; <a href="archive.html">Past scans</a></p>
{portfolio}
<h2>Setups</h2>
<div class="chips">{chips}</div>
{summary}
{section("Act at tomorrow's open", "New buys, profit-taking and exits.", do_now, empty="No action needed tomorrow.")}
{section("Watchlist", "Broke out but not confirmed yet, or too far above the breakout level to buy.", watch, empty="Nothing on watch.")}
{radar_html(radar) if radar is not None else ""}
{section("Open positions", "Setups that triggered a Buy and are still running.", holding, compact=True, empty="No open positions.")}
{closed_sec}
<section><h2>What the labels mean</h2><ul class="gl">{glossary}</ul>
<p class="hint">Entry, stop and target are calculated from the breakout level. Profit/loss is measured from the close on the day of the Buy signal; your real entry is the next open.
A Buy is exited early if it closes back below the breakout level within 10 sessions (failed breakout).</p>
<p class="hint">The score is a pass mark, not a ranking: in the 2022-26 backtest, setups scoring 70-74 did as well as or better than those scoring 75+.
Position size risks {ACCOUNT['risk_pct'] * 100:.0f}% of the account per trade, so a run of 20 losses (Douglas's sample size) would cost about {(1 - (1 - ACCOUNT['risk_pct']) ** 20) * 100:.0f}%.</p></section>
{edge_html}
{rej_sec}
<details><summary>Full scan details ({len(res)} setups)</summary><div class="wrap">{raw_tbl}</div></details>
<details><summary>Scan diagnostics</summary><ul class="hint">{''.join(f"<li>{x}</li>" for x in DIAG)}</ul></details>
<footer>Technical screen only - not investment advice. Scores and thresholds are rules of thumb until the backtest supports them.</footer>
</main></body></html>"""
    html = html.replace("/*EXTRA_CSS*/", REVIEW_CSS + RUN_CSS).replace("<!--RUN-->", RUN_HTML).replace("<!--REVIEW-->", review_html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

# --------------------------------------------------------------------------
# Morning check: intraday VWAP, time-adjusted volume, buy-zone verdict
# --------------------------------------------------------------------------
PAGE_CSS = """
:root{--bg:#f6f6f4;--card:#fff;--ink:#1c1c1e;--muted:#6b6b70;--line:#e6e6e3;--up:#16794a;--dn:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#151517;--card:#1f1f22;--ink:#ececee;--muted:#9a9aa0;--line:#2e2e33;--up:#4cc38a;--dn:#ff6b5e}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px} h1{font-size:22px;margin:4px 0} h2{font-size:17px;margin:24px 0 6px}
.hint{color:var(--muted);font-size:13px} a{color:#1d5fbf}
.card{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:12px;padding:12px 14px;margin:10px 0}
.card header{display:flex;justify-content:space-between;gap:8px;align-items:center} .card h3{margin:0;font-size:19px}
.badge{background:var(--c);color:#fff;font-weight:700;font-size:13px;padding:4px 10px;border-radius:8px}
.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;font-size:13px;margin-top:8px}
.facts div{background:var(--bg);border-radius:8px;padding:6px 8px} .facts span{display:block;color:var(--muted);font-size:11px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px} table{border-collapse:collapse;width:100%;font-size:13px;background:var(--card)}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap} th{color:var(--muted)}
.up{color:var(--up)} .dn{color:var(--dn)}
"""


def _intraday(tickers):
    import yfinance as yf
    out = {}
    if not tickers:
        return out
    df = yf.download(tickers, period="30d", interval="5m", group_by="ticker", auto_adjust=False,
                     prepost=False, threads=True, progress=False)
    for t in tickers:
        try:
            d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
            d = d.dropna(how="all")
            if len(d):
                idx = d.index.tz_convert("America/New_York") if d.index.tz is not None else d.index.tz_localize("America/New_York")
                out[t] = d.set_axis(idx)
        except Exception:
            pass
    return out


def intraday_stats(d):
    """VWAP, its slope, time-of-day relative volume and the opening range for the latest session."""
    days = sorted(set(d.index.date))
    today = days[-1]
    td = d[d.index.date == today]
    if len(td) < 2:
        return None
    tp = (td["High"] + td["Low"] + td["Close"]) / 3
    vwap = (tp * td["Volume"]).cumsum() / td["Volume"].cumsum().replace(0, np.nan)
    now = td.index[-1]
    tod = now.time()
    cum_today = td["Volume"].sum()
    prior = []
    for day in days[-11:-1]:
        pdd = d[(d.index.date == day) & (np.array([x.time() <= tod for x in d.index]))]
        if len(pdd):
            prior.append(pdd["Volume"].sum())
    trvol = cum_today / np.mean(prior) if prior and np.mean(prior) > 0 else np.nan
    first = td[td.index < td.index[0] + pd.Timedelta(minutes=30)]
    prev = d[d.index.date == days[-2]] if len(days) > 1 else None
    return dict(
        time=now.strftime("%H:%M ET"), price=float(td["Close"].iloc[-1]), open=float(td["Open"].iloc[0]),
        prev_close=float(prev["Close"].iloc[-1]) if prev is not None and len(prev) else np.nan,
        vwap=float(vwap.iloc[-1]), vwap_slope=float(vwap.iloc[-1] - vwap.iloc[max(0, len(vwap) - 7)]),
        trvol=float(trvol) if np.isfinite(trvol) else np.nan,
        or_high=float(first["High"].max()), or_low=float(first["Low"].min()),
        low=float(td["Low"].min()), high=float(td["High"].max()),
    )


def morning_verdict(r, x):
    R = float(r["Resistance"])
    top = R * (1 + TRADE["buy_zone"])
    p, v = x["price"], x["vwap"]
    tv = x["trvol"]
    tv_txt = f"{tv:.1f}x normal for this time" if np.isfinite(tv) else "volume n/a"
    st = r["Status"]
    if st in ("HOLD", "TRAIL", "TAKE PROFIT"):
        stop = _num(r.get("Stop"))
        if np.isfinite(stop) and x["low"] <= stop:
            return "STOPPED", "#b42318", f"Traded down to the stop ({_f(stop)}) this morning - your stop order should have filled."
        dist = (p / stop - 1) * 100 if np.isfinite(stop) else np.nan
        return "OK", "#3d4a5c", f"{dist:.1f}% above the stop ({_f(stop)}). {'Above' if p >= v else 'Below'} VWAP, {tv_txt}."
    if st == "WATCH":
        if p < R * (1 - CFG["hold_tol"]) or x["low"] < R * (1 - CFG["hold_tol"]):
            return "FAILING", "#b42318", f"Has traded below {_f(R * (1 - CFG['hold_tol']))} - the breakout is failing; likely a Drop tonight."
        return "HOLDING", "#1d5fbf", f"Holding above the breakout level so far ({'above' if p >= v else 'below'} VWAP, {tv_txt}). Decision comes at the close."
    # BUY and EXTENDED: is there a valid entry right now?
    if p < R * (1 - CFG["hold_tol"]):
        return "SKIP", "#b42318", f"Lost the breakout level ({_f(R)}). Don't enter."
    if x["open"] > top and p > top:
        return "SKIP", "#a15c00", f"Gapped above the buy zone (opened {_f(x['open'])}, zone tops at {_f(top)}). Don't chase."
    if p > top:
        return "WAIT", "#a15c00", f"Trading above the buy zone ({_f(top)}). Wait for a pullback into {_f(R)}-{_f(top)}."
    if p < v:
        return "WAIT", "#a15c00", f"Below VWAP ({_f(v)}). Enter only if it reclaims VWAP and holds."
    if x["vwap_slope"] < 0:
        return "WAIT", "#a15c00", f"Above VWAP ({_f(v)}) but VWAP is still falling. Wait for it to flatten or turn up."
    if np.isfinite(tv) and tv < 1.0:
        return "WAIT", "#a15c00", f"Light volume so far ({tv_txt}). Breakouts without volume often fail."
    if st == "EXTENDED":
        return "ENTER", "#16794a", f"Pulled back into the buy zone and is above a rising VWAP on {tv_txt}. Retest entry is valid."
    return "ENTER", "#16794a", f"Inside the buy zone, above a rising VWAP ({_f(v)}), {tv_txt}. Entry is valid; stop {_f(_num(r.get('Stop')))}."


def morning_check(outdir, force=False):
    now = dt.datetime.now(ET)
    force = force or os.environ.get("FORCE_MORNING") == "1"
    if not force and (now.weekday() >= 5 or not (dt.time(9, 50) <= now.time() <= dt.time(12, 45))):
        print(f"Morning check skipped: it is {now:%a %H:%M} ET (runs 9:50 am-12:45 pm ET on weekdays).")
        return 0
    rows = [r for r in load_tracker(os.path.join(outdir, "tracker.csv"))
            if not r["Closed"] and r["Status"] in ("BUY", "EXTENDED", "WATCH", "HOLD", "TRAIL", "TAKE PROFIT")]
    demo = False
    if not rows and force:
        scans = sorted(f for f in os.listdir(outdir) if f.startswith("breakouts_") and f.endswith(".csv"))
        if scans:
            sc = pd.read_csv(os.path.join(outdir, scans[-1]))
            rows = [dict(Ticker=x.Ticker, Status="WATCH", Resistance=x.Resistance, Stop=x.Stop, Closed=False)
                    for x in sc.head(8).itertuples()]
            demo = True
    try:
        intr = _intraday(sorted({r["Ticker"] for r in rows}))
    except Exception as e:
        print("Intraday download failed:", e)
        intr = {}
    PROFILES.update(_load_profiles(os.path.join(outdir, "sectors.csv")))   # company names from last night's scan
    cards = []
    order = {"ENTER": 0, "STOPPED": 1, "WAIT": 2, "SKIP": 3, "FAILING": 4, "HOLDING": 5, "OK": 6}
    items = []
    for r in rows:
        d = intr.get(r["Ticker"])
        try:
            x = intraday_stats(d) if d is not None else None
        except Exception:
            x = None
        if x is None:
            items.append((9, r, None, ("NO DATA", "#6b6b6b", "No intraday data yet.")))
            continue
        v = morning_verdict(r, x)
        items.append((order.get(v[0], 8), r, x, v))
    items.sort(key=lambda z: z[0])
    for _, r, x, (verdict, color, text) in items:
        facts = ""
        if x:
            gap = (x["open"] / x["prev_close"] - 1) * 100 if np.isfinite(x["prev_close"]) else np.nan
            facts = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in [
                ("Price", _f(x["price"])), ("VWAP", f'{_f(x["vwap"])} {"&#8599;" if x["vwap_slope"] >= 0 else "&#8600;"}'),
                ("Volume vs normal", f'{x["trvol"]:.1f}x' if np.isfinite(x["trvol"]) else "-"),
                ("Breakout level", _f(r["Resistance"])), ("Opening range", f'{_f(x["or_low"])}-{_f(x["or_high"])}'),
                ("Gap at open", f"{gap:+.1f}%" if np.isfinite(gap) else "-")])
        label = STATUS_INFO.get(r["Status"], (r["Status"],))[0]
        cards.append(f'<article class="card" style="--c:{color}"><header><h3>{r["Ticker"]} '
                     f'<small class="hint">{label} from last night</small></h3><span class="badge">{verdict}</span></header>'
                     f'{company_html(r["Ticker"], full=False)}<p>{text}</p><div class="facts">{facts}</div></article>')
    stamp = items[0][2]["time"] if items and items[0][2] else now.strftime("%H:%M ET")
    last = _last_full_scan(outdir)
    last_txt = f"{last:%a %b %d}" if last else "not available"
    brief = live_brief_html(outdir, now)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Morning check {now:%Y-%m-%d}</title>
<style>{PAGE_CSS}{RUN_CSS}</style></head><body><main>
<h1>Morning check</h1>{RUN_HTML}
<p class="hint">{now:%a %b %d, %Y} &middot; prices as of {stamp} &middot; last full scan: close of {last_txt}
&middot; <a href="index.html">Evening report</a> &middot; <a href="live.html">Live report</a></p>
{('<section class="card" style="--c:#1d5fbf"><header><h3>Today\'s market</h3></header>' + brief + '</section>') if brief else
 '<p class="hint">Today\'s full live scan has not run yet (it needs the market to be open).</p>'}
<p class="hint">Checks last night's Buy, Don't-chase, Watch and open positions against today's session: price vs the
day's VWAP and its direction, volume compared with the same time on the last 10 days, and the buy zone.
US quotes are near real-time; TSX quotes can be delayed about 15 minutes. Re-check your broker before acting.</p>
{'<p class="hint"><b>Test run:</b> nothing is being tracked, so this checks the latest scan candidates instead.</p>' if demo else ''}
{''.join(cards) or '<p class="hint">Nothing to check this morning.</p>'}
<p class="hint">Technical screen only - not investment advice.</p>
</main></body></html>"""
    with open(os.path.join(outdir, "morning.html"), "w", encoding="utf-8") as f:
        f.write(html)
    if not demo or brief:
        inject_morning_summary(outdir, items if not demo else [], stamp, now)
    print(f"Morning check written for {len(items)} names")
    return 0


BRIEF_INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^RUT", "Russell 2000"),
                 ("^GSPTSE", "S&P/TSX")]


def write_live_brief(outdir, data, tickers, ctx, n_found, n_radar, frac, raw=None):
    """Morning fix: a small JSON snapshot of today's market from the full live scan, read by the morning check."""
    import json
    now = dt.datetime.now(ET)
    today = now.date()
    errors = []

    def chg_of(d, t):
        if d is None:
            return None
        c = d["Close"].dropna()
        if len(c) < 2 or pd.Timestamp(c.index[-1]).date() != today:
            return None
        c0, c1 = float(c.iloc[-2]), float(c.iloc[-1])
        return dict(t=t, last=round(c1, 2), chg=round((c1 / c0 - 1) * 100, 2)) if c0 > 0 else None

    def day_chg(t):
        return chg_of(data.get(t), t)
    # indexes, sectors and VIX from their own small download (prices only), so a missing volume figure
    # in today's unfinished bar can't hide them
    quick = {}
    try:
        quick = download(sorted({t for t, _ in BRIEF_INDEXES} | set(ALL_SECTOR_ETFS) | {"^VIX"}), period="5d")
    except Exception as e:
        errors.append(f"quick download: {str(e)[:80]}")
    q_chg = lambda t: chg_of(quick.get(t), t) or day_chg(t)
    idx = [dict(name=n, **x) for t, n in BRIEF_INDEXES for x in [q_chg(t)] if x]
    sec = [dict(name=SECTOR_NAMES.get(t, t) + (" (TSX)" if region(t) == "CA" else ""), region=region(t), **x)
           for t in ALL_SECTOR_ETFS for x in [q_chg(t)] if x]
    movers = []
    cover = {"US": [0, 0], "CA": [0, 0]}
    for t in tickers:
        d = data.get(t)
        if d is None:
            continue
        cover[region(t)][1] += 1
        x = day_chg(t)
        if not x:
            continue
        cover[region(t)][0] += 1
        if len(d) < 51 or x["last"] < CFG["min_price"] or abs(x["chg"]) > 60:   # penny stocks and data glitches
            continue
        min_dv = CFG["min_dollar_vol_ca"] if region(t) == "CA" else CFG["min_dollar_vol"]
        if float((d["Close"] * d["Volume"]).iloc[-51:-1].mean()) < min_dv:
            continue
        movers.append(dict(region=region(t), name=PROFILES.get(t, {}).get("Name", ""), **x))
    raw_today = sum(1 for t in tickers if raw is not None and t in raw and len(raw[t])
                    and pd.Timestamp(raw[t].index[-1]).date() == today)
    movers.sort(key=lambda m: m["chg"])
    us = [t for t in tickers if region(t) == "US"]
    ca = [t for t in tickers if region(t) == "CA"]
    br_us, br_ca = breadth_today(data, us), breadth_today(data, ca)
    regime = ""
    try:
        spy = data["SPY"]
        st = market_state(spy, ctx["breadth"]["US"], spy.index[-1], ctx["internals"]["US"], ctx["credit"], ctx["banks"]["US"])
        vix = float(data["^VIX"]["Close"].iloc[-1]) if "^VIX" in data else np.nan
        regime = market_regime(st, br_us, vix)[0]
    except Exception as e:
        errors.append(f"regime: {type(e).__name__} {str(e)[:80]}")
    vx = q_chg("^VIX")

    def clean(v):
        return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else v
    brief = dict(date=today.isoformat(), time=now.strftime("%H:%M"), session_pct=round(frac * 100),
                 indexes=idx, sectors=sorted(sec, key=lambda s: -s["chg"]),
                 gainers=movers[::-1][:5], losers=movers[:5], regime=regime,
                 vix=vx, breadth_us=dict(adv=br_us["adv"], dec=br_us["dec"], a50=clean(br_us["a50"])),
                 breadth_ca=dict(adv=br_ca["adv"], dec=br_ca["dec"], a50=clean(br_ca["a50"])),
                 breakouts=n_found, radar=n_radar,
                 coverage=dict(us=cover["US"], ca=cover["CA"], raw_today=raw_today,
                               prep_fail=LIVE.get("prep_fail", [0, ""])), errors=errors)
    with open(os.path.join(outdir, "live_brief.json"), "w") as f:
        json.dump(brief, f, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    return brief


def _last_full_scan(outdir):
    scans = sorted(f for f in os.listdir(outdir) if f.startswith("breakouts_") and f.endswith(".html"))
    if not scans:
        return None
    try:
        return dt.date.fromisoformat(scans[-1][len("breakouts_"):-len(".html")])
    except ValueError:
        return None


def live_brief_html(outdir, now):
    """'Market right now' strip for the morning check; empty if today's live scan hasn't run."""
    import json
    try:
        b = json.load(open(os.path.join(outdir, "live_brief.json")))
    except Exception:
        return ""
    if b.get("date") != now.date().isoformat():
        return ""

    def pc(x):
        c = x["chg"]
        return f'<b style="color:{"#16794a" if c >= 0 else "#b42318"}">{c:+.1f}%</b>'
    idx = " &middot; ".join(f'{x["name"]} {pc(x)}' for x in b["indexes"]) or "index data not available"
    secs = b.get("sectors") or []
    sec_txt = ""
    if secs:
        sec_txt = (f'<br>Leading sectors: {", ".join(s["name"] + " " + pc(s) for s in secs[:3])}; '
                   f'lagging: {", ".join(s["name"] + " " + pc(s) for s in secs[-3:][::-1])}')
    mv = lambda lst: ", ".join(f'{m["t"]} {pc(m)}' for m in lst)
    mov = (f'<br>Biggest gainers: {mv(b["gainers"])}<br>Biggest decliners: {mv(b["losers"])}'
           if b.get("gainers") else "")
    bu, bc = b.get("breadth_us") or {}, b.get("breadth_ca") or {}
    brd = (f'<br>Breadth: US {bu.get("adv", 0)} up / {bu.get("dec", 0)} down, TSX {bc.get("adv", 0)} up / {bc.get("dec", 0)} down'
           + (f'; VIX {b["vix"]["last"]:.1f} ({b["vix"]["chg"]:+.1f}%)' if b.get("vix") else "")
           + (f'. Regime: <b>{b["regime"]}</b>' if b.get("regime") else ""))
    cov = b.get("coverage") or {}
    if cov.get("us") and cov.get("ca"):
        brd += (f' <span style="color:var(--muted)">(today\'s prices in for {cov["us"][0]:,} of {cov["us"][1]:,} US and '
                f'{cov["ca"][0]} of {cov["ca"][1]} TSX stocks)</span>')
    return (f'<p style="margin:6px 0 0;font-size:14px"><b>Market right now ({b["time"]} ET):</b> {idx}{sec_txt}{mov}{brd}.'
            f'<br>Full live scan of all six indexes: {b["breakouts"]} breakouts forming, {b["radar"]} on the radar '
            f'(<a href="live.html">live report</a>, volume projected from {b["session_pct"]}% of a normal day).</p>')


def inject_morning_summary(outdir, items, stamp, now):
    """Show this morning's verdicts at the top of the latest evening report."""
    import re
    scans = sorted(f for f in os.listdir(outdir) if f.startswith("breakouts_") and f.endswith(".html"))
    if not scans:
        return
    if items:
        lines = "".join(
            f'<li><b>{r["Ticker"]}</b> <span style="background:{color};color:#fff;font-size:11px;font-weight:700;'
            f'padding:2px 7px;border-radius:6px">{verdict}</span> {text}</li>'
            for _, r, x, (verdict, color, text) in items)
        body = f'<ul style="list-style:none;padding:0;margin:6px 0 0;font-size:14px">{lines}</ul>'
    else:
        body = '<p style="margin:6px 0 0">Nothing from last night to check: no Buy, Watch or open positions.</p>'
    block = (f'<!--MORNING--><section style="background:var(--card);border:1px solid var(--line);'
             f'border-left:6px solid #1d5fbf;border-radius:12px;padding:12px 14px;margin:10px 0">'
             f'<span style="display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em">'
             f'Morning update &middot; {now:%a %b %d} &middot; {stamp}</span>{live_brief_html(outdir, now)}{body}'
             f'<p style="margin:8px 0 0;font-size:13px"><a href="morning.html">Full morning check</a> &middot; '
             f'<a href="live.html">Live report</a> &middot; the rest of this page is the full scan from the close of '
             f'{_last_full_scan(outdir) or dt.date.today():%a %b %d}; the next one runs after today\'s close.</p>'
             f'</section><!--/MORNING-->')
    for name in (scans[-1], "live.html"):
        path = os.path.join(outdir, name)
        if not os.path.exists(path):
            continue
        page = open(path, encoding="utf-8").read()
        if "<!--MORNING-->" in page:
            page = re.sub(r"<!--MORNING-->.*?<!--/MORNING-->", lambda m: block, page, flags=re.S)
        elif '<section class="review">' in page:
            page = page.replace('<section class="review">', block + '\n<section class="review">', 1)
        else:
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)

# --------------------------------------------------------------------------
# Backtest: do these rules make money after costs?
# --------------------------------------------------------------------------
EXIT_VARIANTS = {
    # A: the rules before the books - half at target, trail the rest by 2 ATR, hard stop, 15-day time stop
    "A": dict(name="Before: half at target, trail rest", scale=[(0.5, "target")], fail=False, be=False, trail="target"),
    # B: + failed-breakout exit [Murphy ch5; Hougaard] + breakeven after +1R [Gann rule 3; Douglas ch11]
    "B": dict(name="+ failed-breakout exit + breakeven at +1R", scale=[(0.5, "target")], fail=True, be=True,
              trail="target"),
    # C: Douglas's thirds [Trading in the Zone ch11] - 1/3 at +1R, 1/3 at target (stop to entry), trail the last third
    "C": dict(name="Thirds: 1/3 at +1R, 1/3 at target, trail 1/3", scale=[(1 / 3, "1R"), (1 / 3, "target")],
              fail=True, be=True, trail="target"),
    # D: Hougaard - no profit target, trail everything once +1R is reached
    "D": dict(name="No target: trail all after +1R", scale=[], fail=True, be=True, trail="1R"),
    # E/F split B to see which of its two rules helps
    "E": dict(name="A + failed-breakout exit only", scale=[(0.5, "target")], fail=True, be=False, trail="target"),
    "F": dict(name="A + breakeven at +1R only", scale=[(0.5, "target")], fail=False, be=True, trail="target"),
}


def simulate_trade(A, b, r, plan_entry_day, variant="A"):
    """Enter at the open after the hold day; hard stop (filled at the open if it gaps through); exits per variant."""
    V_ = EXIT_VARIANTS[variant]
    O, H, L, C = A["Open"], A["High"], A["Low"], A["Close"]
    n = len(C)
    j0 = plan_entry_day
    if j0 >= n:
        return None
    R = r["Resistance"]
    entry = O[j0]
    if entry < R or entry > R * (1 + TRADE["buy_zone"]):
        return dict(Outcome="no fill (opened outside buy zone)")
    stop, target, rr, _ = trade_plan(R, r["BaseLow"], r["ATR"], entry, _num(r.get("NextRes")))
    if entry <= stop:
        return dict(Outcome="no fill (opened below stop)")
    slip = CFG["slippage"]
    e = entry * (1 + slip)
    risk = e - stop
    one_r = entry + risk
    pending = [list(x) for x in V_["scale"]]
    pos, pnl, high_c, target_hit = 1.0, 0.0, entry, False
    exit_day, reason = None, ""
    for j in range(j0, min(n, j0 + 60)):
        fill_open = O[j] if j > j0 else entry
        if L[j] <= stop:
            px = min(fill_open, stop)
            pnl += pos * (px * (1 - slip) - e)
            exit_day, reason, pos = j, ("trail/breakeven stop" if stop >= entry else "stop"), 0
            break
        for item in pending:
            frac, lvl = item
            if frac <= 0:
                continue
            price = one_r if lvl == "1R" else target
            if H[j] >= price:
                pnl += frac * (max(fill_open, price) * (1 - slip) - e)
                pos -= frac
                item[0] = 0
                if lvl == "target":
                    target_hit = True
                    stop = max(stop, entry)
        if pos <= 1e-9:
            exit_day, reason = j, "all targets"
            break
        high_c = max(high_c, C[j])
        if V_["be"] and C[j] >= one_r:
            stop = max(stop, entry)
        if (V_["trail"] == "target" and target_hit) or (V_["trail"] == "1R" and high_c >= one_r):
            stop = max(stop, high_c - TRADE["trail_atr"] * r["ATR"])
        held = j - j0 + 1
        nxt = O[j + 1] if j + 1 < n else C[j]
        if V_["fail"] and held <= 10 and C[j] < R * (1 - CFG["hold_tol"]):
            pnl += pos * (nxt * (1 - slip) - e)
            exit_day, reason, pos = j + 1, "failed breakout", 0
            break
        if not target_hit and held >= TRADE["time_stop_days"] and C[j] < entry * (1 + TRADE["time_stop_gain"]):
            pnl += pos * (nxt * (1 - slip) - e)
            exit_day, reason, pos = j + 1, "time stop", 0
            break
    if pos > 0:
        j = min(n - 1, j0 + 59)
        pnl += pos * (C[j] * (1 - slip) - e)
        exit_day, reason = j, ("max hold" if j == j0 + 59 else "open at end of data")
    return dict(Outcome="trade", Entry=round(entry, 2), Stop0=round(e - risk, 2), Target=target,
                RRPlan=rr, R=round(pnl / risk, 3), PnLPct=round(pnl / e * 100, 2), Days=exit_day - j0 + 1,
                Exit=reason, HitTarget=target_hit)


def _stats(df):
    t = df[df["Outcome"] == "trade"]
    if not len(t):
        return dict(Trades=0)
    wins, losses = t[t["R"] > 0], t[t["R"] <= 0]
    pf = wins["R"].sum() / -losses["R"].sum() if len(losses) and losses["R"].sum() < 0 else np.inf
    return dict(Trades=len(t), WinRate=round(len(wins) / len(t) * 100, 1), AvgR=round(t["R"].mean(), 3),
                AvgWinR=round(wins["R"].mean(), 2) if len(wins) else 0, AvgLossR=round(losses["R"].mean(), 2) if len(losses) else 0,
                ProfitFactor=round(pf, 2) if np.isfinite(pf) else "inf", TotalR=round(t["R"].sum(), 1),
                AvgPnLPct=round(t["PnLPct"].mean(), 2), AvgDays=round(t["Days"].mean(), 1),
                TargetHit=round(t["HitTarget"].mean() * 100, 1))


def run_backtest(a):
    t0 = time.time()
    print("Backtest: loading universe...")
    membership = build_universe([k.strip() for k in a.bt_indexes.split(",") if k.strip()])
    tickers = sorted(membership)
    raw = download(sorted(set(tickers) | set(BENCH.values()) | set(STRESS_TICKERS) | {"XFN.TO"}),
                   period=f"{a.bt_years}y")
    data = {}
    for t, d in raw.items():
        try:
            data[t] = prep(d)
        except Exception:
            pass
    ctx = build_context(data, tickers)
    ctx["sector_data"] = {}            # no point-in-time sector membership history: scored neutral
    sigs = []
    for k, t in enumerate(tickers):
        d = data.get(t)
        if d is None or len(d) < 260:
            continue
        A = arrays(d)
        V, AV = A["Volume"], A["AvgVol50"]
        with np.errstate(divide="ignore", invalid="ignore"):
            cand = np.where(V / AV >= CFG["min_vol_ratio"])[0]
        next_ok = 0
        for b in cand:
            if b < 215 or b < next_ok or b + 2 >= len(d):
                continue
            r = check_breakout(d, b, 0, A)
            if not r:
                continue
            r["Ticker"] = t
            enrich(r, d, A, ctx)
            R = r["Resistance"]
            held = A["Close"][b + 1] >= R and A["Low"][b + 1] >= R * (1 - CFG["hold_tol"])
            ext = A["Close"][b + 1] > R * (1 + TRADE["extended"])
            score_h = r["Score"] + (2 if held else 0)
            row = dict(Ticker=t, Date=pd.Timestamp(d.index[b]).date().isoformat(), Category=r["Category"],
                       Pattern=r["Pattern"], Score=round(score_h, 1), RRSignal=r["RR"], NWarn=r["NWarn"],
                       VolX=r["VolX"], Held=held, Extended=bool(held and ext), Region=region(t),
                       WeeklyOK=r["WeeklyOK"], New20=r["New20dHigh"], RSIDiv=r["RSIDiv"], OBVDiv=r["OBVDiv"],
                       Squeeze=r["Squeeze"], AboveCloud=r["AboveCloud"], ADXTurnUp=r["ADXTurnUp"],
                       Warnings=r["Warnings"], Year=pd.Timestamp(d.index[b]).year,
                       Gate=bool(score_h >= CFG["score_buy"] and r["RR"] >= CFG["min_rr"] and r["NWarn"] < CFG["max_warnings"]))
            if held and not ext:
                tr = simulate_trade(A, b, r, b + 2, "A")
                row.update(tr or dict(Outcome="no data"))
                for vk in EXIT_VARIANTS:
                    tv = simulate_trade(A, b, r, b + 2, vk) or {}
                    row[f"R_{vk}"] = tv.get("R", np.nan)
                    row[f"Days_{vk}"] = tv.get("Days", np.nan)
                    row[f"Exit_{vk}"] = tv.get("Exit", "")
                next_ok = b + 2 + int(row.get("Days", 5) or 5)
            else:
                row["Outcome"] = "extended - no entry" if held else "failed hold"
                next_ok = b + 5
            sigs.append(row)
        if k % 50 == 0:
            print(f"\r  backtested {k}/{len(tickers)} tickers, {len(sigs)} signals", end="", flush=True)
    print()
    df = pd.DataFrame(sigs)
    os.makedirs(a.outdir, exist_ok=True)
    df.to_csv(os.path.join(a.outdir, "backtest_trades.csv"), index=False)
    write_backtest_html(os.path.join(a.outdir, "backtest.html"), df, a, len(data), time.time() - t0)
    print(f"Backtest: {len(df)} signals, {int((df['Outcome'] == 'trade').sum()) if len(df) else 0} trades "
          f"in {time.time() - t0:.0f}s")
    if len(df):
        print(pd.DataFrame([dict(Group="All trades", **_stats(df)),
                            dict(Group="Gate pass (Buy)", **_stats(df[df["Gate"]])),
                            dict(Group="Gate fail", **_stats(df[~df["Gate"]]))]).to_string(index=False))
    return 0


def write_backtest_html(path, df, a, n_tickers, secs):
    if not len(df):
        body = "<p>No signals found.</p>"
    else:
        def tbl(groups):
            rows_ = [dict(Group=g, **_stats(x)) for g, x in groups if len(x)]
            return pd.DataFrame(rows_).to_html(index=False, border=0, na_rep="-")
        score_bins = pd.cut(df["Score"], [0, CFG["score_watch"], CFG["score_buy"], 101], right=False,
                            labels=[f"Under {CFG['score_watch']}", f"{CFG['score_watch']}-{CFG['score_buy'] - 1}", f"{CFG['score_buy']}+"])
        def stats_for(x, col):
            y = x.copy()
            if col in y.columns:
                y["R"] = y[col]
                y.loc[y["Outcome"] == "trade", "PnLPct"] = y["PnLPct"]
            return _stats(y)

        var_rows = [dict(Rule=f"{k}: {v['name']}", **stats_for(df, f"R_{k}")) for k, v in EXIT_VARIANTS.items()
                    if f"R_{k}" in df.columns]
        tbl_var = pd.DataFrame(var_rows).to_html(index=False, border=0, na_rep="-") if var_rows else ""
        cand = [(stats_for(df, f"R_{k}").get("AvgR", -9), k) for k in EXIT_VARIANTS if f"R_{k}" in df.columns]
        best = max(cand)[1] if cand else "A"
        g = (df["Score"] >= CFG["score_buy"]) & (df["RRSignal"] >= CFG["min_rr"])
        filt = [("All trades", df), (f"Score {CFG['score_buy']}+ and R:R {CFG['min_rr']:g}+", df[g]),
                ("... and under 4 warnings (current Buy gate)", df[g & (df["NWarn"] < CFG["max_warnings"])]),
                ("Score + R:R 3+ [Murphy ch16, Douglas ch11]", df[(df["Score"] >= CFG["score_buy"]) & (df["RRSignal"] >= 3)]),
                ("Score + R:R, excluding 'Other' [Gann rule 16]", df[g & (df["Category"] != "Other")])]
        for col, lab in [("WeeklyOK", "weekly trend up [Murphy ch10]"), ("New20", "new 20-day high [Murphy ch9]"),
                         ("Squeeze", "Bollinger squeeze [Murphy ch9]"), ("AboveCloud", "above Ichimoku cloud [Allemann]"),
                         ("ADXTurnUp", "ADX turning up from <20 [Murphy ch15]")]:
            if col in df.columns:
                filt.append((f"Score + R:R, {lab}", df[g & df[col].astype(bool)]))
                filt.append((f"Score + R:R, NOT {lab}", df[g & ~df[col].astype(bool)]))
        if "RSIDiv" in df.columns:
            filt.append(("Score + R:R, with bearish RSI divergence [Murphy ch10]", df[g & df["RSIDiv"].astype(bool)]))
        tbl_filters = pd.DataFrame([dict(Group=k, **stats_for(x, f"R_{best}")) for k, x in filt if len(x)]
                                   ).to_html(index=False, border=0, na_rep="-")
        gate = df[g & (df["NWarn"] < CFG["max_warnings"])]
        yrs = [dict(Group=str(y), **stats_for(gate[gate["Year"] == y], f"R_{best}"))
               for y in sorted(gate["Year"].unique())] if "Year" in gate.columns and len(gate) else []
        tbl_years = pd.DataFrame(yrs).to_html(index=False, border=0, na_rep="-") if yrs else ""
        held = df["Held"].mean() * 100
        t = df[df["Outcome"] == "trade"]
        span = f"{df['Date'].min()} to {df['Date'].max()}"
        body = f"""
<p class="hint">{n_tickers} stocks ({a.bt_indexes}), {span}. {len(df)} breakouts passed the hard rules;
{held:.0f}% held the next day; {len(t)} became trades. Entry at the next open inside the buy zone, hard stop
(filled at the open if it gaps through), half sold at the target, the rest trailed by 2 ATR, 15-day time stop,
0.1% slippage each side. R = profit divided by the initial risk per share; average R above 0 means the rules made money.</p>
<h2>Does the scorecard work?</h2>{tbl([("All trades", df), ("Gate pass = would be a Buy", df[df["Gate"]]), ("Gate fail", df[~df["Gate"]])]
                                       + [(f"Score {lab}", df[score_bins == lab]) for lab in score_bins.cat.categories])}
<h2>Exit rules compared (same trades)</h2>{tbl_var}
<h2>Entry filters from the books (using exit rule {best})</h2>{tbl_filters}
<h2>Year by year (current Buy gate, exit rule {best})</h2>{tbl_years}
<h2>By setup type</h2>{tbl([(c, df[df["Category"] == c]) for c in ["Bottoming", "Continuation", "Other"]])}
<h2>By pattern</h2>{tbl([(p, df[df["Pattern"] == p]) for p in sorted(df["Pattern"].unique())])}
<h2>By breakout volume</h2>{tbl([("1.5-2x", df[df["VolX"] < 2]), ("2-3x", df[(df["VolX"] >= 2) & (df["VolX"] < 3)]), ("3x+", df[df["VolX"] >= 3])])}
<h2>By reward:risk at the signal</h2>{tbl([("Under 2", df[df["RRSignal"] < 2]), ("2+", df[df["RRSignal"] >= 2])])}
<h2>By warnings</h2>{tbl([("0-1", df[df["NWarn"] <= 1]), ("2", df[df["NWarn"] == 2]), ("3+", df[df["NWarn"] >= 3])])}
<h2>By market</h2>{tbl([("US", df[df["Region"] == "US"]), ("Canada", df[df["Region"] == "CA"])])}
<h2>How to read this</h2>
<ul class="hint"><li>Look at <b>AvgR</b> (expectancy per trade, in units of risk) and <b>ProfitFactor</b> (gross wins / gross losses).
Above 0 and above 1.0 respectively means an edge; the Gate-pass row should beat the Gate-fail row if the scorecard helps.</li>
<li>Small groups (under ~30 trades) are noise.</li>
<li>Limits: today's index members only (survivorship bias flatters results), no sector or earnings data in history
(those score neutral), daily bars only (no intraday VWAP check), no commissions beyond slippage.</li></ul>"""
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Backtest</title>
<style>{PAGE_CSS}{RUN_CSS}</style></head><body><main><h1>Backtest results</h1>{RUN_HTML}
<p class="hint">Run {dt.date.today():%b %d, %Y} in {secs / 60:.0f} min &middot; <a href="index.html">Latest scan</a></p>
<div class="wrap">{body}</div></main></body></html>"""
    html = html.replace('<div class="wrap">', "<div>", 1).replace("<table", '<div class="wrap"><table').replace("</table>", "</table></div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

# --------------------------------------------------------------------------
# Self-test (offline)
# --------------------------------------------------------------------------
def _synthetic(kind, seed=1):
    rng = np.random.default_rng(seed)
    n = 500
    if kind == "bottom":          # long decline, rounded base, coil under 100, breakout
        path = np.concatenate([np.linspace(160, 140, 250), np.linspace(140, 80, 90),
                               80 + 18 * np.sin(np.linspace(0, np.pi / 2, 150)) ** 2])
        path = np.concatenate([path, np.full(n - len(path) - 1, 99.0)])
    else:                         # steady uptrend, tight flat top, breakout
        path = np.concatenate([np.linspace(40, 98, 480), np.full(n - 481, 99.0)])
    close = path + rng.normal(0, 0.25, len(path))
    close[-8:] = np.clip(close[-8:], 98.2, 99.4)
    open_ = close + rng.normal(0, 0.2, len(close))
    high = np.maximum(open_, close) + np.abs(rng.normal(0.3, 0.1, len(close)))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.3, 0.1, len(close)))
    high[-8:] = np.minimum(high[-8:], 100.0)
    high[-8:][::2] = 100.0                         # repeated tests of 100
    vol = rng.normal(1e6, 1e5, len(close))
    # breakout candle: opens 99.5, closes 103.2, high 103.4, volume 2.6x
    open_ = np.append(open_, 99.5); close = np.append(close, 103.2)
    high = np.append(high, 103.4); low = np.append(low, 99.3); vol = np.append(vol, 2.6e6)
    idx = pd.bdate_range(end="2026-10-09", periods=len(close))
    return pd.DataFrame(dict(Open=open_, High=high, Low=low, Close=close, Volume=vol), index=idx)


def _tracker_selftest():
    base = _synthetic("uptrend")
    d0 = prep(base)
    A0 = arrays(d0)
    r = analyze(d0, A0)
    r["Ticker"], r["Indexes"] = "TEST", "Test"
    enrich(r, d0, A0, {})
    r.update(Score=90.0, Grade="A", NWarn=0, Gate=True, NextRes=np.nan)
    res = pd.DataFrame([r])
    asof = d0.index[-1].date().isoformat()
    R = r["Resistance"]

    def add(df, o, h, l, c, days=1):
        idx = pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=days)
        new = pd.DataFrame(dict(Open=[o] * days, High=[h] * days, Low=[l] * days, Close=[c] * days,
                                Volume=[1.5e6] * days), index=idx)
        return pd.concat([df, new])

    def run(path_bars, res=res):
        rows = update_tracker([], res, {"TEST": d0}, asof)
        df, seq = base, [rows[0]["Status"]]
        for bar in path_bars:
            df = add(df, *bar)
            rows = update_tracker(rows, pd.DataFrame(), {"TEST": prep(df)}, df.index[-1].date().isoformat())
            seq.append(rows[0]["Status"])
        return seq, rows[0]

    ok = True
    cases = [
        ("hold, then stop out", [(R * 1.01, R * 1.02, R * 0.999, R * 1.015), (R * 1.0, R * 1.0, R * 0.95, R * 0.96)],
         ["WATCH", "BUY", "SELL"]),
        ("failed hold", [(R * 1.0, R * 1.01, R * 0.97, R * 0.98)], ["WATCH", "DROP"]),
        ("target, then trail", [(R * 1.01, R * 1.02, R * 0.999, R * 1.015), (R * 1.03, R * 1.04, R * 1.02, R * 1.035),
                                (R * 1.10, R * 1.25, R * 1.09, R * 1.22), (R * 1.22, R * 1.24, R * 1.20, R * 1.23),
                                (R * 1.1, R * 1.1, R * 1.0, R * 1.02)],
         ["WATCH", "BUY", "HOLD", "TAKE PROFIT", "TRAIL", "SELL"]),
    ]
    stop_px = trade_plan(R, r["BaseLow"], r["ATR"], R * 1.015, np.nan)[0]
    fail_px = (R * (1 - CFG["hold_tol"]) + stop_px) / 2            # below the level, above the stop
    if fail_px > stop_px:
        cases.append(("failed breakout exit", [(R * 1.01, R * 1.02, R * 0.999, R * 1.015),
                                               (R, R, fail_px * 0.999, fail_px)], ["WATCH", "BUY", "SELL"]))
    low = pd.DataFrame([dict(r, Score=62.0, Grade="B")])
    hold = (R * 1.01, R * 1.02, R * 0.999, R * 1.015)
    cases.append(("score 62: watch only", [hold] * 6, ["WATCH"] * 6 + ["DROP"]))
    for name, bars, want in cases:
        seq, last = run(bars, low if name.startswith("score 62") else res)
        good = seq == want
        ok &= good
        if name == "failed breakout exit":
            good = good and str(last.get("Note", "")).startswith("Failed breakout")
            ok &= good
        print(f"tracker : {name:<20} {' > '.join(seq)}" + ("" if good else f"   EXPECTED {' > '.join(want)}"))
    # report renders
    try:
        import tempfile
        rows = update_tracker([], res, {"TEST": d0}, asof)
        PROFILES["TEST"] = {"Name": "Test Corp & Co.", "Sector": "Industrials", "Industry": "Aerospace & Defense",
                            "Country": "Canada", "Website": "https://example.com",
                            "Summary": "Test Corp designs aircraft parts. It sells to airlines worldwide. " * 6}
        with tempfile.TemporaryDirectory() as td:
            rad = [dict(Ticker="TEST", Indexes="Test", Region="CA", Trigger=100.2, Resistance=100.0, Close=99.1,
                        DistPct=1.1, BaseLen=20, BaseDepthPct=4.0, Touches=4, HigherLows=True, Tightening=True,
                        QuietVol=0.8, Uptrend=True, Rising5d=True, VolNeeded=1500000, Readiness=8.0,
                        FirstSeen=asof, LastSeen=asof)]
            write_html(os.path.join(td, "t.html"), asof, rows, res, {"TEST": d0}, [], 1, radar=rad)
            page = open(os.path.join(td, "t.html"), encoding="utf-8").read()
        PROFILES.pop("TEST", None)
        assert "Test Corp &amp; Co." in page and "Aerospace &amp; Defense" in page and "More about the company" in page
        assert "On the radar" in page and "1,500,000" in page
        print("report  : renders (company profile, radar)")
    except Exception as e:
        print("report  : FAILED", e); ok = False
    return ok


def selftest():
    ok = True
    for kind in ["bottom", "uptrend"]:
        d = prep(_synthetic(kind))
        A = arrays(d)
        r = analyze(d, A)
        if r:
            r["Ticker"] = "TEST"
            enrich(r, d, A, {})
        print(f"{kind:<8}: " + (f"{r['Category']} | {r['Pattern']} | vol {r['VolX']}x | base {r['BaseLen']} | score {r['Score']} | R:R {r['RR']} | {r['NWarn']} warnings" if r else "NOT DETECTED"))
        ok &= r is not None
    # negative cases: same setups with a rejection wick, and with low volume
    for label, mod in [("long wick", lambda df: df.assign(High=np.where(df.index == df.index[-1], 108.0, df["High"]))),
                       ("low vol", lambda df: df.assign(Volume=np.where(df.index == df.index[-1], 1.1e6, df["Volume"])))]:
        r = analyze(prep(mod(_synthetic("uptrend"))))
        print(f"{label:<8}: " + ("correctly rejected" if r is None else "WRONGLY ACCEPTED"))
        ok &= r is None
    # radar: the same base one day before the breakout candle should be flagged, with the trigger just above 100
    pre = prep(_synthetic("uptrend").iloc[:-1])
    f = check_forming(pre, arrays(pre))
    good = bool(f) and 100.0 < f["Trigger"] < 101.0
    print("radar   : " + (f"flagged a day early, trigger {f['Trigger']} ({f['DistPct']}% away)" if good else f"FAILED ({f})"))
    ok &= good
    # catch-up: a breakout two sessions old that held both days is still found; one that slipped back is not
    base_df = _synthetic("uptrend")
    nx = pd.bdate_range(base_df.index[-1] + pd.Timedelta(days=1), periods=2)
    held2 = pd.concat([base_df, pd.DataFrame(dict(Open=[103.0, 103.5], High=[104.0, 104.2], Low=[102.5, 102.9],
                                                  Close=[103.6, 103.9], Volume=[1.4e6, 1.2e6]), index=nx)])
    r = analyze(prep(held2))
    good = bool(r) and r.get("LateDays") == 2
    slip = held2.copy(); slip.iloc[-1, slip.columns.get_loc("Close")] = 99.0; slip.iloc[-1, slip.columns.get_loc("Low")] = 98.8
    good2 = analyze(prep(slip)) is None
    print("late    : " + ("caught a 2-session-old breakout; ignored one that slipped back" if good and good2
                          else f"FAILED (held {r and r.get('LateDays')}, slipped found={not good2})"))
    ok &= good and good2
    ok &= _tracker_selftest()
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return ok

# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Volume-confirmed breakout scanner")
    ap.add_argument("--indexes", default="tsx,tsx60,sp500,djia,nasdaq,r2000",
                    help="comma list from: " + ",".join(INDEX_NAMES))
    ap.add_argument("--tickers", help="text file of Yahoo tickers (one per line) instead of indexes")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--top", type=int, default=40, help="new setups kept per market (Canada, US)")
    ap.add_argument("--min-price", type=float, default=CFG["min_price"])
    ap.add_argument("--min-dollar-vol", type=float, default=CFG["min_dollar_vol"])
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--morning", action="store_true", help="intraday check of Buy/Watch names (run ~10am ET)")
    ap.add_argument("--live", action="store_true", help="intraday preview with projected volume (no tracker changes)")
    ap.add_argument("--auto", action="store_true", help="live preview if the market is open, otherwise the normal scan")
    ap.add_argument("--backtest", action="store_true", help="test the rules on 5 years of history")
    ap.add_argument("--bt-indexes", default="sp500,tsx60,djia", help="universe for --backtest")
    ap.add_argument("--bt-years", type=int, default=5)
    a = ap.parse_args()

    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.morning:
        sys.exit(morning_check(a.outdir))
    if a.backtest:
        sys.exit(run_backtest(a))

    CFG["min_price"], CFG["min_dollar_vol"] = a.min_price, a.min_dollar_vol
    LIVE["on"] = bool(a.live or (a.auto and market_open_now()))
    if LIVE["on"]:
        print("Market is open: running a live preview (today's volume projected to a full day).")
    t0 = time.time()
    if a.tickers:
        with open(a.tickers) as f:
            membership = {l.strip().upper(): {"Custom"} for l in f if l.strip()}
    else:
        print("Loading index constituents...")
        membership = build_universe([k.strip() for k in a.indexes.split(",") if k.strip()])
    tickers = sorted(membership, key=lambda t: (region(t) != "CA", t))   # Canada first, then the US
    print(f"Universe: {len(tickers)} unique tickers")

    os.makedirs(a.outdir, exist_ok=True)
    tracker_path = os.path.join(a.outdir, "tracker.csv")
    rows = load_tracker(tracker_path)
    tracked = {r["Ticker"] for r in rows if not r["Closed"]}
    holdings = load_holdings(os.path.join(a.outdir, "holdings.csv"))
    tracked |= {str(h[k]).strip() for h in holdings for k in ("Ticker", "TrendTicker") if str(h.get(k) or "").strip()}
    ctx_t = ["SPY", "DIA", "QQQ", "IWM", "XIU.TO"]
    brief_t = {t for t, _ in BRIEF_INDEXES} if LIVE["on"] else set()      # morning brief: the real indexes
    raw = download(sorted(set(tickers) | set(ctx_t) | tracked | set(ALL_SECTOR_ETFS) | set(MACRO_TICKERS) | set(STRESS_TICKERS)
                          | brief_t,
                          key=lambda t: (region(t) != "CA", t)))   # TSX first, so any late data trouble hits US names
    DIAG.append(f"Universe {len(tickers)} unique tickers; price data received for {len(raw)}")
    data = {}
    frac = session_fraction()
    today = dt.datetime.now(ET).date()
    for t, d in raw.items():
        d0 = d
        try:
            if LIVE["on"]:
                d = d[~d.index.duplicated(keep="last")].copy()     # Yahoo can send today's bar twice
                d["Volume"] = d["Volume"].astype(float)           # whole-number volume can't take the projection
                if len(d) and pd.Timestamp(d.index[-1]).date() == today:
                    d.iloc[-1, d.columns.get_loc("Volume")] = d["Volume"].iloc[-1] / frac
                data[t] = prep(d)
            else:
                data[t] = prep(drop_live_bar(d))
        except Exception as e:
            LIVE.setdefault("prep_fail", [0, ""])
            LIVE["prep_fail"][0] += 1
            LIVE["prep_fail"][1] = LIVE["prep_fail"][1] or f"{t}: {type(e).__name__} {str(e)[:100]}"
            if LIVE["on"]:
                try:
                    data[t] = prep(drop_live_bar(d0))             # fall back to the last close for this one
                except Exception:
                    pass

    found, radar = [], []
    for t in tickers:
        if t not in data:
            continue
        min_dv = CFG["min_dollar_vol_ca"] if region(t) == "CA" else CFG["min_dollar_vol"]
        try:
            A = arrays(data[t])
            r = analyze(data[t], A, min_dv)
            f = None if (r or t in tracked) else check_forming(data[t], A, min_dv)
        except Exception:
            r = f = None
        if r:
            r["Ticker"] = t
            r["Indexes"] = ", ".join(sorted(membership[t]))
            found.append((r, A))
        elif f:
            f.update(Ticker=t, Indexes=", ".join(sorted(membership[t])), Region=region(t))
            radar.append(f)
    radar = radar_merge(radar, os.path.join(a.outdir, "radar.csv"), save=not LIVE["on"])
    late = sum(1 for r, _ in found if r.get("LateDays"))
    print(f"{len(found)} tickers passed the breakout rules ({late} caught late); {len(radar)} on the radar; scoring...")
    DIAG.append(f"Breakouts found: {len(found)} ({sum(1 for r, _ in found if region(r['Ticker']) == 'CA')} Canadian, "
                f"{late} caught 2-3 sessions late); on the radar: {len(radar)}")

    # market context: benchmark trend, breadth (from the whole universe), sector ETFs
    ctx = build_context(data, tickers)
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    try:
        sectors, industries, earnings = fetch_sector_earnings(
            [r["Ticker"] for r, _ in found], os.path.join(a.outdir, "sectors.csv"),
            profile_only=[r["Ticker"] for r in rows                         # names already tracked, incl. ones
                          if not r["Closed"] or str(r["StatusSince"]) >= recent]   # closed in the last few days,
                         + [x["Ticker"] for x in radar]                             # the radar
                         + [str(h["Ticker"]).strip() for h in holdings])            # and your holdings
    except Exception as e:
        sectors, industries, earnings = {}, {}, {}
        DIAG.append(f"Sector/earnings lookup failed: {str(e)[:120]}")
    DIAG.append(f"Sector known for {sum(1 for v in sectors.values() if v)}/{len(found)} candidates; "
                f"earnings dates for {len(earnings)}")
    ctx.update(sector=sectors, industry=industries, earnings=earnings)

    results = []
    for r, A in found:
        try:
            e = enrich(r, data[r["Ticker"]], A, ctx)
            if r["Ticker"] in RADAR_PREV:                 # it was flagged before the breakout
                since, trig = RADAR_PREV[r["Ticker"]]
                e["Signals"] = (f"on the radar since {since}" + (f" (trigger {trig:.2f})" if np.isfinite(trig) else "")
                                + (" · " + e["Signals"] if e.get("Signals") else ""))
            results.append(e)
        except Exception as e:
            DIAG.append(f"{r['Ticker']}: scoring failed ({str(e)[:80]})")
    res = pd.DataFrame(results)
    if len(res):
        res["_ca"] = res["Ticker"].map(lambda t: region(t) != "CA")
        res = (res.sort_values(["_ca", "Score"], ascending=[True, False]).groupby("_ca", group_keys=False)
               .head(a.top).drop(columns="_ca").reset_index(drop=True))   # top N per market, Canada first
    MARKET.clear()
    for reg, name in (("US", "US (S&P 500)"), ("CA", "Canada (TSX 60)")):
        if ctx["bench"].get(reg) is not None:
            m = market_state(ctx["bench"][reg], ctx["breadth"][reg], ctx["bench"][reg].index[-1],
                             ctx["internals"][reg], ctx["credit"], ctx["banks"][reg])
            MARKET.append((name, m))

    asof = max(data[t].index[-1] for t in ctx_t if t in data) if any(t in data for t in ctx_t) \
        else max(d.index[-1] for d in data.values())
    asof = pd.Timestamp(asof).date().isoformat()

    if LIVE["on"]:
        now = dt.datetime.now(ET)
        prev = load_tracker(tracker_path)
        prev_open = {r["Ticker"] for r in prev if not r["Closed"]}
        live_rows = update_tracker([], res[~res["Ticker"].isin(prev_open)] if len(res) else res, data, asof)
        live_rows += [r for r in prev if not r["Closed"]]
        try:
            review = market_review(data, tickers, n_setups=len(found), live=True, ctx=ctx)
        except Exception as e:
            review = ""
            DIAG.append(f"Market review failed: {str(e)[:120]}")
        sub = (f"<b>Live preview</b>, prices as of {now:%H:%M} ET ({frac * 100:.0f}% of a normal day's volume traded); "
               "today's volume is projected to a full day. Nothing here is final until the close")
        write_html(os.path.join(a.outdir, "live.html"), asof, live_rows, res, data, market_context(data), len(data),
                   review, title="Live preview", subtitle=sub, radar=radar)
        print(f"Live preview: {len(res)} setups in progress -> {os.path.join(a.outdir, 'live.html')}")
        try:
            write_live_brief(a.outdir, data, tickers, ctx, len(found), len(radar), frac, raw)
        except Exception as e:
            print("Live brief failed:", e)
        return
    rows = update_tracker(rows, res, data, asof)
    pd.DataFrame(rows, columns=TRACK_COLS).to_csv(tracker_path, index=False)

    csv_path = os.path.join(a.outdir, f"breakouts_{asof}.csv")
    html_path = os.path.join(a.outdir, f"breakouts_{asof}.html")
    (res.drop(columns=["_b", "_s"]) if len(res) else res).to_csv(csv_path, index=False)
    try:
        review = market_review(data, tickers, n_setups=len(found), ctx=ctx)
    except Exception as e:
        review = ""
        DIAG.append(f"Market review failed: {str(e)[:120]}")
    usdcad = float(data["CAD=X"]["Close"].iloc[-1]) if "CAD=X" in data else np.nan
    write_html(html_path, asof, rows, res, data, market_context(data), len(data), review, radar=radar,
               portfolio=portfolio_html(holdings, data, rows, usdcad))

    print(f"\n{len(res)} new setups (scanned {len(data)} tickers in {time.time() - t0:.0f}s), close of {asof}\n")
    live = [r for r in rows if not r["Closed"] or r["StatusSince"] == asof]
    for r in sorted(live, key=lambda r: r["Status"]):
        print(f"  {r['Status']:<12} {r['Ticker']:<10} {r['Note']}")
    print(f"\nReport:  {os.path.abspath(html_path)}\nTracker: {os.path.abspath(tracker_path)}")
    if a.open:
        webbrowser.open("file://" + os.path.abspath(html_path))


if __name__ == "__main__":
    main()
