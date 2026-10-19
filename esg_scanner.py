"""ESG-only breakout scanner (separate tab: esg/index.html).

Modular add-on: it imports the main scanner's functions and never changes them, its files live in reports/esg/,
and it runs as its own workflow step (continue-on-error), so a problem here cannot stop the evening scan.

Screening framework (what "qualifies as ESG" means here):
  * UN Global Compact - ten principles in four areas: human rights (1-2), labour (3-6), environment (7-9),
    anti-corruption (10). A company involved in severe controversies (Sustainalytics controversy level 4-5) is
    treated as a possible breach and excluded; this is how Sustainalytics' Global Standards Screening flags
    UNGC non-compliance.
  * UN-supported Principles for Responsible Investment (PRI), principle 1: ESG risk is part of the analysis -
    only companies with Negligible, Low or Medium ESG risk (Sustainalytics risk score under 30) qualify.
  * Global exclusion criteria (UN conventions on cluster munitions / landmines / chemical and biological weapons,
    WHO Framework Convention on Tobacco Control, Paris Agreement via the EU Paris-Aligned Benchmark exclusions):
    controversial weapons, civilian firearms, tobacco, thermal coal, oil & gas production, refining and pipelines.
  * UN Sustainable Development Goals: each company's business is linked to the SDGs it serves (by industry).

Data: ESG risk ratings by Morningstar Sustainalytics as published on Yahoo Finance; prices and the breakout
logic are the main scanner's. Universe: S&P 500, Dow, S&P/TSX Composite and TSX 60 (the companies that carry
ESG ratings; most Nasdaq Composite and Russell 2000 small caps are not rated).

  python esg_scanner.py --outdir reports            # daily ESG scan -> reports/esg/index.html
  python esg_scanner.py --outdir reports --lookup X # ESG brief + full analysis -> reports/esg/lookup/X.html
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

import breakout_scanner as bs

e = lambda s: html.escape(str(s if s is not None else ""))

ESG_INDEXES = ["tsx", "tsx60", "sp500", "djia"]
ESG_ETFS = [("ESGU", "iShares ESG Aware MSCI USA"), ("SUSA", "iShares MSCI USA ESG Select"),
            ("XESG.TO", "iShares ESG Aware MSCI Canada")]
RULES = dict(max_risk=30.0, leader_risk=20.0, max_controversy=3, leader_controversy=2,
             max_age_days=30, max_fetch=900, pause=0.25, max_fail_streak=12)

# product involvement flags reported with the rating
EXCLUDE_FLAGS = {
    "controversialWeapons": "Controversial weapons (UN conventions on cluster munitions, landmines, chemical and biological weapons)",
    "smallArms": "Civilian firearms",
    "tobacco": "Tobacco (WHO Framework Convention on Tobacco Control)",
    "coal": "Thermal coal (Paris Agreement; EU Paris-Aligned Benchmark exclusion)",
}
DISCLOSE_FLAGS = {"militaryContract": "military contracting", "nuclear": "nuclear power", "gambling": "gambling",
                  "alcoholic": "alcohol", "adult": "adult entertainment", "palmOil": "palm oil",
                  "pesticides": "pesticides", "gmo": "GMOs", "animalTesting": "animal testing",
                  "furLeather": "fur and specialty leather"}
# fossil-fuel business (EU Paris-Aligned Benchmark: oil & gas exploration, extraction, refining, distribution)
EXCLUDE_INDUSTRY = ["oil & gas e&p", "oil & gas integrated", "oil & gas drilling", "oil & gas refining",
                    "oil & gas midstream", "thermal coal", "coal", "tobacco", "oil & gas producers", "refiners & pipelines"]

UNGC = [  # (principles, area, what it covers, which risk pillar evidences it)
    ("1-2", "Human rights", "Support human rights; no complicity in abuses", "SocRisk"),
    ("3-6", "Labour", "Freedom of association; no forced or child labour; no discrimination", "SocRisk"),
    ("7-9", "Environment", "Precaution, environmental responsibility, clean technology", "EnvRisk"),
    ("10", "Anti-corruption", "Work against corruption, extortion and bribery", "GovRisk"),
]
SDG = {3: "Good health", 2: "Zero hunger", 4: "Quality education", 6: "Clean water", 7: "Clean energy",
       8: "Decent work & growth", 9: "Industry, innovation & infrastructure", 11: "Sustainable cities",
       12: "Responsible consumption", 13: "Climate action"}
SDG_MAP = [  # industry keyword -> SDGs (business link, not a measured impact)
    (("solar", "renewable", "wind"), (7, 13)), (("water",), (6,)), (("waste", "pollution", "environmental"), (11, 12)),
    (("medical", "drug", "biotech", "health", "diagnostic", "pharma"), (3,)), (("education",), (4,)),
    (("bank", "credit", "insurance", "capital markets", "asset management"), (8,)),
    (("railroad", "electrical equipment", "engineering", "construction", "infrastructure operations", "building"), (9, 11)),
    (("semiconductor", "software", "telecom", "communication", "information technology", "computer"), (9,)),
    (("farm", "agricultur", "grocery", "packaged food", "food"), (2,)),
    (("utilities - regulated electric", "utilities - diversified", "utilities - independent"), (7,)),
    (("residential", "reit - healthcare"), (11,)), (("auto", "electric vehicle"), (9, 13)),
    (("copper", "lithium", "industrial metals", "aluminum"), (7, 9)), (("staffing", "employment"), (8,)),
]
RATING_COLS = ["Ticker", "FetchedOn", "Status", "TotalRisk", "EnvRisk", "SocRisk", "GovRisk", "Controversy",
               "Percentile", "PeerGroup", "RatingDate", "Flags"]


def risk_band(x):
    if not np.isfinite(x):
        return "-"
    return "Negligible" if x < 10 else "Low" if x < 20 else "Medium" if x < 30 else "High" if x < 40 else "Severe"


def _num(x):
    try:
        if isinstance(x, dict):
            x = x.get("raw")
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except (TypeError, ValueError):
        return np.nan


# --------------------------------------------------------------------------
# Ratings (Sustainalytics via Yahoo), cached for 30 days in reports/esg/ratings.csv
# --------------------------------------------------------------------------
def fetch_rating(t):
    """Returns (status, dict). status: ok | none (company not rated) | error (blocked / network)."""
    import yfinance as yf
    try:
        df = yf.Ticker(t).sustainability
    except Exception as ex:
        msg = str(ex).lower()
        if "not found" in msg or "no fundamentals" in msg or "404" in msg:
            return "none", {}
        return "error", {"err": str(ex)[:120]}
    if df is None or not len(df):
        return "none", {}
    s = df.iloc[:, 0].to_dict() if isinstance(df, pd.DataFrame) else dict(df)
    tot = _num(s.get("totalEsg"))
    if not np.isfinite(tot):
        return "none", {}
    flags = sorted(k for k in list(EXCLUDE_FLAGS) + list(DISCLOSE_FLAGS) if s.get(k) is True)
    ry, rm = s.get("ratingYear"), s.get("ratingMonth")
    return "ok", dict(TotalRisk=round(tot, 1), EnvRisk=_num(s.get("environmentScore")),
                      SocRisk=_num(s.get("socialScore")), GovRisk=_num(s.get("governanceScore")),
                      Controversy=_num(s.get("highestControversy")), Percentile=_num(s.get("percentile")),
                      PeerGroup=str(s.get("peerGroup") or ""),
                      RatingDate=f"{int(ry)}-{int(rm):02d}" if ry and rm else "", Flags=";".join(flags))


def load_ratings(path):
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, dtype={"Ticker": str, "PeerGroup": str, "Flags": str, "RatingDate": str, "Status": str})
            return {r["Ticker"]: {k: (None if (isinstance(v, float) and np.isnan(v) and k in ("Flags", "PeerGroup", "RatingDate"))
                                      else v) for k, v in r.items()} for r in df.to_dict("records")}
        except Exception:
            pass
    return {}


def save_ratings(path, ratings):
    pd.DataFrame(list(ratings.values()), columns=RATING_COLS).to_csv(path, index=False)


def refresh_ratings(tickers, path, diag):
    ratings = load_ratings(path)
    today = dt.date.today()

    def stale(t):
        r = ratings.get(t)
        if not r or r.get("Status") == "error":
            return True
        try:
            return (today - dt.date.fromisoformat(str(r["FetchedOn"]))).days >= RULES["max_age_days"]
        except ValueError:
            return True
    todo = sorted([t for t in tickers if stale(t)],
                  key=lambda t: (ratings.get(t) is not None, bs.region(t) != "CA", t))[:RULES["max_fetch"]]
    got = none = err = streak = 0
    for i, t in enumerate(todo):
        status, d = fetch_rating(t)
        if status == "error":
            err += 1
            streak += 1
            if t not in ratings:
                ratings[t] = dict(Ticker=t, FetchedOn=today.isoformat(), Status="error")
            if streak >= RULES["max_fail_streak"]:
                diag.append(f"ESG ratings: Yahoo stopped answering after {i + 1} requests ({d.get('err', '')}); "
                            "the rest are tried on the next run")
                break
            time.sleep(2)
            continue
        streak = 0
        got += status == "ok"
        none += status == "none"
        ratings[t] = dict(Ticker=t, FetchedOn=today.isoformat(), Status=status, **d)
        time.sleep(RULES["pause"])
        if i % 100 == 0:
            print(f"  ESG ratings {i}/{len(todo)}", flush=True)
    save_ratings(path, ratings)
    diag.append(f"ESG ratings: refreshed {len(todo)} (rated {got}, not rated {none}, errors {err}); "
                f"{sum(1 for t in tickers if (ratings.get(t) or {}).get('Status') == 'ok')}/{len(tickers)} companies have a rating")
    return ratings


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------
def sdgs_for(industry, peer):
    txt = f"{industry} {peer}".lower()
    out = []
    for keys, goals in SDG_MAP:
        if any(k in txt for k in keys):
            out += [g for g in goals if g not in out]
    return out


def assess(t, rating):
    prof = bs.PROFILES.get(t, {})
    industry, peer = str(prof.get("Industry") or ""), str((rating or {}).get("PeerGroup") or "")
    if not rating or rating.get("Status") != "ok":
        return dict(Qualified=False, Grade="No rating", Reasons=["No ESG risk rating is published for this company, "
                                                                 "so it cannot be screened."], Fails=[], SDGs=[])
    tot, cont = _num(rating.get("TotalRisk")), _num(rating.get("Controversy"))
    flags = [f for f in str(rating.get("Flags") or "").split(";") if f]
    fails, passes = [], []
    if np.isfinite(tot) and tot < RULES["max_risk"]:
        passes.append(f"ESG risk is {risk_band(tot)} ({tot:.1f}; under 30 qualifies) - meets the PRI test that ESG risk "
                      "is manageable.")
    else:
        fails.append(f"ESG risk is {risk_band(tot)} ({tot:.1f}); 30 or more does not qualify.")
    if np.isfinite(cont) and cont <= RULES["max_controversy"]:
        passes.append(f"Highest controversy level {cont:.0f} of 5 - no severe incidents, so no flagged breach of the "
                      "UN Global Compact.")
    elif np.isfinite(cont):
        fails.append(f"Controversy level {cont:.0f} of 5 - severe incidents of the kind that put companies on UN Global "
                     "Compact watch or non-compliant lists.")
    else:
        passes.append("No controversy level reported.")
    hit = [EXCLUDE_FLAGS[f] for f in flags if f in EXCLUDE_FLAGS]
    ind_hit = [k for k in EXCLUDE_INDUSTRY if k in f"{industry} {peer}".lower()]
    if hit:
        fails.append("Excluded business involvement: " + "; ".join(hit) + ".")
    if ind_hit:
        fails.append(f"Fossil-fuel or tobacco business ({industry or peer}) - excluded under the Paris-Aligned "
                     "Benchmark criteria.")
    if not hit and not ind_hit:
        passes.append("No involvement in excluded businesses (controversial weapons, civilian firearms, tobacco, thermal "
                      "coal, oil & gas).")
    disc = [DISCLOSE_FLAGS[f] for f in flags if f in DISCLOSE_FLAGS]
    if disc:
        passes.append("Disclosed involvement (allowed, but worth knowing): " + ", ".join(disc) + ".")
    q = not fails
    grade = ("ESG leader" if q and tot < RULES["leader_risk"] and (not np.isfinite(cont) or cont <= RULES["leader_controversy"])
             else "ESG qualified" if q else "Not qualified")
    return dict(Qualified=q, Grade=grade, Reasons=passes, Fails=fails, SDGs=sdgs_for(industry, peer))


GRADE_COLOR = {"ESG leader": "#16794a", "ESG qualified": "#1d5fbf", "Not qualified": "#b42318", "No rating": "#6b6b6b"}


def brief_html(t, rating, a, open_=False):
    """The per-company ESG brief: why it qualifies, which UN criteria, sustainability metrics."""
    col = GRADE_COLOR.get(a["Grade"], "#555")
    r = rating or {}
    lines = "".join(f"<li>&#10003; {e(x)}</li>" for x in a["Reasons"]) + "".join(f"<li>&#10007; {e(x)}</li>" for x in a["Fails"])
    cont = _num(r.get("Controversy"))
    rows = ""
    for pr, area, what, key in UNGC:
        v = _num(r.get(key))
        ok = np.isfinite(cont) and cont <= RULES["max_controversy"] if r.get("Status") == "ok" else None
        if key == "EnvRisk" and any(k in f"{bs.PROFILES.get(t, {}).get('Industry', '')} {r.get('PeerGroup', '')}".lower()
                                    for k in EXCLUDE_INDUSTRY):
            ok = False
        res = "-" if ok is None else ("Meets" if ok else "Does not meet")
        rows += (f"<tr><td>{pr}</td><td><b>{area}</b><br><small>{what}</small></td>"
                 f"<td><b class='{'up' if ok else 'dn' if ok is False else ''}'>{res}</b><br>"
                 f"<small>{'risk ' + format(v, '.1f') if np.isfinite(v) else 'pillar score n/a'}</small></td></tr>")
    sdg = " ".join(f'<span class="sdg">SDG {g} &middot; {SDG[g]}</span>' for g in a["SDGs"]) or \
        '<span class="hint">No direct SDG link from its line of business.</span>'
    tot = _num(r.get("TotalRisk"))
    pct = _num(r.get("Percentile"))
    facts = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in [
        ("ESG risk score", f"{tot:.1f} ({risk_band(tot)})" if np.isfinite(tot) else "-"),
        ("Environment / Social / Governance risk",
         " / ".join(f"{_num(r.get(k)):.1f}" if np.isfinite(_num(r.get(k))) else "-" for k in ("EnvRisk", "SocRisk", "GovRisk"))),
        ("Peer percentile (lower = less risk)", f"{pct:.0f}th" if np.isfinite(pct) else "-"),
        ("Peer group", e(r.get("PeerGroup") or "-")), ("Highest controversy", f"{cont:.0f} of 5" if np.isfinite(cont) else "-"),
        ("Rating date", e(r.get("RatingDate") or "-"))])
    return (f'<details class="esg"{" open" if open_ else ""}><summary><span class="eg" style="background:{col}">{a["Grade"]}</span> '
            f'ESG brief: why it qualifies</summary><ul class="ck">{lines}</ul>'
            f'<div class="wrap"><table class="ungc"><tr><th>UNGC</th><th>Area</th><th>Result</th></tr>{rows}</table></div>'
            f'<p style="margin:8px 0 4px"><b>UN Sustainable Development Goals</b> (by line of business)</p><p>{sdg}</p>'
            f'<div class="facts">{facts}</div>'
            f'<p class="hint">ESG risk ratings: Morningstar Sustainalytics via Yahoo Finance. UNGC results use the controversy '
            f'screen (levels 4-5 = severe); pillar risk scores are shown as evidence. Ratings are third-party opinions, not '
            f'a certification.</p></details>')


ESG_CSS = """
details.esg{background:#f3f8f4;border:1px solid #cfe3d4;border-radius:10px;padding:8px 10px;margin:8px 0}
details.esg summary{cursor:pointer;font-weight:600}
.eg{color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;margin-right:4px}
.sdg{display:inline-block;background:#e6f0fa;color:#123;border-radius:6px;padding:2px 7px;margin:2px;font-size:12px}
table.ungc{width:100%}table.ungc td,table.ungc th{font-size:13px;vertical-align:top;white-space:normal}
.esgtab{width:100%}.esgtab td{font-size:13px}
.esgsum p{margin:4px 0}
@media (prefers-color-scheme:dark){details.esg{background:#16231a;border-color:#2c4a35}.sdg{background:#1d2b3a;color:#dce8f5}}
"""

ESG_SEARCH = """
<form class="lookbar" onsubmit="event.preventDefault(); esgLook();">
  <input id="elk" placeholder="ESG search: ticker or name" autocomplete="off"
         autocapitalize="characters" maxlength="15" oninput="esgFilter(this.value)">
  <button type="submit">ESG analyze</button>
</form>
<script>
window.esgFilter = q => { q = (q || '').trim().toUpperCase();
  document.querySelectorAll('#esglist tr[data-k]').forEach(r => { r.style.display = !q || r.dataset.k.includes(q) ? '' : 'none'; }); };
window.esgLook = () => { const k = (document.getElementById('elk').value || '').trim().toUpperCase().replace(/ +/g, '');
  if (!/^[A-Z0-9][A-Z0-9.=^-]{0,14}$/.test(k)) { document.getElementById('runstatus').textContent = 'Type a ticker, e.g. RY.TO or MSFT.'; return; }
  runNow('esg_lookup', k); };
</script>"""


def _esg_page_chrome(page, depth=1):
    """Re-point a page made by the main report writer to the ESG tab: links, title, search box."""
    up = "../" * depth
    if depth == 2:                     # a lookup page: its own links point at the main site
        page = page.replace('href="../', 'href="../../')
    page = re.sub(r'<form class="lookbar".*?</form>', ESG_SEARCH.replace("\\", "\\\\"), page, count=1, flags=re.S)
    for name in ("index.html", "live.html", "morning.html", "backtest.html", "archive.html"):
        page = page.replace(f'href="{name}"', f'href="{up}{name}"')
    nav = (f'<p class="links"><b>ESG scanner</b> &middot; <a href="{up}index.html">Main scanner</a> &middot; '
           f'<a href="{"" if depth == 1 else "../"}index.html">ESG home</a> &middot; <a href="{up}app/">App</a></p>')
    page = page.replace("<main>", "<main>" + nav, 1)
    return page.replace("</style>", ESG_CSS + "</style>", 1)


# --------------------------------------------------------------------------
# Daily ESG scan
# --------------------------------------------------------------------------
def summary_html(asof, qual, ratings, data, universe, res, radar, diag):
    n_rated = sum(1 for t in universe if (ratings.get(t) or {}).get("Status") == "ok")
    lead = [t for t, a in qual.items() if a["Grade"] == "ESG leader"]
    ca = [t for t in qual if bs.region(t) == "CA"]

    def avg1(ts):
        xs = [bs._chg(data[t], 1) for t in ts if t in data and len(data[t]) > 2]
        xs = [x for x in xs if np.isfinite(x)]
        return np.mean(xs) if xs else np.nan
    q1, all1 = avg1(list(qual)), avg1(universe)

    def fmt(x):
        return f'<b class="{"up" if x >= 0 else "dn"}">{x:+.1f}%</b>' if np.isfinite(x) else "-"
    etf_rows = ""
    for t, n in ESG_ETFS + [("SPY", "S&P 500 (SPY)"), ("XIU.TO", "S&P/TSX 60 (XIU)")]:
        d = data.get(t)
        if d is None or len(d) < 25:
            continue
        etf_rows += (f"<tr><td class='tk'>{e(n)}</td><td>{bs._f(d['Close'].iloc[-1])}</td><td>{fmt(bs._chg(d, 1))}</td>"
                     f"<td>{fmt(bs._chg(d, 21))}</td><td>{fmt(bs._ytd(d))}</td></tr>")
    movers = sorted(((bs._chg(data[t], 1), t) for t in qual if t in data and len(data[t]) > 2), key=lambda z: z[0])
    movers = [m for m in movers if np.isfinite(m[0])]
    mv = lambda lst: ", ".join(f"{t} {fmt(x)}" for x, t in lst) or "-"
    best_sec = {}
    for t in qual:
        s = bs.PROFILES.get(t, {}).get("Sector") or "Other"
        best_sec[s] = best_sec.get(s, 0) + 1
    secs = ", ".join(f"{k} {v}" for k, v in sorted(best_sec.items(), key=lambda z: -z[1])[:6])
    nb = len(res)
    gate = int(res["Gate"].sum()) if nb and "Gate" in res else 0
    return f"""<section class="review esgsum"><h2>ESG daily summary &middot; close of {pd.Timestamp(asof):%a %b %d}</h2>
<p><b>{len(qual)}</b> of {n_rated} rated companies qualify ({len(lead)} ESG leaders; {len(ca)} Canadian), out of
{len(universe)} in the S&P 500, Dow, S&P/TSX Composite and TSX 60.</p>
<p>ESG breakouts today: <b>{nb}</b> ({gate} pass the Buy gate); on the ESG radar: <b>{len(radar)}</b>.</p>
<p>Average move today: qualified ESG companies {fmt(q1)} vs all companies {fmt(all1)}.</p>
<p>Biggest ESG gainers: {mv([m for m in movers[::-1] if m[0] > 0][:5])}<br>Biggest ESG decliners: {mv([m for m in movers if m[0] < 0][:5])}</p>
<p>Qualified companies by sector: {e(secs)}</p>
<div class="wrap"><table class="mr"><tr><th class="tk">ESG funds vs the market</th><th>Last</th><th>1 day</th><th>1 month</th><th>YTD</th></tr>{etf_rows}</table></div>
<details><summary>How companies qualify (UN standards)</summary><ul class="hint">
<li><b>UN Global Compact</b> (human rights, labour, environment, anti-corruption): no severe controversies (Sustainalytics level 4-5),
the screen used to flag UNGC breaches.</li>
<li><b>UN Principles for Responsible Investment</b>: ESG risk is part of the analysis; only Negligible, Low or Medium ESG risk
(score under 30) qualifies. Under 20 with controversies at level 2 or lower = ESG leader.</li>
<li><b>Global exclusions</b>: controversial weapons (UN conventions), civilian firearms, tobacco (WHO FCTC), thermal coal and
oil &amp; gas production, refining and pipelines (EU Paris-Aligned Benchmark rules).</li>
<li><b>UN SDGs</b>: each company's line of business is linked to the goals it serves.</li>
<li>Breakouts, radar, scores, stops and targets use exactly the same rules as the main scanner.</li>
<li>Ratings are refreshed every 30 days. {' '.join(e(x) for x in diag if x.startswith('ESG ratings'))}</li></ul></details>
</section>"""


def list_html(qual, ratings, data):
    rows = ""
    order = sorted(qual, key=lambda t: (bs.region(t) != "CA", qual[t]["Grade"] != "ESG leader",
                                        _num(ratings[t].get("TotalRisk"))))
    for t in order:
        a, r, d = qual[t], ratings[t], data.get(t)
        name = bs.PROFILES.get(t, {}).get("Name") or ""
        ch = bs._chg(d, 1) if d is not None and len(d) > 2 else np.nan
        trend = ""
        if d is not None and len(d) > 200 and pd.notna(d["SMA200"].iloc[-1]):
            c, s50, s200 = d["Close"].iloc[-1], d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
            trend = "Uptrend" if c > s50 > s200 else "Downtrend" if c < s50 < s200 else "Mixed"
        tot, cont = _num(r.get("TotalRisk")), _num(r.get("Controversy"))
        rows += (f'<tr data-k="{e(t)} {e(name.upper())}"><td class="tk"><a href="lookup/{e(t)}.html">{e(t)}</a></td>'
                 f'<td class="cn">{e(name)}</td><td><span class="eg" style="background:{GRADE_COLOR[a["Grade"]]}">'
                 f'{a["Grade"].replace("ESG ", "")}</span></td><td>{tot:.1f}</td>'
                 f'<td>{f"{cont:.0f}" if np.isfinite(cont) else "-"}</td><td>{e(r.get("PeerGroup") or "")}</td>'
                 f'<td class="{"up" if ch >= 0 else "dn"}">{f"{ch:+.1f}%" if np.isfinite(ch) else "-"}</td>'
                 f'<td class="{trend.lower()}">{trend}</td></tr>')
    return (f'<h2>All qualified ESG companies ({len(qual)})</h2><p class="hint">Type in the ESG search box to filter. '
            f'A ticker link opens its ESG brief if it has been analyzed; use <b>ESG analyze</b> for a fresh full report.</p>'
            f'<div class="wrap"><table class="esgtab" id="esglist"><tr><th>Ticker</th><th>Company</th><th>Grade</th>'
            f'<th>ESG risk</th><th>Contro&shy;versy</th><th>Peer group</th><th>Today</th><th>Trend</th></tr>{rows}</table></div>')


def run_scan(outdir):
    t0 = time.time()
    esg_dir = os.path.join(outdir, "esg")
    os.makedirs(os.path.join(esg_dir, "lookup"), exist_ok=True)
    diag = []
    bs.PROFILES.update(bs._load_profiles(os.path.join(outdir, "sectors.csv")))      # read-only: main scanner's cache
    bs.PROFILES.update(bs._load_profiles(os.path.join(esg_dir, "sectors.csv")))
    print("ESG: loading universe...")
    membership = bs.build_universe(ESG_INDEXES)
    universe = sorted(membership, key=lambda t: (bs.region(t) != "CA", t))
    print(f"ESG universe: {len(universe)} companies")
    ctx_t = ["SPY", "DIA", "QQQ", "IWM", "XIU.TO"]
    raw = bs.download(sorted(set(universe) | set(ctx_t) | set(bs.ALL_SECTOR_ETFS) | set(bs.MACRO_TICKERS)
                             | set(bs.STRESS_TICKERS) | {t for t, _ in ESG_ETFS},
                             key=lambda t: (bs.region(t) != "CA", t)))
    data = {}
    for t, d in raw.items():
        try:
            data[t] = bs.prep(bs.drop_live_bar(d))
        except Exception:
            pass
    # liquid names only (the main scanner's price and dollar-volume floors), then their ESG ratings
    liquid = []
    for t in universe:
        d = data.get(t)
        if d is None or len(d) < 60:
            continue
        min_dv = bs.CFG["min_dollar_vol_ca"] if bs.region(t) == "CA" else bs.CFG["min_dollar_vol"]
        if d["Close"].iloc[-1] >= bs.CFG["min_price"] and float((d["Close"] * d["Volume"]).iloc[-50:].mean()) >= min_dv:
            liquid.append(t)
    ratings = refresh_ratings(liquid, os.path.join(esg_dir, "ratings.csv"), diag)
    # industry for the fossil-fuel screen: profiles for rated names we don't know yet
    rated = [t for t in liquid if (ratings.get(t) or {}).get("Status") == "ok"]
    missing = [t for t in rated if not bs.PROFILES.get(t, {}).get("Industry")]
    if missing:
        try:
            bs.fetch_sector_earnings([], os.path.join(esg_dir, "sectors.csv"), profile_only=missing[:300])
        except Exception as ex:
            diag.append(f"ESG profiles failed: {str(ex)[:100]}")
    qual = {}
    for t in rated:
        a = assess(t, ratings[t])
        if a["Qualified"]:
            qual[t] = a
    print(f"ESG: {len(rated)} rated, {len(qual)} qualify")

    # the main scanner's breakout logic, unchanged, on the qualified list
    found, radar = [], []
    for t in qual:
        min_dv = bs.CFG["min_dollar_vol_ca"] if bs.region(t) == "CA" else bs.CFG["min_dollar_vol"]
        try:
            A = bs.arrays(data[t])
            r = bs.analyze(data[t], A, min_dv)
            f = None if r else bs.check_forming(data[t], A, min_dv)
        except Exception:
            r = f = None
        if r:
            r.update(Ticker=t, Indexes=", ".join(sorted(membership[t])))
            found.append((r, A))
        elif f:
            f.update(Ticker=t, Indexes=", ".join(sorted(membership[t])), Region=bs.region(t))
            radar.append(f)
    radar = bs.radar_merge(radar, os.path.join(esg_dir, "radar.csv"), save=True)
    ctx = bs.build_context(data, universe)
    try:
        sectors, industries, earnings = bs.fetch_sector_earnings(
            [r["Ticker"] for r, _ in found], os.path.join(esg_dir, "sectors.csv"),
            profile_only=[x["Ticker"] for x in radar])
    except Exception as ex:
        sectors, industries, earnings = {}, {}, {}
        diag.append(f"ESG sector/earnings lookup failed: {str(ex)[:100]}")
    ctx.update(sector=sectors, industry=industries, earnings=earnings)
    results = []
    for r, A in found:
        try:
            results.append(bs.enrich(r, data[r["Ticker"]], A, ctx))
        except Exception as ex:
            diag.append(f"{r['Ticker']}: scoring failed ({str(ex)[:80]})")
    res = pd.DataFrame(results)
    if len(res):
        res["_ca"] = res["Ticker"].map(lambda t: bs.region(t) != "CA")
        res = res.sort_values(["_ca", "Score"], ascending=[True, False]).drop(columns="_ca").reset_index(drop=True)
    bs.MARKET.clear()
    for reg, name in (("US", "US (S&P 500)"), ("CA", "Canada (TSX 60)")):
        if ctx["bench"].get(reg) is not None:
            bs.MARKET.append((name, bs.market_state(ctx["bench"][reg], ctx["breadth"][reg], ctx["bench"][reg].index[-1],
                                                    ctx["internals"][reg], ctx["credit"], ctx["banks"][reg])))
    asof = pd.Timestamp(max(data[t].index[-1] for t in ctx_t if t in data)).date().isoformat()
    rows = bs.update_tracker([], res, data, asof) if len(res) else []      # fresh rows: the ESG tab keeps no positions

    # cards get the ESG brief under the company profile (patched in this process only)
    orig = bs.company_html
    bs.company_html = lambda t, full=True: orig(t, full) + (brief_html(t, ratings.get(t), qual[t]) if t in qual else "")
    review = summary_html(asof, qual, ratings, data, universe, res, radar, diag) + list_html(qual, ratings, data)
    path = os.path.join(esg_dir, "index.html")
    bs.write_html(path, asof, rows, res, data, bs.market_context(data), len(data), review, title="ESG scanner",
                  subtitle=f"ESG-only scan: {len(qual)} companies that meet the UN-based criteria", radar=radar)
    bs.company_html = orig
    page = _esg_page_chrome(open(path, encoding="utf-8").read())
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    json.dump(dict(asof=asof, qualified=len(qual), rated=len(rated), breakouts=len(res), radar=len(radar),
                   diag=diag, minutes=round((time.time() - t0) / 60, 1)),
              open(os.path.join(esg_dir, "status.json"), "w"))
    print("\n".join(diag))
    print(f"ESG scan: {len(qual)} qualified, {len(res)} breakouts, {len(radar)} on radar in {time.time() - t0:.0f}s -> {path}")
    return 0


# --------------------------------------------------------------------------
# ESG lookup: ESG brief on top of the regular ticker analysis
# --------------------------------------------------------------------------
def run_lookup(outdir, ticker):
    import lookup as lk
    key = lk.clean(ticker)
    esg_dir = os.path.join(outdir, "esg", "lookup")
    os.makedirs(esg_dir, exist_ok=True)
    if not key:
        print("No valid ticker given.")
        return 1
    t, raw = lk.fetch(key)
    if t is None:
        with open(os.path.join(esg_dir, f"{key}.html"), "w", encoding="utf-8") as fh:
            fh.write(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                     f'<body style="font-family:sans-serif;margin:16px"><p><a href="../index.html">ESG home</a></p>'
                     f'<h1>{e(key)}</h1><p>No price data found for {e(key)} (also tried {e(key)}.TO).</p></body>')
        return 0
    reg = bs.region(t)
    bench_t = bs.BENCH[reg]
    data = {t: bs.prep(bs.drop_live_bar(raw))}
    for k, v in bs.download(sorted(set([bench_t, "SPY", "XIU.TO"] + bs.ALL_SECTOR_ETFS + ["HYG", "IEF", "KBE", "XFN.TO"])),
                            period="2y").items():
        try:
            data[k] = bs.prep(bs.drop_live_bar(v))
        except Exception:
            pass
    F = lk.fundamentals(t)
    info = F["info"] or {}
    bs.PROFILES[t] = {"Name": info.get("longName") or "", "Sector": info.get("sector") or "",
                      "Industry": info.get("industry") or "", "Country": info.get("country") or "",
                      "Website": info.get("website") or "", "Summary": info.get("longBusinessSummary") or ""}
    status, d = fetch_rating(t)
    rating = dict(Ticker=t, Status=status, **d) if status == "ok" else {"Status": status}
    a = assess(t, rating)
    ctx = bs.build_context(data, [t])
    ctx["breadth"] = {"US": None, "CA": None}
    ctx["internals"] = {"US": None, "CA": None}
    earn = None
    cal = F.get("cal")
    if isinstance(cal, dict):
        ds = sorted(pd.Timestamp(x).date() for x in (cal.get("Earnings Date") or []) if pd.Timestamp(x).date() >= dt.date.today())
        earn = ds[0] if ds else None
    ctx.update(sector={t: info.get("sector")}, industry={t: info.get("industry")}, earnings={t: earn} if earn else {})
    bench = data.get(bench_t)
    if bench is not None:
        bench.attrs["t"] = bench_t
    page = lk.build_page(key, t, data[t], F, ctx, bench)
    note = ("" if status != "error" else
            '<p class="hint">The ESG rating could not be fetched just now (data source busy); try again later.</p>')
    sec = (f'<section><h2>ESG assessment (UN standards)</h2>{note}{brief_html(t, rating, a, open_=True)}</section>')
    i = page.find("<h2")
    page = page[:i] + sec + page[i:] if i > 0 else page.replace("</main>", sec + "</main>")
    page = _esg_page_chrome(page, depth=2)
    for k in {key, lk.clean(t)}:
        with open(os.path.join(esg_dir, f"{k}.html"), "w", encoding="utf-8") as fh:
            fh.write(page)
    print(f"ESG lookup written for {t}: {a['Grade']}")
    return 0


# --------------------------------------------------------------------------
def selftest():
    ok = True
    bs.PROFILES["XOMT"] = {"Industry": "Oil & Gas Integrated", "Name": "Oil Test"}
    bs.PROFILES["MSFT"] = {"Industry": "Software - Infrastructure", "Name": "Soft Test"}
    cases = [
        ("MSFT", dict(Status="ok", TotalRisk=15.0, Controversy=2, PeerGroup="Software & Services", Flags=""), "ESG leader"),
        ("BANK", dict(Status="ok", TotalRisk=24.0, Controversy=3, PeerGroup="Banks", Flags=""), "ESG qualified"),
        ("XOMT", dict(Status="ok", TotalRisk=25.0, Controversy=2, PeerGroup="Oil & Gas Producers", Flags=""), "Not qualified"),
        ("SMOK", dict(Status="ok", TotalRisk=18.0, Controversy=1, PeerGroup="Food Products", Flags="tobacco"), "Not qualified"),
        ("CONT", dict(Status="ok", TotalRisk=12.0, Controversy=4, PeerGroup="Retailing", Flags=""), "Not qualified"),
        ("HIGH", dict(Status="ok", TotalRisk=33.0, Controversy=1, PeerGroup="Chemicals", Flags=""), "Not qualified"),
        ("NONE", {"Status": "none"}, "No rating"),
    ]
    for t, r, want in cases:
        a = assess(t, r)
        good = a["Grade"] == want
        ok &= good
        h = brief_html(t, r, a)
        ok &= "UNGC" in h
        print(f"esg     : {t:<5} {a['Grade']:<14}" + ("" if good else f" EXPECTED {want}") +
              (f" SDGs {a['SDGs']}" if a["SDGs"] else ""))
    ok &= assess("MSFT", cases[0][1])["SDGs"] == [9]
    print("ESG SELFTEST", "PASSED" if ok else "FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser(description="ESG-only breakout scanner")
    ap.add_argument("--outdir", default="reports")
    ap.add_argument("--lookup", default=os.environ.get("TICKER", "") if os.environ.get("ESG_LOOKUP") else "")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return 0 if selftest() else 1
    if a.lookup:
        return run_lookup(a.outdir, a.lookup)
    return run_scan(a.outdir)


if __name__ == "__main__":
    sys.exit(main())
