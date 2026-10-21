"""ESG-only breakout scanner (separate tab: esg/index.html).

Modular add-on: it imports the main scanner's functions and never changes them, its files live in reports/esg/,
and it runs as its own workflow step (continue-on-error), so a problem here cannot stop the evening scan.

What "qualifies as ESG" means here
----------------------------------
A company qualifies when it is a member of an MSCI Choice ESG Screened index, published daily as the holdings of
  * iShares ESG Advanced MSCI USA ETF (USXF)      - MSCI USA Choice ESG Screened Index
  * iShares ESG Advanced MSCI Canada ETF (XCSR)   - MSCI Canada IMI Choice ESG Screened 10% Issuer Capped Index
These indexes keep companies with favourable MSCI ESG ratings and screen out
  * UN Global Compact violators (MSCI ESG Controversies) - the UNGC's ten principles on human rights (1-2),
    labour (3-6), environment (7-9) and anti-corruption (10);
  * controversial weapons (UN conventions on cluster munitions, landmines, chemical and biological weapons),
    nuclear weapons, civilian firearms, conventional weapons;
  * tobacco (WHO Framework Convention on Tobacco Control);
  * thermal coal, oil sands and fossil-fuel ties, energy-sector companies (Paris Agreement goals);
  * adult entertainment, alcohol, gambling, nuclear power, palm oil and other activities, with revenue limits.
Using a published, UN-aligned index (rather than home-made scores) keeps the screen consistent with how global ESG
funds and UN PRI signatories apply it. Each company is then linked to the UN Sustainable Development Goals its
line of business serves.

Why not ratings per company: Yahoo Finance removed its Sustainalytics ESG scores (Oct 2025), and the other
company-level ESG sources need a paid account or API key.

  python esg_scanner.py --outdir reports            # daily ESG scan -> reports/esg/index.html
  python esg_scanner.py --outdir reports --lookup X # ESG brief + full analysis -> reports/esg/lookup/X.html
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

import breakout_scanner as bs

e = lambda s: html.escape(str(s if s is not None else ""))

FUNDS = {
    "US": dict(fund="USXF", index="MSCI USA Choice ESG Screened Index", name="iShares ESG Advanced MSCI USA ETF",
               page="https://www.ishares.com/us/products/314365/ishares-esg-advanced-msci-usa-etf",
               urls=["https://www.ishares.com/us/products/314365/ishares-esg-advanced-msci-usa-etf/1467271812596.ajax"
                     "?fileType=csv&fileName=USXF_holdings&dataType=fund",
                     "https://www.ishares.com/us/products/314365/fund/1467271812596.ajax"
                     "?fileType=csv&fileName=USXF_holdings&dataType=fund",
                     "https://www.blackrock.com/us/individual/products/314365/ishares-esg-advanced-msci-usa-etf/"
                     "1464253357814.ajax?fileType=csv&fileName=USXF_holdings&dataType=fund",
                     "https://www.ishares.com/us/products/314365/ishares-esg-advanced-msci-usa-etf/latest-holdings.csv"]),
    "CA": dict(fund="XCSR", index="MSCI Canada IMI Choice ESG Screened 10% Issuer Capped Index",
               name="iShares ESG Advanced MSCI Canada Index ETF",
               page="https://www.blackrock.com/ca/investors/en/products/313580/ishares-esg-advanced-msci-canada-index-etf",
               urls=["https://www.blackrock.com/ca/investors/en/products/313580/ishares-esg-advanced-msci-canada-index-etf/"
                     "1464253357814.ajax?fileType=csv&fileName=XCSR_holdings&dataType=fund",
                     "https://www.blackrock.com/ca/investors/en/rbc/313580/fund/1586252180210.ajax"
                     "?fileType=csv&fileName=XCSR_holdings&dataType=fund"]),
}
ESG_ETFS = [("USXF", "iShares ESG Advanced MSCI USA"), ("XCSR.TO", "iShares ESG Advanced MSCI Canada"),
            ("ESGU", "iShares ESG Aware MSCI USA"), ("XESG.TO", "iShares ESG Aware MSCI Canada")]
MEMBER_COLS = ["Ticker", "Name", "Sector", "Weight", "Fund", "Region", "AsOf"]
MSCI_TOOL = "https://www.msci.com/our-solutions/esg-investing/esg-ratings-climate-search-tool"
SCREENS = ("controversial and nuclear weapons, civilian firearms, tobacco, thermal coal, oil sands and other fossil-fuel "
           "ties (energy sector excluded), plus adult entertainment, alcohol, gambling, conventional weapons, nuclear "
           "power and palm oil, with revenue limits")
FOSSIL = ["oil & gas", "coal", "uranium", "oil and gas"]
WEAPONS = ["aerospace & defense", "tobacco", "gambling", "beverages - wineries", "beverages - brewers",
           "beverages - distillers"]

UNGC = [  # (principles, area, what it covers)
    ("1-2", "Human rights", "Support human rights; no complicity in abuses"),
    ("3-6", "Labour", "Freedom of association; no forced or child labour; no discrimination"),
    ("7-9", "Environment", "Precaution, environmental responsibility, clean technology"),
    ("10", "Anti-corruption", "Work against corruption, extortion and bribery"),
]
SDG = {3: "Good health", 2: "Zero hunger", 4: "Quality education", 6: "Clean water", 7: "Clean energy",
       8: "Decent work & growth", 9: "Industry, innovation & infrastructure", 11: "Sustainable cities",
       12: "Responsible consumption", 13: "Climate action"}
SDG_MAP = [  # industry keyword -> SDGs (business link, not a measured impact)
    (("solar", "renewable", "wind"), (7, 13)), (("water",), (6,)), (("waste", "pollution", "environmental"), (11, 12)),
    (("medical", "drug", "biotech", "health", "diagnostic", "pharma"), (3,)), (("education",), (4,)),
    (("bank", "credit", "insurance", "capital markets", "asset management", "financ"), (8,)),
    (("railroad", "electrical equipment", "engineering", "construction", "infrastructure operations", "building"), (9, 11)),
    (("semiconductor", "software", "telecom", "communication", "information technology", "computer"), (9,)),
    (("farm", "agricultur", "grocery", "packaged food", "food"), (2,)),
    (("utilities - regulated electric", "utilities - diversified", "utilities - independent"), (7,)),
    (("residential", "reit - healthcare"), (11,)), (("auto", "electric vehicle"), (9, 13)),
    (("copper", "lithium", "industrial metals", "aluminum"), (7, 9)), (("staffing", "employment"), (8,)),
]


# --------------------------------------------------------------------------
# Index membership (daily holdings files), cached in reports/esg/members.csv
# --------------------------------------------------------------------------
def parse_holdings(text):
    """iShares / BlackRock holdings CSV: a few metadata lines, then a table starting with 'Ticker,'."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip('﻿"').startswith("Ticker")), None)
    if start is None:
        raise ValueError("no holdings table")
    asof = ""
    for l in lines[:start]:
        if "as of" in l.lower():
            m = re.search(r'"?([A-Z][a-z]{2}\.? \d{1,2}, \d{4})"?', l)
            if m:
                try:
                    asof = pd.Timestamp(m.group(1).replace(".", "")).date().isoformat()
                except Exception:
                    pass
            break
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df.columns = [str(c).strip() for c in df.columns]
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip() == "Equity"]
    df = df[df["Ticker"].notna() & (df["Ticker"].astype(str).str.strip() != "-")]
    w = "Weight (%)" if "Weight (%)" in df.columns else next((c for c in df.columns if c.startswith("Weight")), None)
    out = pd.DataFrame(dict(Ticker=df["Ticker"].astype(str).str.strip(), Name=df.get("Name", ""),
                            Sector=df.get("Sector", ""),
                            Weight=pd.to_numeric(df[w].astype(str).str.replace(",", ""), errors="coerce") if w else np.nan))
    return out, asof


