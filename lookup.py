"""
Ticker lookup: a full page on one stock or ETF, started from the search box on the report.

    python lookup.py --outdir reports --ticker SHOP.TO        (or set the TICKER environment variable)

Writes reports/lookup/<TICKER>.html with:
  - what the company does, industry, size, next earnings (Yahoo Finance profile)
  - earnings and financial health: revenue / profit history, margins, debt, cash flow, earnings surprises
  - valuation and analyst consensus, recent headlines
  - breakout analysis with the scanner's exact rules and 100-point scorecard (breakout_scanner.py)
  - a long-term investment scorecard built from the books (BOOK_RULES.md):
      trend        [Murphy ch8-10, ch19: trade with the long-term (weekly/monthly) trend; Gann rule 16 via Allemann]
      strength     [Murphy ch17: buy the strongest stocks, avoid falling relative-strength lines]
      growth, profitability, valuation   (standard fundamental analysis; Murphy ch1 places it beside technicals)
      balance sheet [Oygard ch2-5: leverage and credit booms are what break companies and countries]
      risk         [Hougaard, Douglas ch11: size for the drawdowns you will live through; Gann rule 4: liquid markets]
  - how to buy it if you want it [Murphy ch16: in uptrends buy the dips; Hougaard: add in smaller layers]
Technical screen and public data only - not investment advice.
"""
import argparse
import base64
import datetime as dt
import html
import io
import os
import re
import sys

import numpy as np
import pandas as pd

import breakout_scanner as bs

e = html.escape
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


def clean(t):
    t = (t or "").strip().upper().replace(" ", "")
    return t if TICKER_RE.match(t) else ""


def _n(x):
    try:
        x = float(x)
        return x if np.isfinite(x) else np.nan
    except (TypeError, ValueError):
        return np.nan


def big(x, cur=""):
    x = _n(x)
    if not np.isfinite(x):
        return "-"
    for d, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= d:
            return f"{cur}{x / d:,.1f}{s}"
    return f"{cur}{x:,.0f}"


def pct(x, mult=100, nd=1, sign=False):
    x = _n(x)
    return "-" if not np.isfinite(x) else (f"{x * mult:+.{nd}f}%" if sign else f"{x * mult:.{nd}f}%")


# ------------------------------------------------------------------ data
def fetch(t):
    """Price history (5 years) for the ticker; tries the TSX listing if the bare symbol has no data."""
    for cand in ([t] if "." in t or "=" in t or "^" in t else [t, t + ".TO"]):
        raw = bs.download([cand], period="5y")
        if cand in raw and len(raw[cand]) > 30:
            return cand, raw[cand]
    return None, None


def fundamentals(t):
    import yfinance as yf
    tk = yf.Ticker(t)
    out = {"info": bs._retry(lambda: tk.get_info() or {}) or {}}
    for name, fn in [("inc", lambda: tk.income_stmt), ("qinc", lambda: tk.quarterly_income_stmt),
                     ("cf", lambda: tk.cashflow), ("bal", lambda: tk.balance_sheet),
                     ("ehist", lambda: tk.get_earnings_history()), ("cal", lambda: tk.calendar),
                     ("news", lambda: tk.news), ("recs", lambda: tk.recommendations_summary)]:
        try:
            out[name] = fn()
        except Exception:
            out[name] = None
    return out


def row(df, *names):
    if df is None or not hasattr(df, "index") or not len(df):
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce").dropna()
            if len(s):
                return s.sort_index()
    return None


