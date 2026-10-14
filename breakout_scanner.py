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
    max_base=40,
    tight_short=0.06,         # max base depth for bases <= 10 candles
    tight_long=0.15,          # max base depth for a 40-candle base
    touch_tol=0.015,          # high within 1.5% of resistance = a touch
    min_touches=2,
    breakout_buffer=0.002,    # close must clear resistance by 0.2%
    max_ext_above_res=0.06,   # > 6% above resistance = late entry, rejected
    max_ext_sma50=0.30,       # > 30% above 50-day = extended, rejected
    hold_tol=0.01,            # next candle low may dip at most 1% under resistance
    bottom_drawdown=0.20,     # decline needed before a "bottoming" base
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
    lows = _swing_lows(L, w0, b - 3)
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
def check_breakout(d, b, off):
    O, H, L, C, V = (d[c].to_numpy() for c in ["Open", "High", "Low", "Close", "Volume"])
    atr = d["ATR14"].iloc[b - 1]
    avgv = d["AvgVol50"].iloc[b]
    sma50 = d["SMA50"].iloc[b]
    sma200 = d["SMA200"].iloc[b]
    if not np.isfinite([atr, avgv, sma50, sma200]).all() or avgv <= 0 or atr <= 0:
        return None
    c = C[b]
    if c < CFG["min_price"] or avgv * c < CFG["min_dollar_vol"]:
        return None

    # --- breakout candle rules
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
        limit = CFG["tight_short"] if Lb <= 10 else \
            CFG["tight_short"] + (CFG["tight_long"] - CFG["tight_short"]) * (Lb - 10) / (CFG["max_base"] - 10)
        if depth > limit:
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

    if c <= R * (1 + CFG["breakout_buffer"]):        # must CLOSE above resistance
        return None
    ext = (c - R) / R
    if ext > CFG["max_ext_above_res"] or c > sma50 * (1 + CFG["max_ext_sma50"]):
        return None                                  # late entry

    # --- no immediate rejection
    if off == 1:
        n = b + 1
        if C[n] < R or L[n] < R * (1 - CFG["hold_tol"]):
            return None
        status = "Confirmed (held next day)"
    else:
        status = "Fresh - confirm hold next session"

    # --- pressure build-up
    lo, hi, vb = L[s:b], H[s:b], V[s:b]
    third = max(1, Lb // 3)
    higher_lows = Lb >= 5 and _slope(lo) > 0 and lo[-third:].min() > lo[:third].min()
    vol_rising = Lb >= 4 and _slope(vb / avgv) > 0
    atr_start = d["ATR14"].iloc[s - 1]
    contraction = np.isfinite(atr_start) and atr_start > 0 and (hi - lo).mean() / atr_start < 0.8
    tests = resistance_tests(H, b, R)

    # --- context & pattern
    sma50_prev = d["SMA50"].iloc[b - 10]
    uptrend = c > sma50 > sma200 and sma50 > sma50_prev
    w0 = max(0, b - 220)
    ih = w0 + int(np.argmax(H[w0:b]))
    il = ih + int(np.argmin(L[ih:b])) if ih < b else ih
    drawdown = (H[ih] - L[il]) / H[ih]
    sma200_il = d["SMA200"].iloc[il]
    sma50_il = d["SMA50"].iloc[il]
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
        flags.append("no clear trend/bottom context")

    # late-entry check on the run-up into the base (flags excluded: a pole is expected)
    r0 = max(0, s - 60)
    runup = (low - L[r0:s].min()) / L[r0:s].min() if s > r0 else 0
    late_runup = runup > 0.5 and pattern != "Bull flag"
    if late_runup:
        flags.append("big run-up before base")
    if vol_ratio < CFG["strong_vol_ratio"]:
        flags.append("volume < 2x")

    # --- score (0-100ish)
    limit = CFG["tight_short"] if Lb <= 10 else \
        CFG["tight_short"] + (CFG["tight_long"] - CFG["tight_short"]) * (Lb - 10) / (CFG["max_base"] - 10)
    score = 0.0
    score += min(vol_ratio / CFG["strong_vol_ratio"], 1.25) * 20
    score += (1 - wick) * 8 + close_pos * 5
    score += min(range_atr / 2, 1) * 8
    score += max(0, 1 - depth / limit) * 8
    score += min(touches, 4) / 4 * 6 + min(tests, 3) / 3 * 6
    score += 5 * higher_lows + 4 * vol_rising + 4 * contraction
    score += {"Bottoming": 15, "Continuation": 8, "Other": 0}[category]
    score += 4 if ("Double" in pattern or "Rounded" in pattern) else 0
    score += 8 if off == 1 else 0
    score -= 10 if late_runup else 0
    score -= ext / CFG["max_ext_above_res"] * 5

    return dict(
        Score=round(score, 1), Category=category, Pattern=pattern, Status=status,
        BreakoutDate=pd.Timestamp(d.index[b]).date().isoformat(),
        Close=round(C[-1], 2), Resistance=round(R, 2),
        PctAboveRes=round((C[-1] - R) / R * 100, 2),
        VolX=round(vol_ratio, 2), RangeATR=round(range_atr, 2),
        UpperWickPct=round(wick * 100, 1), BaseLen=Lb, BaseDepthPct=round(depth * 100, 1),
        Touches=touches, ResTests120d=tests, HigherLows=bool(higher_lows),
        VolRisingInBase=bool(vol_rising), RangeContraction=bool(contraction),
        BaseLow=round(low, 2), ATR=round(atr, 2),
        BaseStart=pd.Timestamp(d.index[s]).date().isoformat(),
        Flags="; ".join(flags), _b=b, _s=s,
    )


def analyze(d):
    if len(d) < 215:
        return None
    best = None
    for off in (0, 1):
        r = check_breakout(d, len(d) - 1 - off, off)
        if r and (best is None or r["Score"] > best["Score"]):
            best = r
    return best

# --------------------------------------------------------------------------
# Signal tracker: follows every setup from day to day and gives an action
# --------------------------------------------------------------------------
TRADE = dict(
    buy_zone=0.04,        # buy only within 4% above resistance (same as the "extended" cutoff)
    extended=0.04,        # > 4% above resistance after confirmation = don't chase
    extended_days=10,     # give up on an extended name that never pulls back
    stop_atr=1.0,         # stop = max(base low, resistance - 1 ATR), at least 1.5% under resistance
    min_rr=2.0,           # target is at least 2x the risk
    trail_atr=2.0,        # trailing stop after the target: highest close - 2 ATR
    time_stop_days=15,    # sell if no follow-through after 15 sessions
    time_stop_gain=0.02,
)

TRACK_COLS = ["Ticker", "Indexes", "Category", "Pattern", "Score", "BreakoutDate", "BaseStart",
              "Resistance", "BaseLow", "ATR", "VolX", "BaseLen", "Flags", "Status", "StatusSince", "Stop", "Target",
              "EntryDate", "EntryPrice", "LastClose", "HighClose", "PnLPct", "LastDate", "Note", "Closed"]
ACTIVE = {"WATCH", "BUY", "EXTENDED", "HOLD", "TAKE PROFIT", "TRAIL"}

STATUS_INFO = {
    "BUY":         ("Buy", "#16794a", "Breakout confirmed: it held the day after. Entry plan below."),
    "WATCH":       ("Watch", "#1d5fbf", "Fresh breakout today. Needs to hold above the breakout level tomorrow before it becomes a Buy."),
    "EXTENDED":    ("Don't chase", "#a15c00", "Breakout held but price ran too far above the breakout level. Wait for a pullback."),
    "HOLD":        ("Hold", "#3d4a5c", "You own it (if you took the Buy). Keep the stop in place."),
    "TAKE PROFIT": ("Take profit", "#7a3db8", "Target hit. Sell part, protect the rest."),
    "TRAIL":       ("Hold - trailing", "#3d4a5c", "Holding the remainder with a stop that rises with the price."),
    "SELL":        ("Sell", "#b42318", "Exit the position."),
    "DROP":        ("Drop", "#6b6b6b", "Setup failed before entry. Remove from watchlist."),
}


def _dates(d):
    return np.array([pd.Timestamp(i).date() for i in d.index])


def _levels(R, base_low, atr, entry):
    stop = max(base_low, R - TRADE["stop_atr"] * atr)
    stop = min(stop, R * 0.985)
    risk = max(entry - stop, entry * 0.005)
    target = max(R + (R - base_low), entry + TRADE["min_rr"] * risk)
    return round(stop, 2), round(target, 2)


def _set(row, status, date, note):
    row["Status"], row["StatusSince"], row["Note"] = status, date, note
    if status in ("SELL", "DROP"):
        row["Closed"] = True


def _start_buy(row, date, close, note):
    row["EntryDate"], row["EntryPrice"] = date, round(close, 2)
    row["Stop"], row["Target"] = _levels(row["Resistance"], row["BaseLow"], row["ATR"], close)
    row["HighClose"] = round(close, 2)
    _set(row, "BUY", date, note)


def step_row(row, date, o, h, l, c, n_held):
    """Advance one tracked setup by one daily candle."""
    R, st = row["Resistance"], row["Status"]
    row["LastClose"], row["LastDate"] = round(c, 2), date
    if row.get("EntryPrice") and np.isfinite(row["EntryPrice"]):
        row["HighClose"] = round(max(row.get("HighClose") or c, c), 2)
        row["PnLPct"] = round((c / row["EntryPrice"] - 1) * 100, 2)

    if st == "WATCH":
        if c < R or l < R * (1 - CFG["hold_tol"]):
            _set(row, "DROP", date, f"Failed to hold the breakout: closed {c:.2f}, low {l:.2f} vs level {R:.2f}")
        elif c > R * (1 + TRADE["extended"]):
            _set(row, "EXTENDED", date, f"Held, but closed {100 * (c / R - 1):.1f}% above the breakout level")
        else:
            _start_buy(row, date, c, "Held above the breakout level the next day")
    elif st == "EXTENDED":
        if c < R * (1 - CFG["hold_tol"]):
            _set(row, "DROP", date, "Fell back below the breakout level")
        elif c <= R * (1 + TRADE["buy_zone"]) and l >= R * (1 - CFG["hold_tol"]):
            _start_buy(row, date, c, "Pulled back to the breakout level and held")
        elif n_held >= TRADE["extended_days"]:
            _set(row, "DROP", date, "Never pulled back - opportunity missed")
    elif st in ("BUY", "HOLD"):
        if c < row["Stop"]:
            _set(row, "SELL", date, f"Closed below the stop ({row['Stop']:.2f})")
        elif h >= row["Target"]:
            row["Stop"] = round(max(row["Stop"], row["EntryPrice"]), 2)
            _set(row, "TAKE PROFIT", date, f"Hit target {row['Target']:.2f}")
        elif n_held >= TRADE["time_stop_days"] and c < row["EntryPrice"] * (1 + TRADE["time_stop_gain"]):
            _set(row, "SELL", date, f"Time stop: no follow-through after {n_held} sessions")
        elif st == "BUY":
            _set(row, "HOLD", date, "Breakout working - stop unchanged")
    elif st in ("TAKE PROFIT", "TRAIL"):
        trail = row["HighClose"] - TRADE["trail_atr"] * row["ATR"]
        row["Stop"] = round(max(row["Stop"], trail), 2)
        if c < row["Stop"]:
            _set(row, "SELL", date, f"Trailing stop hit ({row['Stop']:.2f})")
        elif st == "TAKE PROFIT":
            _set(row, "TRAIL", date, "Riding the rest with a trailing stop")


def load_tracker(path):
    if os.path.exists(path):
        tr = pd.read_csv(path, dtype={"Ticker": str})
        for col in TRACK_COLS:
            if col not in tr.columns:
                tr[col] = np.nan
        tr["Closed"] = tr["Closed"].fillna(False).astype(bool)
        return tr[TRACK_COLS].to_dict("records")
    return []


def update_tracker(rows, res, data, asof):
    """Advance open setups with every new candle, then add today's new setups."""
    for row in rows:
        if row["Closed"] or row["Ticker"] not in data:
            continue
        d = data[row["Ticker"]]
        dts = _dates(d)
        last = pd.Timestamp(row["LastDate"]).date()
        for i in np.where(dts > last)[0]:
            if row["Closed"]:
                break
            since = pd.Timestamp(row["EntryDate"] if row["Status"] not in ("WATCH", "EXTENDED") and isinstance(row["EntryDate"], str)
                                 else row["StatusSince"]).date()
            n_held = int(((dts > since) & (dts <= dts[i])).sum())
            step_row(row, dts[i].isoformat(), *d[["Open", "High", "Low", "Close"]].iloc[i].tolist(), n_held)

    open_now = {r["Ticker"] for r in rows if not r["Closed"]}
    for _, r in res.iterrows():
        if r["Ticker"] in open_now:
            continue
        row = {k: np.nan for k in TRACK_COLS}
        row.update({k: r[k] for k in ["Ticker", "Indexes", "Category", "Pattern", "Score", "BreakoutDate",
                                      "BaseStart", "Resistance", "BaseLow", "ATR", "VolX", "BaseLen", "Flags"]})
        row.update(LastClose=r["Close"], LastDate=asof, Closed=False)
        if r["Status"].startswith("Fresh"):
            _set(row, "WATCH", asof, f"Broke out on {r['VolX']}x volume")
        elif r["Close"] > r["Resistance"] * (1 + TRADE["extended"]):
            _set(row, "EXTENDED", asof, f"Breakout held but closed {r['PctAboveRes']}% above the level")
        else:
            _start_buy(row, asof, r["Close"], "Broke out and held the next day")
        rows.append(row)
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


def instruction(r):
    st, R = r["Status"], r["Resistance"]
    if st == "BUY":
        hi = R * (1 + TRADE["buy_zone"])
        return (f"Buy at tomorrow's open if it opens between <b>{_f(R)}</b> and <b>{_f(hi)}</b>. "
                f"Skip if it gaps above {_f(hi)} or opens below {_f(R)}. "
                f"Stop <b>{_f(r['Stop'])}</b> ({_pct(r['Stop'], r['EntryPrice'])}), "
                f"target <b>{_f(r['Target'])}</b> ({_pct(r['Target'], r['EntryPrice'])}).")
    if st == "WATCH":
        return (f"Broke above <b>{_f(R)}</b> today on {r['VolX']}x normal volume. Don't buy yet: tomorrow it must "
                f"close above {_f(R)} without trading below {_f(R * (1 - CFG['hold_tol']))}. If it does, it becomes a Buy.")
    if st == "EXTENDED":
        return (f"The breakout held, but price is {_pct(r['LastClose'], R)} above the breakout level. "
                f"Don't chase it. Buy only if it pulls back to <b>{_f(R)}-{_f(R * (1 + TRADE['buy_zone']))}</b> and holds.")
    if st == "HOLD":
        return (f"In the trade since {r['EntryDate']} at {_f(r['EntryPrice'])} (<b>{r['PnLPct']:+.1f}%</b>). "
                f"Keep the stop at <b>{_f(r['Stop'])}</b>; target <b>{_f(r['Target'])}</b>.")
    if st == "TAKE PROFIT":
        return (f"Hit the {_f(r['Target'])} target (<b>{r['PnLPct']:+.1f}%</b>). Sell about half. "
                f"Move the stop on the rest to <b>{_f(r['Stop'])}</b> (your entry), so it can't turn into a loss.")
    if st == "TRAIL":
        return (f"Holding the remainder, <b>{r['PnLPct']:+.1f}%</b> since {r['EntryDate']}. "
                f"Trailing stop now <b>{_f(r['Stop'])}</b>; it rises as the price does.")
    if st == "SELL":
        res = f" Result <b>{r['PnLPct']:+.1f}%</b> from {_f(r['EntryPrice'])}." if pd.notna(r.get("PnLPct")) else ""
        return f"Sell at tomorrow's open. {r['Note']}.{res}"
    if st == "DROP":
        return f"Take it off the watchlist. {r['Note']}."
    return r.get("Note", "")


def card_html(r, chart=None, compact=False):
    label, color, _ = STATUS_INFO.get(r["Status"], (r["Status"], "#555", ""))
    new = r["StatusSince"] == r["LastDate"] and r["Status"] not in ("HOLD", "TRAIL")
    flags = [f for f in str(r.get("Flags") or "").split("; ") if f and f != "nan"]
    facts = [("Breakout level", _f(r["Resistance"])), ("Stop", _f(r.get("Stop"))),
             ("Target", _f(r.get("Target"))), ("Volume", f"{r['VolX']}x avg"),
             ("Breakout", str(r["BreakoutDate"])), ("Score", _f(r["Score"], 0))]
    facts_html = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in facts)
    flag_html = "".join(f'<span class="flag">{f}</span>' for f in flags)
    chart_html = (f'<details><summary>Chart</summary><img alt="{r["Ticker"]} chart" '
                  f'src="data:image/png;base64,{chart}"/></details>') if chart else ""
    return f"""<article class="card" id="t-{r['Ticker']}" style="--c:{color}">
  <header><div><h3>{r['Ticker']}</h3><p class="sub">{r['Pattern']} &middot; {r['Indexes']}</p></div>
  <div class="right"><span class="badge">{label}{' &middot; new' if new else ''}</span><p class="px">{_f(r['LastClose'])}</p></div></header>
  <p class="do">{instruction(r)}</p>
  {'' if compact else f'<div class="facts">{facts_html}</div>'}
  {flag_html and f'<div class="flags">{flag_html}</div>'}
  {chart_html}
</article>"""