def _norm(t):
    return re.sub(r"[^A-Z0-9]", "", str(t).upper().replace(".TO", ""))


def map_ticker(raw, reg, known):
    """Holdings tickers -> Yahoo symbols (BRKB -> BRK-B, RCI.B -> RCI-B.TO)."""
    n = _norm(raw)
    if (n, reg) in known:
        return known[(n, reg)]
    t = str(raw).strip().upper().replace(".", "-").replace(" ", "-")
    return t + ".TO" if reg == "CA" else t


def fetch_members(esg_dir, diag, universe=()):
    import requests
    known = {(_norm(t), bs.region(t)): t for t in universe}
    path = os.path.join(esg_dir, "members.csv")
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=MEMBER_COLS)
    frames = []
    for reg, f in FUNDS.items():
        got, tried = None, []
        for url in f["urls"]:
            try:
                r = requests.get(url, headers=bs._WEB, timeout=30)
                if r.ok and "Ticker" in r.text[:5000]:
                    got = parse_holdings(r.text)
                    break
                tried.append(f"{r.status_code}{'' if r.ok else ''}{' (not a holdings file)' if r.ok else ''}")
            except Exception as ex:
                tried.append(type(ex).__name__)
        if got is not None and len(got[0]) >= 30:
            df, asof = got
            df["Ticker"] = [map_ticker(t, reg, known) for t in df["Ticker"]]
            df["Fund"], df["Region"], df["AsOf"] = f["fund"], reg, asof or dt.date.today().isoformat()
            frames.append(df[MEMBER_COLS])
            diag.append(f"ESG list {f['fund']}: {len(df)} companies (holdings of {df['AsOf'].iloc[0]})")
        else:
            prev = old[old["Region"] == reg] if len(old) else old
            if len(prev):
                frames.append(prev[MEMBER_COLS])
                diag.append(f"ESG list {f['fund']}: download failed ({', '.join(tried)}), using the saved list from "
                            f"{prev['AsOf'].iloc[0]}")
            else:
                diag.append(f"ESG list {f['fund']}: download failed ({', '.join(tried)}) and no saved list")
    mem = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=MEMBER_COLS)
    mem = mem.drop_duplicates("Ticker")
    if len(mem):
        mem.to_csv(path, index=False)
    return {r["Ticker"]: r for r in mem.to_dict("records")}


