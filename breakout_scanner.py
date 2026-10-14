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
import io
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
    if i2 - i1 < 10 or abs(L[i1] - L[i2]) / min(L[i1], L[i2]) > 0.04:
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


def detect_bull_flag(H, L, s, Lb, R, base_low):
    if Lb > 15:
        return False
    ps = max(0, s - 20)
    if s - ps < 3:
        return False
    i_low = ps + int(np.argmin(L[ps:s]))
    pole_top = max(H[i_low:s + 1].max(), R)
    pole = pole_top - L[i_low]
    if pole <= 0:
        return False
    gain = pole / L[i_low]
    retrace = (pole_top - base_low) / pole
    return gain >= 0.15 and retrace <= 0.5 and _slope(H[s:s + Lb]) <= 0.002 * R


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


def trade_plan(R, low, atr, entry, next_res):
    stop = min(max(low, R - TRADE["stop_atr"] * atr), R * 0.985)
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


def check_breakout(d, b, off, A=None):
    """Hard rules from the original brief. Returns None if any one fails."""
    A = A or arrays(d)
    O, H, L, C, V = A["Open"], A["High"], A["Low"], A["Close"], A["Volume"]
    atr, avgv, sma50, sma200 = A["ATR14"][b - 1], A["AvgVol50"][b], A["SMA50"][b], A["SMA200"][b]
    if not np.isfinite([atr, avgv, sma50, sma200]).all() or avgv <= 0 or atr <= 0:
        return None
    c = C[b]
    if c < CFG["min_price"] or avgv * c < CFG["min_dollar_vol"]:
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
    if off == 1:
        n = b + 1
        if C[n] < R or L[n] < R * (1 - CFG["hold_tol"]):
            return None
        status = "Confirmed (held next day)"
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
        if detect_bull_flag(H, L, s, Lb, R, low):
            pattern = "Bull flag"
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
    gap = O[b] > H[b - 1] * 1.02
    if gap:
        flags.append("Gap-up breakout (check for news/earnings)")

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

    # --- overhead resistance, trade plan (from the latest close: breakout day, or the hold day)
    entry = C[b + off]
    nres = next_resistance(A, s, b, entry)
    stop, target, rr, capped = trade_plan(R, low, atr, entry, nres)
    supply = supply_zone(A, b, entry, target)
    room = (not np.isfinite(nres)) or nres >= entry + CFG["min_rr"] * (entry - stop)

    return dict(
        Category=category, Pattern=pattern, Status=status, Held=off == 1,
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
        Flags="; ".join(flags), _b=b, _s=s,
    )


def analyze(d, A=None):
    if len(d) < 215:
        return None
    A = A or arrays(d)
    n = len(d) - 1
    return check_breakout(d, n - 1, 1, A) or check_breakout(d, n, 0, A)

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