def write_html(path, asof, rows, res, data, ctx, n_scanned):
    asof_txt = pd.Timestamp(asof).strftime("%a %b %d, %Y")
    act = [r for r in rows if not r["Closed"] or r["StatusSince"] == asof]
    order = {"SELL": 0, "TAKE PROFIT": 1, "BUY": 2, "WATCH": 3, "EXTENDED": 4, "HOLD": 5, "TRAIL": 6, "DROP": 7}
    act.sort(key=lambda r: (order.get(r["Status"], 9), -float(r["Score"] or 0)))

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
        return (f'<tr><td class="tk"><a href="#t-{r["Ticker"]}">{r["Ticker"]}</a></td>'
                f'<td><span class="chip" style="--c:{color}">{label}</span></td>'
                f'<td>{r["Pattern"]}</td><td>{r["Category"]}</td><td>{_f(r["LastClose"])}</td>'
                f'<td>{_f(r["Resistance"])}</td><td>{above:+.1f}%</td><td>{r["VolX"]}x</td><td>{base}</td>'
                f'<td>{_f(r.get("Stop"))}</td><td>{_f(r.get("Target"))}</td>{pnl_td}'
                f'<td>{r["BreakoutDate"]}</td><td>{_f(r["Score"], 0)}</td><td>{r["Indexes"]}</td></tr>')

    summary = ("<section><h2>Summary</h2><p class=\"hint\">Every setup being tracked. Tap a ticker for its card and chart; "
               "swipe the table sideways for more columns.</p><div class=\"wrap\"><table class=\"sum\"><tr>"
               "<th class=\"tk\">Ticker</th><th>Action</th><th>Pattern</th><th>Type</th><th>Close</th>"
               "<th>Breakout level</th><th>Above level</th><th>Volume</th><th>Base</th><th>Stop</th><th>Target</th>"
               "<th>P&amp;L</th><th>Breakout date</th><th>Score</th><th>Index</th></tr>"
               + "".join(sum_row(r) for r in act) + "</table></div></section>") if act else ""

    cols = ["Ticker", "Score", "Category", "Pattern", "Status", "Close", "Resistance", "PctAboveRes", "VolX",
            "RangeATR", "UpperWickPct", "BaseLen", "BaseDepthPct", "Touches", "ResTests120d", "HigherLows",
            "VolRisingInBase", "Flags"]
    raw_tbl = res[cols].to_html(index=False, border=0) if len(res) else "<p>No new setups today.</p>"
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
</style></head><body><main>
<h1>Breakout Scan</h1>
<p class="hint">Based on the close of <b>{asof_txt}</b> &middot; {n_scanned:,} stocks scanned &middot; {len(res)} new setups</p>
<div class="chips">{chips}</div>
<div class="mkts">{mkt}</div>
{summary}
{section("Act at tomorrow's open", "New buys, profit-taking and exits.", do_now, empty="No action needed tomorrow.")}
{section("Watchlist", "Broke out but not confirmed yet, or too far above the breakout level to buy.", watch, empty="Nothing on watch.")}
{section("Open positions", "Setups that triggered a Buy and are still running.", holding, compact=True, empty="No open positions.")}
{closed_sec}
<section><h2>What the labels mean</h2><ul class="gl">{glossary}</ul>
<p class="hint">Entry, stop and target are calculated from the breakout level. Profit/loss is measured from the close on the day of the Buy signal; your real entry is the next open.</p></section>
<details><summary>Full scan details ({len(res)} setups)</summary><div class="wrap">{raw_tbl}</div></details>
<details><summary>Scan diagnostics</summary><ul class="hint">{''.join(f"<li>{x}</li>" for x in DIAG)}</ul></details>
<footer>Technical screen only - not investment advice. <a href="archive.html">Past scans</a></footer>
</main></body></html>"""
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
    r = analyze(d0)
    r["Ticker"], r["Indexes"] = "TEST", "Test"
    res = pd.DataFrame([r])
    asof = d0.index[-1].date().isoformat()
    R = r["Resistance"]

    def add(df, o, h, l, c, days=1):
        idx = pd.bdate_range(df.index[-1] + pd.Timedelta(days=1), periods=days)
        new = pd.DataFrame(dict(Open=[o] * days, High=[h] * days, Low=[l] * days, Close=[c] * days,
                                Volume=[1.5e6] * days), index=idx)
        return pd.concat([df, new])

    def run(path_bars):
        rows = update_tracker([], res, {"TEST": d0}, asof)
        df, seq = base, [rows[0]["Status"]]
        for bar in path_bars:
            df = add(df, *bar)
            rows = update_tracker(rows, pd.DataFrame(), {"TEST": prep(df)}, df.index[-1].date().isoformat())
            seq.append(rows[0]["Status"])
        return seq, rows[0]

    ok = True
    cases = [
        ("hold, then stop out", [(R * 1.01, R * 1.02, R * 0.999, R * 1.015), (R * 1.0, R * 1.0, R * 0.95, R * 0.95)],
         ["WATCH", "BUY", "SELL"]),
        ("failed hold", [(R * 1.0, R * 1.01, R * 0.97, R * 0.98)], ["WATCH", "DROP"]),
        ("target, then trail", [(R * 1.01, R * 1.02, R * 0.999, R * 1.015), (R * 1.03, R * 1.04, R * 1.02, R * 1.035),
                                (R * 1.10, R * 1.25, R * 1.09, R * 1.22), (R * 1.22, R * 1.24, R * 1.20, R * 1.23),
                                (R * 1.1, R * 1.1, R * 1.0, R * 1.02)],
         ["WATCH", "BUY", "HOLD", "TAKE PROFIT", "TRAIL", "SELL"]),
    ]
    for name, bars, want in cases:
        seq, last = run(bars)
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
        r = analyze(d)
        print(f"{kind:<8}: " + (f"{r['Category']} | {r['Pattern']} | vol {r['VolX']}x | base {r['BaseLen']} | score {r['Score']}" if r else "NOT DETECTED"))
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
    a = ap.parse_args()

    if a.selftest:
        sys.exit(0 if selftest() else 1)

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
    raw = download(sorted(set(tickers) | set(ctx_t) | tracked))
    DIAG.append(f"Universe {len(tickers)} unique tickers; price data received for {len(raw)}")
    data = {}
    for t, d in raw.items():
        try:
            data[t] = prep(drop_live_bar(d))
        except Exception:
            pass

    results = []
    for t in tickers:
        if t not in data:
            continue
        try:
            r = analyze(data[t])
        except Exception:
            r = None
        if r:
            r["Ticker"] = t
            r["Indexes"] = ", ".join(sorted(membership[t]))
            results.append(r)

    res = pd.DataFrame(results)
    if len(res):
        res = res.sort_values("Score", ascending=False).head(a.top).reset_index(drop=True)

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
