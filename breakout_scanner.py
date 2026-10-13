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


def get_djia():
    t = _wiki_tables("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
    return [_us(s) for s in _pick_col(t, ["Symbol"], 25, 35)]


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
    df = pd.read_csv(StringIO(r.text), sep="|")
    df = df[~df["Symbol"].astype(str).str.startswith("File Creation")]
    df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
    bad = df["Security Name"].astype(str).str.contains(
        r"Warrant|\bUnits?\b|\bRights?\b|Preferred|Notes due|Debenture", case=False, regex=True)
    df = df[~bad]
    syms = df["Symbol"].astype(str)
    syms = syms[~syms.str.contains(r"[\$\.\^]", regex=True)]
    return [_us(s) for s in syms]


def get_r2000():
    url = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
           "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    lines = r.text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    df = pd.read_csv(StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    syms = df["Ticker"].astype(str).str.strip()
    syms = syms[syms.str.match(r"^[A-Z][A-Z0-9 .\-]*$")]
    return [_us(s) for s in syms]


FETCHERS = dict(tsx=get_tsx, tsx60=get_tsx60, sp500=get_sp500,
                djia=get_djia, nasdaq=get_nasdaq, r2000=get_r2000)


def build_universe(index_keys):
    membership = {}
    for k in index_keys:
        try:
            syms = FETCHERS[k]()
            print(f"  {INDEX_NAMES[k]:<20} {len(syms):>5} tickers")
        except Exception as e:
            print(f"  {INDEX_NAMES[k]:<20} FAILED ({e}) - skipped")
            continue
        for s in syms:
            membership.setdefault(s, set()).add(INDEX_NAMES[k])
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
# Report
# --------------------------------------------------------------------------
def chart_png(t, d, r, bars=120):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n0 = max(0, len(d) - bars)
    w = d.iloc[n0:]
    x = np.arange(len(w))
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(8, 4.6), sharex=True,
                                  gridspec_kw=dict(height_ratios=[3, 1], hspace=0.05))
    up = w["Close"] >= w["Open"]
    col = np.where(up, "#1a9e5f", "#d1453b")
    ax.vlines(x, w["Low"], w["High"], color=col, lw=0.8)
    ax.bar(x, (w["Close"] - w["Open"]).abs().clip(lower=1e-9), bottom=w[["Open", "Close"]].min(axis=1),
           color=col, width=0.7)
    ax.plot(x, w["SMA50"], color="#4a6fd1", lw=1, label="SMA50")
    ax.plot(x, w["SMA200"], color="#999", lw=1, label="SMA200")
    ax.axhline(r["Resistance"], color="#e08a00", lw=1.2, ls="--", label="Resistance")
    s, b = r["_s"] - n0, r["_b"] - n0
    ax.axvspan(s - 0.5, b - 0.5, color="#e08a00", alpha=0.12)
    ax.set_title(f"{t}  -  {r['Pattern']}  ({r['Status']})", fontsize=10)
    ax.legend(fontsize=7, loc="upper left")
    axv.bar(x, w["Volume"], color=col, width=0.7)
    axv.plot(x, w["AvgVol50"], color="#333", lw=1)
    axv.set_yticks([])
    step = max(1, len(w) // 6)
    axv.set_xticks(x[::step])
    axv.set_xticklabels([pd.Timestamp(i).strftime("%b %d") for i in w.index[::step]], fontsize=7)
    ax.tick_params(labelsize=7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90, bbox_inches="tight")
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
        trend = "Uptrend" if c > s50 > s200 else ("Downtrend" if c < s50 < s200 else "Mixed")
        rows.append((name, t, round(c, 2), trend))
    return rows


def write_html(path, res, data, ctx, n_scanned, n_charts):
    today = dt.date.today().isoformat()
    cols = ["Ticker", "Indexes", "Score", "Category", "Pattern", "Status", "BreakoutDate", "Close",
            "Resistance", "PctAboveRes", "VolX", "RangeATR", "UpperWickPct", "BaseLen",
            "BaseDepthPct", "Touches", "ResTests120d", "HigherLows", "VolRisingInBase",
            "BaseLow", "Flags"]
    table = res[cols].to_html(index=False, classes="t", border=0) if len(res) else "<p>No setups passed every rule.</p>"
    ctx_html = "".join(f"<li><b>{n}</b> ({t}) {c} - {tr}</li>" for n, t, c, tr in ctx)
    charts = []
    for _, r in res.head(n_charts).iterrows():
        try:
            png = chart_png(r["Ticker"], data[r["Ticker"]], r)
            charts.append(f'<figure><img src="data:image/png;base64,{png}"/>'
                          f'<figcaption>{r["Ticker"]}: vol {r["VolX"]}x, base {r["BaseLen"]} candles / '
                          f'{r["BaseDepthPct"]}% deep, resistance {r["Resistance"]}, base low {r["BaseLow"]}'
                          f'{" - " + r["Flags"] if r["Flags"] else ""}</figcaption></figure>')
        except Exception as e:
            charts.append(f"<p>{r['Ticker']}: chart failed ({e})</p>")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Breakout Scan {today}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:16px;color:#1d1d1f;background:#fff}}
h1{{font-size:20px}} .t{{border-collapse:collapse;font-size:12px;width:100%}}
.t th,.t td{{padding:4px 6px;border-bottom:1px solid #e5e5e5;text-align:left;white-space:nowrap}}
.t th{{background:#f4f4f6;position:sticky;top:0}} .wrap{{overflow-x:auto}}
figure{{margin:16px 0}} img{{max-width:100%}} figcaption{{font-size:12px;color:#555}}
.note{{font-size:12px;color:#666}}
</style></head><body>
<h1>Breakout Scan - {today}</h1>
<p class="note">{n_scanned} tickers scanned, {len(res)} passed every rule. Ranked by score
(bottoming breakouts weighted highest). "Fresh" = breakout on the last candle, still needs to hold;
"Confirmed" = broke out the session before and held. Technical screen only - not investment advice.</p>
<h3>Market context</h3><ul>{ctx_html}</ul>
<h3>Setups</h3><div class="wrap">{table}</div>
<h3>Charts (top {min(n_charts, len(res))})</h3>{''.join(charts)}
</body></html>"""
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
    ap.add_argument("--charts", type=int, default=12, help="charts drawn for the top N")
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

    ctx_t = ["SPY", "DIA", "QQQ", "IWM", "XIU.TO"]
    raw = download(sorted(set(tickers) | set(ctx_t)))
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

    os.makedirs(a.outdir, exist_ok=True)
    stamp = dt.date.today().isoformat()
    csv_path = os.path.join(a.outdir, f"breakouts_{stamp}.csv")
    html_path = os.path.join(a.outdir, f"breakouts_{stamp}.html")
    (res.drop(columns=["_b", "_s"]) if len(res) else res).to_csv(csv_path, index=False)
    write_html(html_path, res, data, market_context(data), len(data), a.charts)

    print(f"\n{len(res)} setups passed every rule (scanned {len(data)} tickers in {time.time() - t0:.0f}s)\n")
    if len(res):
        show = ["Ticker", "Score", "Category", "Pattern", "Status", "Close", "Resistance", "VolX", "BaseLen"]
        print(res[show].head(20).to_string(index=False))
    print(f"\nReport: {os.path.abspath(html_path)}\nCSV:    {os.path.abspath(csv_path)}")
    if a.open:
        webbrowser.open("file://" + os.path.abspath(html_path))


if __name__ == "__main__":
    main()