def market_state(bench, breadth, date):
    """Trend, breadth and distribution days for the benchmark as of a date."""
    out = dict(Trend="Unknown", Breadth=np.nan, DistDays=0, Points=5.0, Pressure=False)
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
    br = breadth.loc[:date].iloc[-1] if breadth is not None and len(breadth.loc[:date]) else np.nan
    pts = {"Uptrend": 5, "Mixed": 3 if c > s50 else 1.5, "Downtrend": 0}[trend]
    if np.isfinite(br):
        pts += 5 if br >= 0.6 else 3.5 if br >= 0.5 else 2 if br >= 0.4 else 0
    else:
        pts += 2.5
    if dist >= 6:
        pts -= 2
    pressure = trend == "Downtrend" or dist >= 6 or (np.isfinite(br) and br < 0.4)
    return dict(Trend=trend, Breadth=br, DistDays=dist, Points=max(0.0, pts), Pressure=bool(pressure))


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

    # sector strength
    sector, sec_rs = ctx.get("sector", {}).get(r.get("Ticker")), np.nan
    sec_d = ctx.get("sector_data", {}).get(SECTOR_ETF.get(reg, {}).get(sector)) if sector else None
    if sec_d is not None and bench is not None:
        sc = sec_d["Close"].reindex(d.index).ffill().to_numpy(dtype=float)
        sec_rs = (_ret(sc, b, 20) - _ret(A["BENCH"], b, 20)) * 100
    sec_ok = None if not np.isfinite(sec_rs) else bool(sec_rs > 0)

    mkt = market_state(bench, ctx.get("breadth", {}).get(reg), date)

    # earnings
    earn = ctx.get("earnings", {}).get(r.get("Ticker"))
    earn_days = np.nan
    if earn is not None:
        earn_days = int(np.busday_count(pd.Timestamp(date).date(), pd.Timestamp(earn).date()))

    # ---------- 100-point scorecard (weights from the checklist) ----------
    v = r["VolX"]
    p_vol = 0 if v < 1.5 else 10 + 10 * (v - 1.5) if v < 2 else min(20, 15 + 5 * (v - 2))
    p_price = (5 * np.clip((r["ClosePos"] - 0.6) / 0.4, 0, 1) + 4 * (1 - r["UpperWickPct"] / 100 / CFG["max_wick_frac"])
               + 2 * np.clip((r["RangeATR"] - 1.1) / 0.9, 0, 1) + 2 * np.clip(r["ClearATR"] / 0.5, 0, 1)
               + (2 if r["Held"] else 0))
    dry = r["VolDryUp"]
    p_base = (2 * max(0, r["BaseTightness"]) + 2 * min(1, (r["Touches"] + r["ResTests120d"]) / 5)
              + 2 * r["RangeContraction"] + (2 if np.isfinite(dry) and dry < 0.85 else 1 if np.isfinite(dry) and dry < 1 else 0)
              + (1 if r["UpDownVol"] >= 1.2 else 0) + (1 if r["Category"] == "Bottoming" else 0))
    p_base = min(10, p_base)
    p_rs = ((5 * np.clip(rs20 / 10, 0, 1) if np.isfinite(rs20) else 2.5)
            + (3 * np.clip(rs63 / 15, 0, 1) if np.isfinite(rs63) else 1.5)
            + (4 if rs_high else 0) + (1.5 if sec_ok is None else 3 if sec_ok else 0))
    if leader:
        p_rs = min(15, p_rs + 2)
    p_mkt = mkt["Points"]
    ev = r["ExtVWAP20ATR"]
    p_vwap = (3 * r["AboveAVWAPBase"] + 2 * r["AboveAVWAP52wLow"]
              + (2 if np.isfinite(ev) and ev <= 2.5 else 0) + 3 * r["Structure"])
    rr = r["RR"]
    p_rr = 10 if rr >= 3 else 7 + 3 * (rr - 2) if rr >= 2 else 3 if rr >= 1.5 else 0
    p_rr += 5 if r["Room"] else 0
    p_rr -= 3 if np.isfinite(r["SupplyZone"]) else 0
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
    if r["UpperWickPct"] > 20:
        warn.append(f"Upper wick {r['UpperWickPct']}% of the candle")
    if r["ClosePos"] < 0.7:
        warn.append("Closed off the high of the day")
    if np.isfinite(rs20) and rs20 < 0:
        warn.append(f"Lagging the market over 20 days ({rs20:+.1f} pts)")
    if sec_ok is False:
        warn.append(f"Sector ({sector}) lagging the market")
    if r["PctAboveRes"] > TRADE["extended"] * 100 or (np.isfinite(ev) and ev > 2.5):
        warn.append("Extended above the breakout level / 20-day VWAP")
    if not r["Room"] or rr < CFG["min_rr"]:
        warn.append(f"Resistance at {r['NextRes']} limits upside (R:R {rr})")
    if np.isfinite(r["SupplyZone"]):
        warn.append(f"Heavy-volume supply zone near {r['SupplyZone']}")
    if np.isfinite(earn_days) and 0 <= earn_days <= CFG["earnings_block_days"]:
        warn.append(f"Earnings in {earn_days} sessions")
    if mkt["Pressure"]:
        warn.append(f"Market under pressure ({mkt['Trend']}, {mkt['DistDays']} distribution days)")
    if r["ATRPct"] / 100 > CFG["max_atr_pct"]:
        warn.append(f"Erratic: daily range {r['ATRPct']}% of price")
    if r["RSI"] > 80:
        warn.append(f"RSI overbought ({r['RSI']})")
    if dv < 10:
        warn.append(f"Thin liquidity (${dv}M/day)")
    if r["LateRunup"]:
        warn.append("Big run-up before the base (late)")
    if r["Category"] == "Other":
        warn.append("No clear trend or bottom behind it")
    if r["Gap"]:
        warn.append("Gap-up breakout: check the news")

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
        ("VWAP and momentum support the trade", bool(r["AboveAVWAPBase"] and 50 <= r["RSI"] <= 80 and r["MACDH"] > 0)),
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