def cagr(s):
    if s is None or len(s) < 2 or s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return np.nan
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 1)
    return (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1


# ------------------------------------------------------------------ analysis
def long_term(d, bench, info, F, is_fund):
    """Long-term scorecard out of 100 (ETFs: trend, strength and risk only, rescaled)."""
    c = d["Close"]
    last = c.iloc[-1]
    s50, s200 = d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
    s200_prev = d["SMA200"].iloc[-21] if len(d) > 230 else np.nan
    hi52 = d["High"].iloc[-252:].max()
    parts, notes = {}, []

    # trend [Murphy ch8-10, ch19; Gann rule 16]
    p = 0
    p += 6 if last > s200 else 0
    p += 5 if np.isfinite(s200_prev) and s200 > s200_prev else 0
    p += 4 if s50 > s200 else 0
    p += 3 if d["WMACDH"].iloc[-1] > 0 else 0
    p += 2 if last >= 0.75 * hi52 else 0
    parts["Long-term trend"] = (p, 20, "Murphy ch8-10, 19; Gann rule 16")
    notes.append(("Trend", f"Price {bs._f(last)} vs 200-day {bs._f(s200)} ({'rising' if np.isfinite(s200_prev) and s200 > s200_prev else 'falling'}), "
                           f"50-day {bs._f(s50)}; {(last / hi52 - 1) * 100:+.1f}% from the 52-week high."))

    # relative strength [Murphy ch17]
    rs = np.nan
    if bench is not None and len(bench) > 252 and len(c) > 252:
        b = bench["Close"].reindex(c.index).ffill()
        rs = (c.iloc[-1] / c.iloc[-252] - 1) - (b.iloc[-1] / b.iloc[-252] - 1)
    p = (15 if rs > 0.10 else 10 if rs > 0 else 5 if rs > -0.10 else 0) if np.isfinite(rs) else 7
    parts["Strength vs market"] = (p, 15, "Murphy ch17")
    if np.isfinite(rs):
        notes.append(("Strength", f"Over 1 year it {'beat' if rs > 0 else 'lagged'} its market index by {abs(rs) * 100:.1f} points."))

    # risk [Hougaard; Douglas ch11; Gann rule 4]
    c5 = c.iloc[-1260:]
    mdd = float((c5 / c5.cummax() - 1).min())
    beta = _n(info.get("beta") or info.get("beta3Year"))
    dv = (d["Close"] * d["Volume"]).iloc[-50:].mean()
    p = (5 if mdd > -0.30 else 3 if mdd > -0.50 else 0) + ((3 if beta < 1.2 else 2 if beta < 1.6 else 0) if np.isfinite(beta) else 1.5) \
        + (2 if dv > 5e6 else 1 if dv > 1e6 else 0)
    parts["Risk & liquidity"] = (p, 10, "Hougaard; Douglas ch11; Gann rule 4")
    notes.append(("Risk", f"Worst fall in 5 years: {mdd * 100:.0f}%. Beta {beta:.2f}." if np.isfinite(beta)
                  else f"Worst fall in 5 years: {mdd * 100:.0f}%."))

    if not is_fund:
        rev, ni = row(F.get("inc"), "Total Revenue", "Operating Revenue"), row(F.get("inc"), "Net Income", "Net Income Common Stockholders")
        rg, eg = cagr(rev), _n(info.get("earningsGrowth"))
        if not np.isfinite(eg):
            eg = cagr(ni)
        p = ((8 if rg >= 0.15 else 5 if rg >= 0.07 else 2 if rg >= 0 else 0) if np.isfinite(rg) else 3) + \
            ((7 if eg >= 0.15 else 4 if eg >= 0.05 else 2 if eg >= 0 else 0) if np.isfinite(eg) else 3)
        parts["Growth"] = (p, 15, "fundamentals")
        notes.append(("Growth", f"Revenue {pct(rg)} a year over {len(rev) if rev is not None else 0} reported years; "
                                f"latest earnings growth {pct(eg)}."))

        om, roe = _n(info.get("operatingMargins")), _n(info.get("returnOnEquity"))
        fcf = row(F.get("cf"), "Free Cash Flow")
        fcf_last = fcf.iloc[-1] if fcf is not None else _n(info.get("freeCashflow"))
        p = ((6 if om >= 0.20 else 4 if om >= 0.10 else 2 if om > 0 else 0) if np.isfinite(om) else 3) + \
            ((5 if roe >= 0.15 else 3 if roe >= 0.08 else 1 if roe > 0 else 0) if np.isfinite(roe) else 2) + \
            ((4 if fcf_last > 0 else 0) if np.isfinite(_n(fcf_last)) else 2)
        parts["Profitability"] = (p, 15, "fundamentals")
        notes.append(("Profitability", f"Operating margin {pct(om)}, return on equity {pct(roe)}, "
                                       f"free cash flow {big(fcf_last)}."))

        de, cash, debt, cr = _n(info.get("debtToEquity")), _n(info.get("totalCash")), _n(info.get("totalDebt")), _n(info.get("currentRatio"))
        p = ((8 if de < 50 else 5 if de < 100 else 2 if de < 200 else 0) if np.isfinite(de) else 4) + \
            ((4 if cash > debt else 0) if np.isfinite(cash) and np.isfinite(debt) else 2) + \
            ((3 if cr >= 1.2 else 1 if cr >= 1 else 0) if np.isfinite(cr) else 1.5)
        parts["Balance sheet"] = (p, 15, "Oygard ch2-5 (leverage)")
        notes.append(("Balance sheet", f"Debt/equity {de:.0f}%, cash {big(cash)} vs debt {big(debt)}." if np.isfinite(de)
                      else f"Cash {big(cash)} vs debt {big(debt)}."))

        fpe, tpe, peg = _n(info.get("forwardPE")), _n(info.get("trailingPE")), _n(info.get("trailingPegRatio") or info.get("pegRatio"))
        pe = fpe if np.isfinite(fpe) and fpe > 0 else tpe
        if np.isfinite(peg) and 0 < peg <= 1.5:
            p = 10
        elif np.isfinite(pe) and pe > 0:
            p = 8 if pe < 15 else 6 if pe < 25 else 3 if pe < 40 else 0
        else:
            p = 0 if np.isfinite(_n(info.get("trailingEps"))) and _n(info.get("trailingEps")) < 0 else 4
        parts["Valuation"] = (p, 10, "fundamentals")
        val_txt = (f"Forward P/E {fpe:.1f}" if np.isfinite(fpe) else "Forward P/E -") \
            + (f", trailing P/E {tpe:.1f}" if np.isfinite(tpe) else "") + (f", PEG {peg:.2f}" if np.isfinite(peg) else "") + "."
        notes.append(("Valuation", val_txt))

    got, mx = sum(v[0] for v in parts.values()), sum(v[1] for v in parts.values())
    score = got / mx * 100
    verdict = ("Strong long-term candidate", "#16794a") if score >= 70 else \
              ("Mixed: real strengths, real weaknesses", "#a15c00") if score >= 55 else ("Weak long-term case today", "#b42318")
    # entry [Murphy ch16 "in uptrends buy the dips"; Hougaard: build in smaller layers]
    ext50 = last / s50 - 1
    if last < s200:
        entry = (f"Below its 200-day average ({bs._f(s200)}): the long-term trend is down. By the trend rules, wait until it "
                 f"closes back above the 200-day before buying for the long term.")
    elif ext50 > 0.15:
        entry = (f"{ext50 * 100:.0f}% above its 50-day average ({bs._f(s50)}): stretched. In an uptrend, buy the dips: "
                 f"wait for a pullback toward the 50-day, then build the position in 2-3 smaller layers.")
    elif last < s50:
        entry = (f"Pulling back below its 50-day ({bs._f(s50)}) but above the 200-day ({bs._f(s200)}). A dip in an uptrend is "
                 f"the usual place to start; start small and add only if it turns back up above the 50-day.")
    else:
        entry = (f"In an uptrend and not stretched (within {ext50 * 100:.0f}% of the 50-day, {bs._f(s50)}). A reasonable zone "
                 f"to start in 2-3 layers; a close below the 200-day ({bs._f(s200)}) would say the trend has failed.")
    return dict(score=score, parts=parts, notes=notes, verdict=verdict, entry=entry, mdd=mdd, rs=rs)


def breakout_view(t, d, ctx):
    """The scanner's own rules on this ticker: breakout (fresh, held or caught late), radar, or nothing."""
    A = bs.arrays(d)
    min_dv = bs.CFG["min_dollar_vol_ca"] if bs.region(t) == "CA" else bs.CFG["min_dollar_vol"]
    r = bs.analyze(d, A, min_dv)
    if r:
        r["Ticker"], r["Indexes"] = t, "Lookup"
        r = bs.enrich(r, d, A, ctx)
        return "breakout", r
    f = bs.check_forming(d, A, min_dv)
    if f:
        return "radar", f
    return "none", None


def chart(t, d, levels):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = d.iloc[-500:]
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=110)
    ax.plot(x.index, x["Close"], color="#1d5fbf", lw=1.4, label="Close")
    ax.plot(x.index, x["SMA50"], color="#a15c00", lw=1, label="50-day")
    ax.plot(x.index, x["SMA200"], color="#6b6b6b", lw=1, label="200-day")
    for v, lab, col in levels:
        if np.isfinite(_n(v)):
            ax.axhline(v, color=col, lw=0.9, ls="--", label=f"{lab} {v:.2f}")
    ax.set_title(f"{t} - last 2 years", fontsize=10, loc="left")
    ax.legend(fontsize=7, loc="upper left", frameon=False)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ page