def load_members(esg_dir):
    path = os.path.join(esg_dir, "members.csv")
    if not os.path.exists(path):
        return {}
    return {r["Ticker"]: r for r in pd.read_csv(path).to_dict("records")}


# --------------------------------------------------------------------------
# Assessment and the per-company ESG brief
# --------------------------------------------------------------------------
def sdgs_for(industry, sector=""):
    txt = f"{industry} {sector}".lower()
    out = []
    for keys, goals in SDG_MAP:
        if any(k in txt for k in keys):
            out += [g for g in goals if g not in out]
    return out


def assess(t, member, lists_ok=True, is_fund=False):
    prof = bs.PROFILES.get(t, {})
    industry, sector = str(prof.get("Industry") or ""), str(prof.get("Sector") or (member or {}).get("Sector") or "")
    sd = sdgs_for(industry, sector)
    if member:
        f = FUNDS[member["Region"]]
        w = member.get("Weight")
        wt = f" at {float(w):.2f}% of the fund" if w is not None and np.isfinite(float(w)) else ""
        return dict(Qualified=True, Grade="ESG qualified", SDGs=sd, Reasons=[
            f"Member of the {f['index']} (held by iShares {f['fund']}{wt}; holdings of {member.get('AsOf') or '-'}).",
            "The index keeps companies with favourable MSCI ESG ratings and removes UN Global Compact violators, so "
            "it is not flagged for a breach of the UNGC principles.",
            f"It passed the index's business screens: {SCREENS}."], Fails=[])
    if is_fund:
        return dict(Qualified=False, Grade="Not screened", SDGs=[], Fails=[], Reasons=[
            "This is a fund, not a company: the ESG screens apply to the companies it holds. Check the fund's own "
            "ESG fund rating on its provider's page."])
    if not lists_ok:
        return dict(Qualified=False, Grade="Unknown", SDGs=sd, Fails=[], Reasons=[
            "The ESG index lists could not be loaded, so this company could not be screened."])
    low = f"{industry} {sector}".lower()
    why = ("its fossil-fuel business: the index excludes the energy sector and companies tied to oil, gas or coal"
           if any(k in low for k in FOSSIL) or sector.lower() == "energy" else
           "its line of business: weapons, tobacco, alcohol and gambling are screened out" if any(k in low for k in WEAPONS)
           else "a lower MSCI ESG rating, a serious controversy or a screened business activity; or it is outside "
                "the index's universe (smaller companies, listings outside the US and Canada)")
    return dict(Qualified=False, Grade="Not qualified", SDGs=sd, Reasons=[], Fails=[
        f"Not a member of the MSCI Choice ESG Screened indexes. Most likely reason: {why}.",
        "Check the company's MSCI ESG rating and controversy flags in MSCI's free search tool (link below)."])


GRADE_COLOR = {"ESG qualified": "#16794a", "Not qualified": "#b42318", "Unknown": "#6b6b6b", "Not screened": "#6b6b6b"}