def fetch_sector_earnings(tickers, cache_path):
    """Sector (cached) and next earnings date for the candidates only."""
    import yfinance as yf
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            cache = dict(pd.read_csv(cache_path, dtype=str, keep_default_na=False).values.tolist())
        except Exception:
            cache = {}
    sectors, earnings = {}, {}
    today = dt.date.today()
    for t in tickers:
        tk = yf.Ticker(t)
        if t not in cache or not cache[t]:
            try:
                cache[t] = (tk.get_info() or {}).get("sector", "") or ""
            except Exception:
                cache[t] = ""
        sectors[t] = cache[t] or None
        try:
            cal = tk.calendar
            dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
            fut = sorted(pd.Timestamp(x).date() for x in dates if pd.Timestamp(x).date() >= today)
            if fut:
                earnings[t] = fut[0]
        except Exception:
            pass
    if cache_path:
        pd.DataFrame(sorted(cache.items()), columns=["Ticker", "Sector"]).to_csv(cache_path, index=False)
    return sectors, earnings

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
    time_stop_gain=0.02,
)

TRACK_COLS = ["Ticker", "Indexes", "Sector", "Category", "Pattern", "Score", "Grade", "ScoreParts", "Checklist",
              "Warnings", "NWarn", "Gate", "BreakoutDate", "BaseStart", "Resistance", "BaseLow", "ATR", "VolX",
              "BaseLen", "NextRes", "SupplyZone", "RR", "RS20", "Market", "EarningsDate", "Shares", "RiskPerShare",
              "Flags", "Status", "StatusSince", "Stop", "Target", "EntryDate", "EntryPrice", "LastClose",
              "HighClose", "PnLPct", "LastDate", "Note", "Closed"]
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
    keep = [k for k in TRACK_COLS if k not in ("Status", "StatusSince", "Stop", "Target", "EntryDate", "EntryPrice",
                                               "LastClose", "HighClose", "PnLPct", "LastDate", "Note", "Closed")]
    for _, r in res.iterrows():
        if r["Ticker"] in open_now or _num(r.get("Score"), 0) < CFG["score_watch"]:
            continue
        row = {k: np.nan for k in TRACK_COLS}
        row.update({k: r[k] for k in keep if k in r})
        row["NextRes"] = _num(r.get("NextRes"))
        row.update(LastClose=r["Close"], LastDate=asof, Closed=False)
        if not r["Held"]:
            row["Stop"], row["Target"] = r["Stop"], r["Target"]
            _set(row, "WATCH", asof, f"Broke out on {r['VolX']}x volume - must hold tomorrow")
        else:
            _qualify(row, asof, r["Close"], "Broke out and held the next day", held_bonus=0.0)
        rows.append(row)
    for row in rows:
        row.pop("_watch_only", None)
    return rows

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
        return (f"Broke above <b>{_f(R)}</b> today on {r['VolX']}x normal volume. Don't buy yet: tomorrow it must "
                f"close above {_f(R)} without trading below {_f(R * (1 - CFG['hold_tol']))}, and pass the scorecard.")
    if st == "EXTENDED":
        return (f"The breakout held, but price is {_pct(r['LastClose'], R)} above the breakout level. "
                f"Don't chase it. Buy only if it pulls back to <b>{_f(R)}-{_f(R * (1 + TRADE['buy_zone']))}</b> and holds (a retest).")
    if st == "HOLD":
        return (f"In the trade since {r['EntryDate']} at {_f(r['EntryPrice'])} (<b>{r['PnLPct']:+.1f}%</b>). "
                f"Keep the stop order at <b>{_f(r['Stop'])}</b>; target <b>{_f(r['Target'])}</b>.")
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
                  f'{_checklist_html(r.get("Checklist"))}<p class="hint">Market at signal: {r.get("Market", "")}</p></details>')
    chart_html = (f'<details><summary>Chart</summary><img alt="{r["Ticker"]} chart" '
                  f'src="data:image/png;base64,{chart}"/></details>') if chart else ""
    return f"""<article class="card" id="t-{r['Ticker']}" style="--c:{color}">
  <header><div><h3>{r['Ticker']} {score_html}</h3><p class="sub">{r['Pattern']} &middot; {r['Indexes']}</p></div>
  <div class="right"><span class="badge">{label}{' &middot; new' if new else ''}</span><p class="px">{_f(r['LastClose'])}</p></div></header>
  <p class="do">{instruction(r)}</p>
  {'' if compact else f'<div class="facts">{facts_html}</div>'}
  {warn_html and f'<div class="flags"><span class="wl">{len(warns)} warning{"s" if len(warns) != 1 else ""}:</span>{warn_html}</div>'}
  {detail}{chart_html}
</article>"""