def facts_grid(items):
    return '<div class="facts">' + "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in items) + "</div>"


def bars(parts):
    out = []
    for k, (got, mx, src) in parts.items():
        w = max(0, min(100, got / mx * 100))
        out.append(f'<div class="lb"><div class="lbt"><span>{k}</span><b>{got:.0f}/{mx}</b></div>'
                   f'<div class="lbb"><i style="width:{w:.0f}%"></i></div><small>{e(src)}</small></div>')
    return "".join(out)


def news_html(news):
    items = []
    for n in (news or [])[:8]:
        c = n.get("content", n) if isinstance(n, dict) else {}
        title = c.get("title") or n.get("title")
        url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None) or n.get("link")
        src = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else n.get("publisher")
        when = c.get("pubDate") or ""
        if title and url and str(url).startswith("http"):
            items.append(f'<li><a href="{e(url)}" target="_blank" rel="noopener">{e(title)}</a>'
                         f'<br><small class="hint">{e(src or "")} {e(str(when)[:10])}</small></li>')
    return "<ul class=\"news\">" + "".join(items) + "</ul>" if items else '<p class="hint">No recent headlines found.</p>'


def earnings_html(F, info):
    inc = F.get("inc")
    rev, ni = row(inc, "Total Revenue", "Operating Revenue"), row(inc, "Net Income", "Net Income Common Stockholders")
    eps = row(inc, "Diluted EPS", "Basic EPS")
    out = ""
    if rev is not None:
        yrs = list(rev.index)[-4:]
        head = "".join(f"<th>{pd.Timestamp(y).year}</th>" for y in yrs)
        line = lambda lab, s, f: f"<tr><td>{lab}</td>" + "".join(
            f"<td>{f(s.get(y)) if s is not None and y in s.index else '-'}</td>" for y in yrs) + "</tr>"
        out += (f'<h4>Annual results ({info.get("financialCurrency") or info.get("currency") or ""})</h4>'
                f'<div class="wrap"><table class="sum"><tr><th></th>{head}</tr>'
                + line("Revenue", rev, big) + line("Net income", ni, big)
                + line("EPS (diluted)", eps, lambda v: f"{v:.2f}" if np.isfinite(_n(v)) else "-") + "</table></div>")
    eh = F.get("ehist")
    if isinstance(eh, pd.DataFrame) and len(eh):
        rows_ = []
        for idx, x in eh.tail(4).iterrows():
            act, est = _n(x.get("epsActual")), _n(x.get("epsEstimate"))
            sp = _n(x.get("surprisePercent"))
            beat = "" if not np.isfinite(sp) else ('<span class="up">beat</span>' if sp > 0 else '<span class="dn">miss</span>')
            rows_.append(f"<tr><td>{e(str(idx)[:10])}</td><td>{act:.2f}</td><td>{est:.2f}</td><td>{pct(sp, 100, 1, True)} {beat}</td></tr>"
                         if np.isfinite(act) and np.isfinite(est) else "")
        if any(rows_):
            out += ('<h4>Last quarters: earnings per share vs analysts\' estimate</h4><div class="wrap"><table class="sum">'
                    '<tr><th>Quarter</th><th>Actual</th><th>Estimate</th><th>Surprise</th></tr>' + "".join(rows_) + "</table></div>")
    cal = F.get("cal")
    nxt = None
    if isinstance(cal, dict):
        ds = [pd.Timestamp(x).date() for x in cal.get("Earnings Date", []) or []]
        ds = [x for x in ds if x >= dt.date.today()]
        nxt = min(ds) if ds else None
    out += f'<p class="hint">Next earnings: <b>{nxt.strftime("%b %d, %Y") if nxt else "not announced"}</b>.</p>'
    return out or '<p class="hint">No earnings data available.</p>', nxt