def brief_html(t, member, a, open_=False):
    """Why it qualifies, which UN criteria it meets, and the sustainability facts available for it."""
    col = GRADE_COLOR.get(a["Grade"], "#555")
    lines = "".join(f"<li>&#10003; {e(x)}</li>" for x in a["Reasons"]) + "".join(f"<li>&#10007; {e(x)}</li>" for x in a["Fails"])
    q = a["Qualified"]
    rows = "".join(
        f"<tr><td>{pr}</td><td><b>{area}</b><br><small>{what}</small></td>"
        f"<td><b class='{'up' if q else 'dn' if a['Grade'] == 'Not qualified' else ''}'>"
        f"{'Meets' if q else 'Not shown' if a['Grade'] == 'Not qualified' else '-'}</b><br>"
        f"<small>{'not flagged by the UNGC-violator screen' if q else 'not in a screened index'}</small></td></tr>"
        for pr, area, what in UNGC)
    sdg = " ".join(f'<span class="sdg">SDG {g} &middot; {SDG[g]}</span>' for g in a["SDGs"]) or \
        '<span class="hint">No direct SDG link from its line of business.</span>'
    prof = bs.PROFILES.get(t, {})
    m = member or {}
    w = m.get("Weight")
    facts = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in [
        ("ESG index", e(FUNDS[m["Region"]]["index"]) if m else "None"),
        ("Weight in the ESG fund", f"{float(w):.2f}%" if m and w is not None and np.isfinite(float(w)) else "-"),
        ("Sector", e(prof.get("Sector") or m.get("Sector") or "-")), ("Industry", e(prof.get("Industry") or "-")),
        ("UNGC violator screen", "Passed" if q else "-"), ("Fossil-fuel / weapons / tobacco screens", "Passed" if q else "-")])
    src = (f'<a href="{FUNDS[m["Region"]]["page"]}" target="_blank" rel="noopener">{FUNDS[m["Region"]]["fund"]} holdings and '
           f'screens</a> &middot; ' if m else "")
    return (f'<details class="esg"{" open" if open_ else ""}><summary><span class="eg" style="background:{col}">{a["Grade"]}</span> '
            f'ESG brief: why it {"qualifies" if q else "does not qualify"}</summary><ul class="ck">{lines}</ul>'
            f'<div class="wrap"><table class="ungc"><tr><th>UNGC</th><th>Area</th><th>Result</th></tr>{rows}</table></div>'
            f'<p style="margin:8px 0 4px"><b>UN Sustainable Development Goals</b> (by line of business)</p><p>{sdg}</p>'
            f'<div class="facts">{facts}</div>'
            f'<p class="hint">Sources: {src}<a href="{MSCI_TOOL}" target="_blank" rel="noopener">MSCI ESG rating for this '
            f'company</a> (free search). Index membership is MSCI\'s screen, not a certification; company-level ESG scores need '
            f'a paid data source.</p></details>')


ESG_CSS = """
details.esg{background:#f3f8f4;border:1px solid #cfe3d4;border-radius:10px;padding:8px 10px;margin:8px 0}
details.esg summary{cursor:pointer;font-weight:600}
details.esg ul.ck{list-style:none;padding-left:2px}
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
window.esgLook = async () => {
  const st = document.getElementById('runstatus');
  const k = (document.getElementById('elk').value || '').trim().toUpperCase().replace(/ +/g, '');
  if (!/^[A-Z0-9][A-Z0-9.=^-]{0,14}$/.test(k)) { st.textContent = 'Type a ticker, e.g. RY.TO or MSFT.'; return; }
  const here = location.pathname.includes('/esg/lookup/') ? '../' : '';
  const cands = k.includes('.') ? [k, k.replace('.', '-')] : [k, k + '.TO'];
  let list = null;
  try { list = await (await fetch(here + 'companies.json?t=' + Date.now(), { cache: 'no-store' })).json(); } catch (e) {}
  const hit = list && cands.find(c => list.items && list.items[c]);
  if (hit) { location.href = here + 'company.html?t=' + encodeURIComponent(hit); return; }
  for (const c of cands) {
    try { const r = await fetch(here + 'lookup/' + c + '.html?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) { location.href = here + 'lookup/' + c + '.html'; return; } } catch (e) {}
  }
  st.innerHTML = '<b>' + k + '</b> is not in the ESG indexes, so it does not qualify as ESG here (or the ticker is not listed in '
    + 'the US or Canada). <a href="#" id="esgfull">Run a full ESG check with the reasons</a> (about 3 minutes).';
  document.getElementById('esgfull').onclick = ev => { ev.preventDefault(); runNow('esg_lookup', k); };
};
</script>"""


def _esg_page_chrome(page, depth=1):
    """Re-point a page made by the main report writer to the ESG tab: links, title, search box."""
    up = "../" * depth
    if depth == 2:                     # a lookup page: its own links point at the main site
        page = page.replace('href="../', 'href="../../')
    page = re.sub(r'<form class="lookbar".*?</form>', lambda m: ESG_SEARCH, page, count=1, flags=re.S)
    for name in ("index.html", "live.html", "morning.html", "backtest.html", "archive.html"):
        page = page.replace(f'href="{name}"', f'href="{up}{name}"')
    nav = (f'<p class="links"><b>ESG scanner</b> &middot; <a href="{up}index.html">Main scanner</a> &middot; '
           f'<a href="{"" if depth == 1 else "../"}index.html">ESG home</a> &middot; <a href="{up}app/">App</a></p>')
    page = page.replace("<main>", "<main>" + nav, 1)
    return page.replace("</style>", ESG_CSS + "</style>", 1)