def write_html(path, asof, rows, res, data, ctx, n_scanned):
    asof_txt = pd.Timestamp(asof).strftime("%a %b %d, %Y")
    act = [r for r in rows if not r["Closed"] or r["StatusSince"] == asof]
    order = {"SELL": 0, "TAKE PROFIT": 1, "BUY": 2, "WATCH": 3, "EXTENDED": 4, "HOLD": 5, "TRAIL": 6, "DROP": 7}
    act.sort(key=lambda r: (order.get(r["Status"], 9), -_num(r.get("Score"), 0)))

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
    closed.sort(key=lambda r: r["StatusSince"], reverse=True)

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
        return (f'<tr><td class="tk"><a href="#t-{r["Ticker"]}">{r["Ticker"]}</a></td>'
                f'<td><span class="chip" style="--c:{color}">{label}</span></td><td>{sc}</td>'
                f'<td>{r["Pattern"]}</td><td>{r["Category"]}</td><td>{_f(r["LastClose"])}</td>'
                f'<td>{_f(r["Resistance"])}</td><td>{above:+.1f}%</td><td>{r["VolX"]}x</td><td>{base}</td>'
                f'<td>{_f(_num(r.get("Stop")))}</td><td>{_f(_num(r.get("Target")))}</td><td>{_f(_num(r.get("RR")), 1)}</td>'
                f'<td>{nw}</td>{pnl_td}<td>{ed}</td><td>{r["BreakoutDate"]}</td><td>{r["Indexes"]}</td></tr>')

    summary = ("<section><h2>Summary</h2><p class=\"hint\">Every setup being tracked. Tap a ticker for its card and chart; "
               "swipe the table sideways for more columns.</p><div class=\"wrap\"><table class=\"sum\"><tr>"
               "<th class=\"tk\">Ticker</th><th>Action</th><th>Score</th><th>Pattern</th><th>Type</th><th>Close</th>"
               "<th>Breakout level</th><th>Above level</th><th>Volume</th><th>Base</th><th>Stop</th><th>Target</th>"
               "<th>R:R</th><th>Warnings</th><th>P&amp;L</th><th>Earnings</th><th>Breakout date</th><th>Index</th></tr>"
               + "".join(sum_row(r) for r in act) + "</table></div></section>") if act else ""

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
<h1>Breakout Scan</h1>
<p class="hint">Based on the close of <b>{asof_txt}</b> &middot; {n_scanned:,} stocks scanned &middot; {len(res)} new setups</p>
<div class="chips">{chips}</div>
<div class="mkts">{mkt}</div>
{regime}
<p class="links"><a href="morning.html">Morning check (10 am)</a> &middot; <a href="backtest.html">Backtest results</a> &middot; <a href="archive.html">Past scans</a></p>
{summary}
{section("Act at tomorrow's open", "New buys, profit-taking and exits.", do_now, empty="No action needed tomorrow.")}
{section("Watchlist", "Broke out but not confirmed yet, or too far above the breakout level to buy.", watch, empty="Nothing on watch.")}
{section("Open positions", "Setups that triggered a Buy and are still running.", holding, compact=True, empty="No open positions.")}
{closed_sec}
<section><h2>What the labels mean</h2><ul class="gl">{glossary}</ul>
<p class="hint">Entry, stop and target are calculated from the breakout level. Profit/loss is measured from the close on the day of the Buy signal; your real entry is the next open.</p></section>
{rej_sec}
<details><summary>Full scan details ({len(res)} setups)</summary><div class="wrap">{raw_tbl}</div></details>
<details><summary>Scan diagnostics</summary><ul class="hint">{''.join(f"<li>{x}</li>" for x in DIAG)}</ul></details>
<footer>Technical screen only - not investment advice. Scores and thresholds are rules of thumb until the backtest supports them.</footer>
</main></body></html>"""
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
    if not force and (now.weekday() >= 5 or not (dt.time(9, 50) <= now.time() <= dt.time(11, 30))):
        print(f"Morning check skipped: it is {now:%a %H:%M} ET (runs 9:50-11:30 ET on weekdays).")
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
                     f'<p>{text}</p><div class="facts">{facts}</div></article>')
    stamp = items[0][2]["time"] if items and items[0][2] else now.strftime("%H:%M ET")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Morning check {now:%Y-%m-%d}</title>
<style>{PAGE_CSS}</style></head><body><main>
<h1>Morning check</h1>
<p class="hint">{now:%a %b %d, %Y} &middot; prices as of {stamp} &middot; <a href="index.html">Last night's scan</a></p>
<p class="hint">Checks last night's Buy, Don't-chase, Watch and open positions against today's session: price vs the
day's VWAP and its direction, volume compared with the same time on the last 10 days, and the buy zone.
US quotes are near real-time; TSX quotes can be delayed about 15 minutes. Re-check your broker before acting.</p>
{'<p class="hint"><b>Test run:</b> nothing is being tracked, so this checks the latest scan candidates instead.</p>' if demo else ''}
{''.join(cards) or '<p class="hint">Nothing to check this morning.</p>'}
<p class="hint">Technical screen only - not investment advice.</p>
</main></body></html>"""
    with open(os.path.join(outdir, "morning.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Morning check written for {len(items)} names")
    return 0

# --------------------------------------------------------------------------
# Backtest: do these rules make money after costs?
# --------------------------------------------------------------------------
def simulate_trade(A, b, r, plan_entry_day):
    """Enter at the open after the hold day; hard stop (gap-aware), half off at target, trail the rest."""
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
    pos, pnl, half, high_c = 1.0, 0.0, False, entry
    exit_day, reason = None, ""
    for j in range(j0, min(n, j0 + 60)):
        fill_open = O[j] if j > j0 else entry
        if L[j] <= stop:
            px = min(fill_open, stop)
            pnl += pos * (px * (1 - slip) - e)
            exit_day, reason, pos = j, ("trail stop" if half else "stop"), 0
            break
        if not half and H[j] >= target:
            px = max(fill_open, target)
            pnl += 0.5 * (px * (1 - slip) - e)
            pos, half = 0.5, True
            stop = max(stop, entry)
        high_c = max(high_c, C[j])
        if half:
            stop = max(stop, high_c - TRADE["trail_atr"] * r["ATR"])
        held = j - j0 + 1
        if not half and held >= TRADE["time_stop_days"] and C[j] < entry * (1 + TRADE["time_stop_gain"]):
            px = O[j + 1] if j + 1 < n else C[j]
            pnl += pos * (px * (1 - slip) - e)
            exit_day, reason, pos = j + 1, "time stop", 0
            break
    if pos > 0:
        j = min(n - 1, j0 + 59)
        pnl += pos * (C[j] * (1 - slip) - e)
        exit_day, reason = j, ("max hold" if j == j0 + 59 else "open at end of data")
    return dict(Outcome="trade", Entry=round(entry, 2), Stop0=round(e - risk, 2), Target=target, RRPlan=rr,
                R=round(pnl / risk, 3), PnLPct=round(pnl / e * 100, 2), Days=exit_day - j0 + 1, Exit=reason,
                HitTarget=half)


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
    raw = download(sorted(set(tickers) | set(BENCH.values())), period=f"{a.bt_years}y")
    data = {}
    for t, d in raw.items():
        try:
            data[t] = prep(d)
        except Exception:
            pass
    ctx = dict(bench={k: data.get(v) for k, v in BENCH.items()},
               breadth={"US": breadth_series(data, [t for t in tickers if region(t) == "US"]),
                        "CA": breadth_series(data, [t for t in tickers if region(t) == "CA"])},
               sector={}, earnings={}, sector_data={})
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
                       Gate=bool(score_h >= CFG["score_buy"] and r["RR"] >= CFG["min_rr"] and r["NWarn"] < CFG["max_warnings"]))
            if held and not ext:
                tr = simulate_trade(A, b, r, b + 2)
                row.update(tr or dict(Outcome="no data"))
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
<style>{PAGE_CSS}</style></head><body><main><h1>Backtest results</h1>
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
    low = pd.DataFrame([dict(r, Score=62.0, Grade="B")])
    hold = (R * 1.01, R * 1.02, R * 0.999, R * 1.015)
    cases.append(("score 62: watch only", [hold] * 6, ["WATCH"] * 6 + ["DROP"]))
    for name, bars, want in cases:
        seq, last = run(bars, low if name.startswith("score 62") else res)
        good = seq == want
        ok &= good
        print(f"tracker : {name:<20} {' > '.join(seq)}" + ("" if good else f"   EXPECTED {' > '.join(want)}"))
    # report renders
    try:
        import tempfile
        rows = update_tracker([], res, {"TEST": d0}, asof)
        with tempfile.TemporaryDirectory() as td:
            write_html(os.path.join(td, "t.html"), asof, rows, res, {"TEST": d0}, [], 1)
        print("report  : renders")
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
    ap.add_argument("--top", type=int, default=40, help="rows kept in the report")
    ap.add_argument("--min-price", type=float, default=CFG["min_price"])
    ap.add_argument("--min-dollar-vol", type=float, default=CFG["min_dollar_vol"])
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--morning", action="store_true", help="intraday check of Buy/Watch names (run ~10am ET)")
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
    t0 = time.time()
    if a.tickers:
        with open(a.tickers) as f:
            membership = {l.strip().upper(): {"Custom"} for l in f if l.strip()}
    else:
        print("Loading index constituents...")
        membership = build_universe([k.strip() for k in a.indexes.split(",") if k.strip()])
    tickers = sorted(membership)
    print(f"Universe: {len(tickers)} unique tickers")

    os.makedirs(a.outdir, exist_ok=True)
    tracker_path = os.path.join(a.outdir, "tracker.csv")
    rows = load_tracker(tracker_path)
    tracked = {r["Ticker"] for r in rows if not r["Closed"]}
    ctx_t = ["SPY", "DIA", "QQQ", "IWM", "XIU.TO"]
    raw = download(sorted(set(tickers) | set(ctx_t) | tracked | set(ALL_SECTOR_ETFS)))
    DIAG.append(f"Universe {len(tickers)} unique tickers; price data received for {len(raw)}")
    data = {}
    for t, d in raw.items():
        try:
            data[t] = prep(drop_live_bar(d))
        except Exception:
            pass

    found = []
    for t in tickers:
        if t not in data:
            continue
        try:
            A = arrays(data[t])
            r = analyze(data[t], A)
        except Exception:
            r = None
        if r:
            r["Ticker"] = t
            r["Indexes"] = ", ".join(sorted(membership[t]))
            found.append((r, A))
    print(f"{len(found)} tickers passed the breakout rules; scoring...")

    # market context: benchmark trend, breadth (from the whole universe), sector ETFs
    ctx = dict(bench={k: data.get(v) for k, v in BENCH.items()},
               breadth={"US": breadth_series(data, [t for t in tickers if region(t) == "US"]),
                        "CA": breadth_series(data, [t for t in tickers if region(t) == "CA"])},
               sector_data={e: data[e] for e in ALL_SECTOR_ETFS if e in data})
    try:
        sectors, earnings = fetch_sector_earnings([r["Ticker"] for r, _ in found],
                                                  os.path.join(a.outdir, "sectors.csv"))
    except Exception as e:
        sectors, earnings = {}, {}
        DIAG.append(f"Sector/earnings lookup failed: {str(e)[:120]}")
    DIAG.append(f"Sector known for {sum(1 for v in sectors.values() if v)}/{len(found)} candidates; "
                f"earnings dates for {len(earnings)}")
    ctx.update(sector=sectors, earnings=earnings)

    results = []
    for r, A in found:
        try:
            results.append(enrich(r, data[r["Ticker"]], A, ctx))
        except Exception as e:
            DIAG.append(f"{r['Ticker']}: scoring failed ({str(e)[:80]})")
    res = pd.DataFrame(results)
    if len(res):
        res = res.sort_values(["Score"], ascending=False).head(a.top).reset_index(drop=True)
    MARKET.clear()
    for reg, name in (("US", "US (S&P 500)"), ("CA", "Canada (TSX 60)")):
        if ctx["bench"].get(reg) is not None:
            m = market_state(ctx["bench"][reg], ctx["breadth"][reg], ctx["bench"][reg].index[-1])
            MARKET.append((name, m))

    asof = max(data[t].index[-1] for t in ctx_t if t in data) if any(t in data for t in ctx_t) \
        else max(d.index[-1] for d in data.values())
    asof = pd.Timestamp(asof).date().isoformat()
    rows = update_tracker(rows, res, data, asof)
    pd.DataFrame(rows, columns=TRACK_COLS).to_csv(tracker_path, index=False)

    csv_path = os.path.join(a.outdir, f"breakouts_{asof}.csv")
    html_path = os.path.join(a.outdir, f"breakouts_{asof}.html")
    (res.drop(columns=["_b", "_s"]) if len(res) else res).to_csv(csv_path, index=False)
    write_html(html_path, asof, rows, res, data, market_context(data), len(data))

    print(f"\n{len(res)} new setups (scanned {len(data)} tickers in {time.time() - t0:.0f}s), close of {asof}\n")
    live = [r for r in rows if not r["Closed"] or r["StatusSince"] == asof]
    for r in sorted(live, key=lambda r: r["Status"]):
        print(f"  {r['Status']:<12} {r['Ticker']:<10} {r['Note']}")
    print(f"\nReport:  {os.path.abspath(html_path)}\nTracker: {os.path.abspath(tracker_path)}")
    if a.open:
        webbrowser.open("file://" + os.path.abspath(html_path))


if __name__ == "__main__":
    main()