def build_page(key, t, d, F, ctx, bench):
    info = F["info"] or {}
    is_fund = (info.get("quoteType") or "").upper() in ("ETF", "MUTUALFUND")
    name = info.get("longName") or info.get("shortName") or t
    cur = info.get("currency") or ("CAD" if t.endswith(".TO") else "USD")
    last, prev = d["Close"].iloc[-1], d["Close"].iloc[-2]
    asof = pd.Timestamp(d.index[-1]).strftime("%a %b %d, %Y")

    lt = long_term(d, bench, info, F, is_fund)
    kind, br = breakout_view(t, d, ctx)
    earn_html, nxt = earnings_html(F, info) if not is_fund else ("", None)

    # breakout block, in the scanner's own language
    levels = []
    if kind == "breakout":
        ok_gate = (br["Score"] >= bs.CFG["score_buy"] and br["RR"] >= bs.CFG["min_rr"] and br["NWarn"] < bs.CFG["max_warnings"]
                   and not (br.get("EarnDays") is not None and np.isfinite(_n(br.get("EarnDays"))) and 0 <= br["EarnDays"] <= bs.CFG["earnings_block_days"]))
        ext = br["PctAboveRes"] > bs.TRADE["extended"] * 100
        if not br["Held"]:
            st = ("Watch", "#1d5fbf", f"Broke out on {br['BreakoutDate']} on {br['VolX']}x volume. It must hold above "
                                      f"{bs._f(br['Resistance'])} at tomorrow's close to confirm.")
        elif not ok_gate:
            st = ("Watch", "#1d5fbf", "Broke out and held, but doesn't pass the scorecard for a Buy.")
        elif ext:
            st = ("Don't chase", "#a15c00", f"Confirmed, but {br['PctAboveRes']:.1f}% above the breakout level. Wait for a pullback "
                                            f"toward {bs._f(br['Resistance'])}-{bs._f(br['Resistance'] * (1 + bs.TRADE['buy_zone']))}.")
        else:
            st = ("Buy setup", "#16794a", "Confirmed breakout that passes the scorecard. Same rules as a scanner Buy.")
        warns = [w for w in str(br.get("Warnings") or "").split("; ") if w]
        bx = (f'<p class="do"><span class="badge" style="background:{st[1]}">{st[0]}</span> {st[2]}</p>'
              + facts_grid([("Pattern", e(br["Pattern"])), ("Score", f"{br['Score']:.0f}/100"), ("Breakout level", bs._f(br["Resistance"])),
                            ("Volume", f"{br['VolX']}x avg"), ("Stop", bs._f(br["Stop"])), ("Target", bs._f(br["Target"])),
                            ("Reward:risk", f"{br['RR']:.1f}"), ("Base", f"{br['BaseLen']} days"), ("Warnings", str(br["NWarn"]))])
              + (f'<div class="flags">' + "".join(f'<span class="flag">{e(w)}</span>' for w in warns) + "</div>" if warns else "")
              + f'<details><summary>Scorecard &amp; checklist</summary><div class="sbs">{bs._score_bars(br.get("ScoreParts"))}</div>'
              + bs._checklist_html(br.get("Checklist"))
              + (f'<p class="hint"><b>Confirming signals:</b> {e(br["Signals"])}</p>' if br.get("Signals") else "") + "</details>")
        levels = [(br["Resistance"], "Breakout level", "#16794a"), (br["Stop"], "Stop", "#b42318")]
    elif kind == "radar":
        bx = (f'<p class="do"><span class="badge" style="background:#1d5fbf">On the radar</span> A tight {br["BaseLen"]}-day base '
              f'closing {br["DistPct"]:.1f}% under its ceiling. It becomes a breakout on a close above <b>{bs._f(br["Trigger"])}</b> '
              f'on at least <b>{br["VolNeeded"]:,}</b> shares (1.5x average), then has to hold the next day. Set a price alert there.</p>')
        levels = [(br["Trigger"], "Trigger", "#1d5fbf")]
    else:
        hi60 = d["High"].iloc[-61:-1].max()
        bx = (f'<p class="do"><span class="badge" style="background:#6b6b6b">No setup</span> No breakout and no tight base just under '
              f'resistance today. The nearest ceiling is the 3-month high at <b>{bs._f(hi60)}</b> '
              f'({(hi60 / last - 1) * 100:+.1f}% from here). The scanner checks it every evening if it is in one of the six indexes.</p>')
        levels = [(hi60, "3-month high", "#6b6b6b")]

    try:
        img = chart(t, d, levels)
    except Exception:
        img = ""
    p = info
    about = " ".join(str(p.get("longBusinessSummary") or "").split())
    snap = facts_grid([
        ("Price", f"{bs._f(last)} {cur}"), ("Today", pct(last / prev - 1, 100, 2, True)),
        ("Market cap", big(p.get("marketCap") or p.get("totalAssets"))),
        ("52-week range", f"{bs._f(d['Low'].iloc[-252:].min())} - {bs._f(d['High'].iloc[-252:].max())}"),
        ("1-year return", pct(last / d["Close"].iloc[-252] - 1, 100, 1, True) if len(d) > 252 else "-"),
        ("Dividend yield", pct(_n(p.get("dividendYield")) / (100 if _n(p.get("dividendYield")) > 1 else 1)) if np.isfinite(_n(p.get("dividendYield"))) else "-"),
    ])
    fin = "" if is_fund else facts_grid([
        ("Revenue (TTM)", big(p.get("totalRevenue"))), ("Revenue growth", pct(p.get("revenueGrowth"), 100, 1, True)),
        ("Profit margin", pct(p.get("profitMargins"))), ("Operating margin", pct(p.get("operatingMargins"))),
        ("Return on equity", pct(p.get("returnOnEquity"))), ("Free cash flow", big(p.get("freeCashflow"))),
        ("Debt / equity", f"{_n(p.get('debtToEquity')):.0f}%" if np.isfinite(_n(p.get("debtToEquity"))) else "-"),
        ("Cash", big(p.get("totalCash"))), ("Debt", big(p.get("totalDebt"))),
        ("P/E (trailing)", f"{_n(p.get('trailingPE')):.1f}" if np.isfinite(_n(p.get("trailingPE"))) else "-"),
        ("P/E (forward)", f"{_n(p.get('forwardPE')):.1f}" if np.isfinite(_n(p.get("forwardPE"))) else "-"),
        ("Price / book", f"{_n(p.get('priceToBook')):.1f}" if np.isfinite(_n(p.get("priceToBook"))) else "-"),
    ])
    fund_extra = "" if not is_fund else facts_grid([
        ("Expense ratio", pct(_n(p.get("netExpenseRatio")) / 100) if np.isfinite(_n(p.get("netExpenseRatio"))) else "-"),
        ("Assets", big(p.get("totalAssets"))), ("Category", e(p.get("category") or "-")),
        ("Fund family", e(p.get("fundFamily") or "-"))])
    tgt, n_an = _n(p.get("targetMeanPrice")), p.get("numberOfAnalystOpinions")
    analysts = ("" if not np.isfinite(tgt) else
                f'<p>{n_an or "Several"} analysts: consensus <b>{e(str(p.get("recommendationKey") or "-").replace("_", " "))}</b>, '
                f'average target <b>{bs._f(tgt)}</b> ({(tgt / last - 1) * 100:+.1f}% from here; range '
                f'{bs._f(_n(p.get("targetLowPrice")))}-{bs._f(_n(p.get("targetHighPrice")))}). '
                f'Analyst targets are often wrong and tend to follow the price; treat them as sentiment, not a forecast.</p>')
    # context notes from the books
    ctxnotes = []
    ind = (p.get("industry") or "").lower()
    if "gold" in ind or "silver" in ind or "precious" in ind:
        ctxnotes.append("Precious-metals company: Krauth (ch15, 32, 35) ties these to real interest rates, the gold/silver ratio and "
                        "seasonality (strongest Sep-Feb, weak Jun-Aug). See the Metals line in the evening report's market review.")
    if lt["mdd"] < -0.5:
        ctxnotes.append(f"It has fallen {abs(lt['mdd']) * 100:.0f}% at its worst in 5 years. Douglas (ch11) and Hougaard: size it so a "
                        f"fall like that again is survivable, and decide your exit before you buy.")
    if not is_fund and _n(p.get("debtToEquity")) > 200:
        ctxnotes.append("Heavy debt: Oygard's account of Iceland shows leverage turning a downturn into a collapse. Watch credit stress "
                        "in the market review.")
    if nxt and 0 <= (nxt - dt.date.today()).days <= 10:
        ctxnotes.append(f"Earnings on {nxt:%b %d}: the scanner never opens a breakout trade within 5 sessions of a report.")

    lt_bars = bars(lt["parts"])
    lt_notes = "".join(f"<li><b>{k}.</b> {e(v)}</li>" for k, v in lt["notes"])
    co = f'<p class="hint">{e(" · ".join(x for x in (p.get("industry"), p.get("sector"), p.get("country"), p.get("exchange")) if x))}</p>'
    site = p.get("website") or ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(t)} - lookup</title>