# --------------------------------------------------------------------------
# Daily ESG scan
# --------------------------------------------------------------------------
def summary_html(asof, qual, members, data, res, radar, diag):
    ca = [t for t in qual if bs.region(t) == "CA"]

    def fmt(x):
        return f'<b class="{"up" if x >= 0 else "dn"}">{x:+.1f}%</b>' if np.isfinite(x) else "-"

    def avg1(ts):
        xs = [bs._chg(data[t], 1) for t in ts if t in data and len(data[t]) > 2]
        xs = [x for x in xs if np.isfinite(x)]
        return np.mean(xs) if xs else np.nan
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
    secs = {}
    for t in qual:
        s = str(members[t].get("Sector") or bs.PROFILES.get(t, {}).get("Sector") or "Other")
        secs[s] = secs.get(s, 0) + 1
    sec_txt = ", ".join(f"{k} {v}" for k, v in sorted(secs.items(), key=lambda z: -z[1])[:7])
    nb = len(res)
    gate = int(res["Gate"].sum()) if nb and "Gate" in res else 0
    us_avg = avg1([t for t in qual if bs.region(t) == "US"])
    ca_avg = avg1(ca)
    return f"""<section class="review esgsum"><h2>ESG daily summary &middot; close of {pd.Timestamp(asof):%a %b %d}</h2>
<p><b>{len(qual)}</b> companies qualify ({len(qual) - len(ca)} US in the MSCI USA Choice ESG Screened Index, {len(ca)} Canadian in
the MSCI Canada Choice ESG Screened Index) with enough trading volume to scan.</p>
<p>ESG breakouts: <b>{nb}</b> ({gate} pass the Buy gate); on the ESG radar: <b>{len(radar)}</b>.</p>
<p>Average move of the qualified companies on the day: US {fmt(us_avg)}, Canada {fmt(ca_avg)}.</p>
<p>Biggest ESG gainers: {mv([m for m in movers[::-1] if m[0] > 0][:5])}<br>Biggest ESG decliners: {mv([m for m in movers if m[0] < 0][:5])}</p>
<p>Qualified companies by sector: {e(sec_txt)}</p>
<div class="wrap"><table class="mr"><tr><th class="tk">ESG funds vs the market</th><th>Last</th><th>1 day</th><th>1 month</th><th>YTD</th></tr>{etf_rows}</table></div>
<details><summary>How companies qualify (UN standards)</summary><ul class="hint">
<li><b>UN Global Compact</b> (human rights, labour, environment, anti-corruption): the indexes remove companies MSCI flags as UNGC violators.</li>
<li><b>Global exclusions</b>: {SCREENS}.</li>
<li><b>ESG quality</b>: only companies with favourable MSCI ESG ratings stay in the index (the screen UN PRI signatory funds use).</li>
<li><b>UN SDGs</b>: each company's line of business is linked to the goals it serves.</li>
<li>Breakouts, radar, scores, stops and targets use exactly the same rules as the main scanner.</li>
<li>Lists: daily holdings of iShares USXF and XCSR. {' '.join(e(x) for x in diag if x.startswith('ESG list'))}</li></ul></details>
</section>"""


def list_html(qual, members, data):
    rows = ""
    order = sorted(qual, key=lambda t: (bs.region(t) != "CA", -float(members[t].get("Weight") or 0)))
    for t in order:
        m, d = members[t], data.get(t)
        name = bs.PROFILES.get(t, {}).get("Name") or str(m.get("Name") or "").title()
        ch = bs._chg(d, 1) if d is not None and len(d) > 2 else np.nan
        trend = ""
        if d is not None and len(d) > 200 and pd.notna(d["SMA200"].iloc[-1]):
            c, s50, s200 = d["Close"].iloc[-1], d["SMA50"].iloc[-1], d["SMA200"].iloc[-1]
            trend = "Uptrend" if c > s50 > s200 else "Downtrend" if c < s50 < s200 else "Mixed"
        w = float(m.get("Weight") or np.nan)
        rows += (f'<tr data-k="{e(t)} {e(name.upper())}"><td class="tk">{e(t)}</td><td class="cn">{e(name)}</td>'
                 f'<td>{e(m.get("Sector") or "")}</td><td>{f"{w:.2f}%" if np.isfinite(w) else "-"}</td>'
                 f'<td class="{"up" if ch >= 0 else "dn"}">{f"{ch:+.1f}%" if np.isfinite(ch) else "-"}</td>'
                 f'<td class="{trend.lower()}">{trend}</td></tr>')
    return (f'<h2>All qualified ESG companies ({len(qual)})</h2><p class="hint">Canada first, then by weight in the ESG '
            f'fund. Type in the ESG search box to filter; <b>ESG analyze</b> gives any ticker a full ESG and breakout report.</p>'
            f'<div class="wrap"><table class="esgtab" id="esglist"><tr><th>Ticker</th><th>Company</th><th>Sector</th>'
            f'<th>Fund weight</th><th>Today</th><th>Trend</th></tr>{rows}</table></div>')


def run_scan(outdir):
    t0 = time.time()
    esg_dir = os.path.join(outdir, "esg")
    os.makedirs(os.path.join(esg_dir, "lookup"), exist_ok=True)
    diag = []
    bs.PROFILES.update(bs._load_profiles(os.path.join(outdir, "sectors.csv")))      # read-only: main scanner's cache
    bs.PROFILES.update(bs._load_profiles(os.path.join(esg_dir, "sectors.csv")))
    try:
        known = bs.build_universe(["tsx", "sp500"])           # only to map holdings symbols to Yahoo tickers
    except Exception:
        known = {}
    members = fetch_members(esg_dir, diag, known)
    universe = sorted(members, key=lambda t: (bs.region(t) != "CA", t))
    print(f"ESG universe: {len(universe)} index members")
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
    qual = {}
    for t in universe:
        d = data.get(t)
        if d is None or len(d) < 60:
            continue
        min_dv = bs.CFG["min_dollar_vol_ca"] if bs.region(t) == "CA" else bs.CFG["min_dollar_vol"]
        if d["Close"].iloc[-1] >= bs.CFG["min_price"] and float((d["Close"] * d["Volume"]).iloc[-50:].mean()) >= min_dv:
            qual[t] = assess(t, members[t])
    diag.append(f"ESG: {len(universe)} members, prices for {sum(1 for t in universe if t in data)}, "
                f"{len(qual)} liquid enough to scan")
    print(diag[-1])

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
        idx = f"ESG ({members[t]['Fund']})"
        if r:
            r.update(Ticker=t, Indexes=idx)
            found.append((r, A))
        elif f:
            f.update(Ticker=t, Indexes=idx, Region=bs.region(t))
            radar.append(f)
    radar = bs.radar_merge(radar, os.path.join(esg_dir, "radar.csv"), save=True)
    ctx = bs.build_context(data, list(qual))
    try:
        sectors, industries, earnings = bs.fetch_sector_earnings(
            [r["Ticker"] for r, _ in found], os.path.join(esg_dir, "sectors.csv"),
            profile_only=[x["Ticker"] for x in radar])
    except Exception as ex:
        sectors, industries, earnings = {}, {}, {}
        diag.append(f"ESG sector/earnings lookup failed: {str(ex)[:100]}")
    ctx.update(sector=sectors, industry=industries, earnings=earnings)
    for t in qual:                                        # refresh SDG links now that more industries are known
        qual[t] = assess(t, members[t])
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
    bs.company_html = lambda t, full=True: orig(t, full) + (brief_html(t, members.get(t), qual[t]) if t in qual else "")
    review = summary_html(asof, qual, members, data, res, radar, diag) + list_html(qual, members, data)
    path = os.path.join(esg_dir, "index.html")
    try:
        bs.write_html(path, asof, rows, res, data, bs.market_context(data), len(qual), review, title="ESG scanner",
                      subtitle=f"ESG-only scan of {len(qual)} companies in the MSCI Choice ESG Screened indexes", radar=radar)
    finally:
        bs.company_html = orig
    page = _esg_page_chrome(open(path, encoding="utf-8").read())
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    try:
        write_companies(esg_dir, asof, qual, members, data, rows, radar)
    except Exception as ex:
        diag.append(f"ESG company briefs failed: {str(ex)[:100]}")
    json.dump(dict(asof=asof, members=len(members), qualified=len(qual), breakouts=len(res), radar=len(radar),
                   diag=diag, minutes=round((time.time() - t0) / 60, 1)),
              open(os.path.join(esg_dir, "status.json"), "w"))
    print("\n".join(diag))
    print(f"ESG scan: {len(qual)} qualified, {len(res)} breakouts, {len(radar)} on radar in {time.time() - t0:.0f}s -> {path}")
    return 0