<style>{bs.PAGE_CSS}{bs.RUN_CSS}{LOOKUP_CSS}</style></head><body><main>
<p class="links"><a href="../index.html">Latest scan</a> &middot; <a href="index.html">Past lookups</a></p>
{bs.RUN_HTML}
<h1>{e(name)} <small class="hint">{e(t)}</small></h1>
{co}
<p class="hint">Prices as of the close of {asof}. Data from Yahoo Finance; analysis with the scanner's rules and the books in BOOK_RULES.md.</p>
{snap}
<section><h2>Verdicts</h2>
<div class="verd"><div style="--c:{lt['verdict'][1]}"><span>Long term</span><b>{lt['score']:.0f}/100</b><em>{lt['verdict'][0]}</em></div>
<div style="--c:{'#16794a' if kind == 'breakout' else '#1d5fbf' if kind == 'radar' else '#6b6b6b'}"><span>Breakout</span>
<b>{'Breakout' if kind == 'breakout' else 'On the radar' if kind == 'radar' else 'No setup'}</b>
<em>{(f"Score {br['Score']:.0f}/100" if kind == 'breakout' else f"Trigger {bs._f(br['Trigger'])}" if kind == 'radar' else 'nothing to act on')}</em></div></div>
<p class="do"><b>If you want to own it:</b> {e(lt['entry'])}</p>
{''.join(f'<p class="hint">{e(x)}</p>' for x in ctxnotes)}
</section>
{f'<img class="chart" alt="{e(t)} chart" src="data:image/png;base64,{img}"/>' if img else ''}
<section><h2>What it does</h2><p>{e(about) or 'No description available.'}</p>
{f'<p class="hint"><a href="{e(site)}" target="_blank" rel="noopener">Company website</a></p>' if site.startswith('http') else ''}
{fund_extra}</section>
{'' if is_fund else f'<section><h2>Earnings &amp; financial health</h2>{fin}{earn_html}</section>'}
{f'<section><h2>Analysts</h2>{analysts}</section>' if analysts else ''}
<section><h2>Long-term scorecard</h2><p class="hint">{'An ETF: company fundamentals do not apply, so it is scored on trend, strength and risk only. ' if is_fund else ''}
Each line shows the book or method it comes from.</p>{lt_bars}<ul class="gl">{lt_notes}</ul></section>
<section><h2>Breakout analysis (the scanner's rules)</h2>{bx}</section>
<section><h2>Recent headlines</h2>{news_html(F.get('news'))}</section>
<section><h2>Limits</h2><p class="hint">Automated from public data; numbers can be stale or missing for some listings (CDRs, small caps).
For a researched write-up - competitors, management, the latest quarter's details, what the financial press is saying - ask Claude
in the chat with the ticker. Not investment advice.</p></section>
</main></body></html>"""
    return page


LOOKUP_CSS = """
h1 small{font-size:14px;font-weight:600}
.verd{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.verd div{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:10px;padding:10px}
.verd span{display:block;color:var(--muted);font-size:12px} .verd b{font-size:20px} .verd em{display:block;font-style:normal;font-size:13px}
img.chart{width:100%;border-radius:10px;margin:12px 0;background:#fff}
.lb{margin:8px 0} .lbt{display:flex;justify-content:space-between;font-size:13px}
.lbb{height:8px;background:var(--line);border-radius:4px;overflow:hidden} .lbb i{display:block;height:100%;background:var(--ink)}
.lb small{color:var(--muted);font-size:11px}
ul.news{padding-left:18px;font-size:14px} ul.news li{margin:8px 0} ul.news a{color:var(--ink)}
.badge{color:#fff}
.flags{margin-top:8px} .flag{display:inline-block;font-size:12px;color:#a15c00;background:#a15c0018;border-radius:6px;padding:2px 8px;margin:2px 4px 0 0}
.sbs{margin:8px 0} .sb{display:grid;grid-template-columns:80px 1fr 44px;gap:8px;align-items:center;font-size:12px;margin:3px 0}
.sb i{display:block;height:8px;background:var(--line);border-radius:4px;overflow:hidden} .sb i b{display:block;height:100%;background:var(--c)}
.sb em{font-style:normal;color:var(--muted);text-align:right}
ul.ck{list-style:none;padding:0;margin:6px 0;font-size:13px} ul.ck li{margin:3px 0} ul.ck li.grp{color:var(--muted);font-size:11px;text-transform:uppercase;margin-top:8px}
ul.ck b{display:inline-block;width:20px} .ok{color:var(--up)} .no{color:var(--dn)} .na{color:var(--muted)}
"""


def write_index(outdir):
    ld = os.path.join(outdir, "lookup")
    items = []
    for f in sorted(os.listdir(ld)):
        if f.endswith(".html") and f != "index.html":
            ts = dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(ld, f)))
            items.append((ts, f))
    items.sort(reverse=True)
    li = "".join(f'<li><a href="{f}">{f[:-5]}</a> <small class="hint">{ts:%b %d %H:%M}</small></li>' for ts, f in items)
    with open(os.path.join(ld, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Past lookups</title><style>{bs.PAGE_CSS}{bs.RUN_CSS}</style></head><body><main>
<p class="links"><a href="../index.html">Latest scan</a></p>{bs.RUN_HTML}<h1>Past lookups</h1><ul class="gl">{li or '<li>None yet.</li>'}</ul></main></body></html>""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="reports")
    ap.add_argument("--ticker", default=os.environ.get("TICKER", ""))
    a = ap.parse_args()
    key = clean(a.ticker)
    os.makedirs(os.path.join(a.outdir, "lookup"), exist_ok=True)
    if not key:
        print("No valid ticker given.")
        return 1
    t, raw = fetch(key)
    out_path = os.path.join(a.outdir, "lookup", f"{key}.html")
    if t is None:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{bs.PAGE_CSS}{bs.RUN_CSS}</style></head><body><main><p class="links"><a href="../index.html">Latest scan</a></p>{bs.RUN_HTML}
<h1>{e(key)}</h1><p>No price data found for <b>{e(key)}</b> (also tried {e(key)}.TO). Check the symbol: Canadian stocks end in .TO
(e.g. SHOP.TO), Cboe Canada listings in .NE.</p></main></body></html>""")
        write_index(a.outdir)
        return 0
    reg = bs.region(t)
    bench_t = bs.BENCH[reg]
    extra = sorted(set([bench_t, "SPY", "XIU.TO"] + bs.ALL_SECTOR_ETFS + ["HYG", "IEF", "KBE", "XFN.TO"]))
    data = {t: bs.prep(bs.drop_live_bar(raw))}
    for k, v in bs.download(extra, period="2y").items():
        try:
            data[k] = bs.prep(bs.drop_live_bar(v))
        except Exception:
            pass
    F = fundamentals(t)
    info = F["info"] or {}
    bs.PROFILES[t] = {"Name": info.get("longName") or "", "Sector": info.get("sector") or "", "Industry": info.get("industry") or "",
                      "Country": info.get("country") or "", "Website": info.get("website") or "",
                      "Summary": info.get("longBusinessSummary") or ""}
    ctx = bs.build_context(data, [t])
    ctx["breadth"] = {"US": None, "CA": None}        # one stock is not a market: breadth scored neutral
    ctx["internals"] = {"US": None, "CA": None}
    earn = None
    cal = F.get("cal")
    if isinstance(cal, dict):
        ds = sorted(pd.Timestamp(x).date() for x in (cal.get("Earnings Date") or []) if pd.Timestamp(x).date() >= dt.date.today())
        earn = ds[0] if ds else None
    ctx.update(sector={t: info.get("sector")}, industry={t: info.get("industry")}, earnings={t: earn} if earn else {})
    page = build_page(key, t, data[t], F, ctx, data.get(bench_t))
    for k in {key, clean(t)}:
        with open(os.path.join(a.outdir, "lookup", f"{k}.html"), "w", encoding="utf-8") as fh:
            fh.write(page)
    write_index(a.outdir)
    print(f"Lookup written for {t}: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