# --------------------------------------------------------------------------
# Instant ESG briefs: one small JSON for every qualified company + a page that shows one
# --------------------------------------------------------------------------
def write_companies(esg_dir, asof, qual, members, data, rows, radar):
    open_rows = {r["Ticker"]: r for r in rows}
    rad = {x["Ticker"]: x for x in radar}
    num = lambda x, nd=2: round(float(x), nd) if x is not None and np.isfinite(float(x)) else None
    items = {}
    for t, a in qual.items():
        d, m, p = data.get(t), members[t], bs.PROFILES.get(t) or {}
        it = dict(name=p.get("Name") or str(m.get("Name") or t).title(), sector=p.get("Sector") or m.get("Sector") or "",
                  industry=p.get("Industry") or "", country=p.get("Country") or "", website=p.get("Website") or "",
                  summary=bs._short_summary(p["Summary"]) if p.get("Summary") else "",
                  brief=brief_html(t, m, a, open_=True))
        if d is not None and len(d) > 2:
            it.update(price=num(d["Close"].iloc[-1]), chg1=num(bs._chg(d, 1), 1), chg21=num(bs._chg(d, 21), 1),
                      ytd=num(bs._ytd(d), 1))
            if len(d) > 200:
                label, color, what, _ = bs.holding_status(d)
                it.update(trend=label, trend_color=color, trend_what=what)
        if t in open_rows:
            r = open_rows[t]
            label, color, _ = bs.STATUS_INFO.get(r["Status"], (r["Status"], "#555", ""))
            sc = bs._num(r.get("Score"))
            it["setup"] = dict(kind="breakout", label=label, color=color, score=round(sc) if np.isfinite(sc) else None,
                               note=bs._plain(r.get("Note")))
        elif t in rad:
            x = rad[t]
            it["setup"] = dict(kind="radar", label="On the radar", color="#1d5fbf",
                               note=f"Close to a breakout: it needs a close above {bs._f(x.get('Trigger'))} on strong volume "
                                    f"({bs._f(x.get('DistPct'), 1)}% away).")
        items[t] = it
    with open(os.path.join(esg_dir, "companies.json"), "w") as f:
        json.dump(dict(asof=asof, items=items), f, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    run = bs.RUN_HTML.replace('<form class="lookbar"', '<form class="lookbar" style="display:none"')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ESG brief</title>
<style>{bs.PAGE_CSS}{bs.RUN_CSS}{ESG_CSS}.runbar button{{display:none}}.px2{{font-size:22px;font-weight:700}}</style></head>
<body><main><p class="links"><a href="index.html">&larr; ESG home</a></p>
{run}
<div id="out"><p class="hint">Loading...</p></div>
<p class="hint">ESG screen and breakout rules as in the ESG scanner. Not investment advice.</p></main>
<script>
(async function () {{
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
  const pc = x => x == null ? '-' : `<b class="${{x >= 0 ? 'up' : 'dn'}}">${{x >= 0 ? '+' : ''}}${{x.toFixed(1)}}%</b>`;
  const t = new URLSearchParams(location.search).get('t') || '';
  const out = document.getElementById('out');
  let j = null;
  try {{ j = await (await fetch('companies.json?t=' + Date.now(), {{ cache: 'no-store' }})).json(); }} catch (e) {{}}
  const c = j && j.items && j.items[t];
  if (!c) {{ out.innerHTML = `<h1>${{esc(t)}}</h1><p>${{esc(t)}} is not in today's ESG list.</p>`; return; }}
  const day = new Date(j.asof + 'T12:00:00').toLocaleDateString('en-US', {{ weekday: 'short', month: 'short', day: 'numeric' }});
  const s = c.setup;
  out.innerHTML = `<h1>${{esc(c.name)}} <small class="hint">${{esc(t)}}</small></h1>
   <p class="hint">${{[c.industry, c.sector, c.country].filter(Boolean).map(esc).join(' &middot; ')}}</p>
   <p class="hint">Prices at the close of ${{day}} (updated after each evening scan).</p>
   ${{c.summary ? `<p>${{esc(c.summary)}}</p>` : ''}}
   <div class="facts"><div><span>Price</span><b>${{c.price != null ? c.price.toFixed(2) : '-'}}</b></div>
     <div><span>Day</span>${{pc(c.chg1)}}</div><div><span>1 month</span>${{pc(c.chg21)}}</div><div><span>Year to date</span>${{pc(c.ytd)}}</div></div>
   ${{c.trend ? `<p><span class="eg" style="background:${{esc(c.trend_color)}}">${{esc(c.trend)}}</span> ${{esc(c.trend_what)}}</p>` : ''}}
   <h2>Breakout</h2><p>${{s ? `<span class="eg" style="background:${{esc(s.color)}}">${{esc(s.label)}}</span>${{s.score != null ? ' score ' + s.score + ' &middot; ' : ' '}}${{esc(s.note)}}` : 'No breakout setup right now.'}}</p>
   <h2>ESG</h2>${{c.brief}}
   <p><a href="#" class="btn" id="full" style="display:inline-block;background:var(--accent,#1d5fbf);color:#fff;padding:9px 13px;border-radius:9px;text-decoration:none;font-weight:600">Full report: earnings, valuation, long-term view</a></p>`;
  document.getElementById('full').onclick = async ev => {{ ev.preventDefault();
    try {{ const r = await fetch('lookup/' + t + '.html?t=' + Date.now(), {{ cache: 'no-store' }}); if (r.ok) {{ location.href = 'lookup/' + t + '.html'; return; }} }} catch (e) {{}}
    runNow('esg_lookup', t); window.scrollTo(0, 0); }};
}})();
</script></body></html>"""
    with open(os.path.join(esg_dir, "company.html"), "w", encoding="utf-8") as f:
        f.write(page)


# --------------------------------------------------------------------------
# ESG lookup: ESG brief on top of the regular ticker analysis
# --------------------------------------------------------------------------
def run_lookup(outdir, ticker):
    import lookup as lk
    key = lk.clean(ticker)
    esg_dir = os.path.join(outdir, "esg")
    os.makedirs(os.path.join(esg_dir, "lookup"), exist_ok=True)
    if not key:
        print("No valid ticker given.")
        return 1
    t, raw = lk.fetch(key)
    out = os.path.join(esg_dir, "lookup", f"{key}.html")
    if t is None:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                     f'<body style="font-family:sans-serif;margin:16px"><p><a href="../index.html">ESG home</a></p>'
                     f'<h1>{e(key)}</h1><p>No price data found for {e(key)} (also tried {e(key)}.TO).</p></body>')
        return 0
    diag = []
    members = load_members(esg_dir)
    if not members:
        members = fetch_members(esg_dir, diag)
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
    is_fund = str(info.get("quoteType") or "").upper() in ("ETF", "MUTUALFUND")
    a = assess(t, members.get(t), lists_ok=bool(members), is_fund=is_fund)
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
    sec = f'<section><h2>ESG assessment (UN standards)</h2>{brief_html(t, members.get(t), a, open_=True)}</section>'
    i = page.find("<h2")
    page = page[:i] + sec + page[i:] if i > 0 else page.replace("</main>", sec + "</main>")
    page = _esg_page_chrome(page, depth=2)
    for k in {key, lk.clean(t)}:
        with open(os.path.join(esg_dir, "lookup", f"{k}.html"), "w", encoding="utf-8") as fh:
            fh.write(page)
    print(f"ESG lookup written for {t}: {a['Grade']}")
    return 0


# --------------------------------------------------------------------------
def selftest():
    ok = True
    us = ('Fund Holdings as of,"Oct 17, 2026"\nInception Date,"Jun 16, 2020"\n \n'
          'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location\n'
          '"MSFT","MICROSOFT CORP","Information Technology","Equity","1,000","6.51","1,000","10","400","United States"\n'
          '"BRKB","BERKSHIRE HATHAWAY INC CLASS B","Financials","Equity","1,000","1.20","1,000","10","400","United States"\n'
          '"USD","USD CASH","Cash and/or Derivatives","Cash","10","0.05","10","10","1","United States"\n'
          '"-","-","-","-","-","-","-","-","-","-"\n')
    df, asof = parse_holdings(us)
    good = list(df["Ticker"]) == ["MSFT", "BRKB"] and asof == "2026-10-17" and abs(df["Weight"].iloc[0] - 6.51) < 1e-9
    known = {(_norm(t), bs.region(t)): t for t in ["BRK-B", "RCI-B.TO", "RY.TO"]}
    good &= map_ticker("BRKB", "US", known) == "BRK-B" and map_ticker("RCI.B", "CA", known) == "RCI-B.TO"
    good &= map_ticker("RY", "CA", known) == "RY.TO" and map_ticker("GIB.A", "CA", {}) == "GIB-A.TO"
    print("esg     : holdings parse and ticker mapping " + ("ok" if good else "FAILED"))
    ok &= good
    bs.PROFILES["MSFT"] = {"Industry": "Software - Infrastructure", "Sector": "Technology", "Name": "Microsoft"}
    bs.PROFILES["XOMT"] = {"Industry": "Oil & Gas Integrated", "Sector": "Energy"}
    bs.PROFILES["LMTT"] = {"Industry": "Aerospace & Defense", "Sector": "Industrials"}
    mem = dict(Ticker="MSFT", Region="US", Weight=6.51, AsOf="2026-10-17", Sector="Information Technology")
    for t, m, kw, want, needle in [("MSFT", mem, {}, "ESG qualified", "UN Global Compact"),
                                   ("XOMT", None, {}, "Not qualified", "fossil-fuel"),
                                   ("LMTT", None, {}, "Not qualified", "weapons"),
                                   ("XIU.TO", None, dict(is_fund=True), "Not screened", "fund"),
                                   ("ZZZ", None, dict(lists_ok=False), "Unknown", "could not be loaded")]:
        a = assess(t, m, **kw)
        h = brief_html(t, m, a)
        good = a["Grade"] == want and needle in " ".join(a["Reasons"] + a["Fails"]) and "UNGC" in h
        ok &= good
        print(f"esg     : {t:<6} {a['Grade']:<14}" + ("" if good else " FAILED") + (f" SDGs {a['SDGs']}" if a["SDGs"] else ""))
    ok &= assess("MSFT", mem)["SDGs"] == [9]
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
