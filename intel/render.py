"""Render a brief as a web page (app tab), an archive/search page, and Telegram messages."""
import datetime as dt
import html
import json

from .config import SECTION_BY_ID

e = lambda s: html.escape(str(s if s is not None else ""), quote=True)

LABELS = dict(summary="", why_it_matters="Why it matters", economic_impact="Economic impact",
              market_impact="Market impact", investment_implications="Investment implications",
              opportunities="Opportunities", risks="Risks")

CSS = """
:root{--bg:#f4f6f9;--card:#fff;--ink:#14202e;--muted:#5b6b7d;--line:#dde3ea;--accent:#1d5fbf;--up:#16794a;--dn:#b42318;--chip:#eef2f7;--hl:#fff7e0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e141b;--card:#17202a;--ink:#e6edf3;--muted:#9aa7b4;--line:#2a3542;--accent:#6aa5ff;--up:#4ade80;--dn:#f87171;--chip:#22303e;--hl:#2a2614}}
:root[data-theme="dark"]{--bg:#0e141b;--card:#17202a;--ink:#e6edf3;--muted:#9aa7b4;--line:#2a3542;--accent:#6aa5ff;--up:#4ade80;--dn:#f87171;--chip:#22303e;--hl:#2a2614}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:820px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:20px;margin:6px 0 2px;letter-spacing:.2px}h2{font-size:17px;margin:22px 0 8px;scroll-margin-top:56px}
.meta{color:var(--muted);font-size:13px}.banner{background:var(--hl);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:10px 0;font-size:14px}
nav.chips{position:sticky;top:0;z-index:5;background:var(--bg);display:flex;gap:6px;overflow-x:auto;padding:8px 0;margin:0 -16px;padding-left:16px;padding-right:16px;scrollbar-width:none}
nav.chips a{flex:0 0 auto;background:var(--chip);color:var(--ink);text-decoration:none;font-size:13px;font-weight:600;padding:5px 10px;border-radius:999px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px}
.card h3{font-size:15.5px;margin:0 0 4px}.card p{margin:6px 0}.lbl{font-weight:650}
.src{font-size:12.5px;color:var(--muted);margin-top:6px}.src a{color:var(--accent);text-decoration:none}
.tag{display:inline-block;background:var(--chip);color:var(--muted);border-radius:6px;padding:1px 7px;font-size:12px;margin:0 4px 2px 0}
.ov{color:var(--muted);margin:-2px 0 10px}
.mk{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}
.mk div{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:6px 9px;font-size:13px}.mk span{display:block;color:var(--muted);font-size:12px}
.up{color:var(--up);font-weight:650}.dn{color:var(--dn);font-weight:650}
.ceo h3{margin-top:10px}.ceo ol{margin:4px 0 0;padding-left:20px}.ceo li{margin:5px 0}
.srcs li{margin:4px 0;font-size:13px;word-break:break-word}
a{color:var(--accent)}input{font:inherit;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);width:100%}
.bar{height:8px;background:var(--accent);border-radius:4px;display:inline-block;vertical-align:middle;margin-left:6px}
"""


def _fmt_chg(m):
    c = m["chg"]
    cls = "up" if c > 0 else "dn" if c < 0 else ""
    unit = " pts" if m["kind"] == "yield" else "%"
    return f'<b class="{cls}">{c:+.2f}{unit}</b>'


def _src_links(ids, arts):
    out = []
    for s in ids:
        a = arts.get(s)
        if not a:
            continue
        day = a.get("published", "")[:10]
        note = " (site blocks automated checks)" if a.get("check") == "protected" else ""
        out.append(f'<a href="{e(a["url"])}" target="_blank" rel="noopener">{e(a["source"])}</a> {e(day)}{note}')
    return " &middot; ".join(out)


def _item_html(it, arts, sid):
    parts = [f'<h3>{e(it["headline"])}</h3>']
    if it.get("tag"):
        parts.insert(0, f'<span class="tag">{e(it["tag"])}</span>')
    for k, lbl in LABELS.items():
        v = (it.get(k) or "").strip()
        if v:
            parts.append(f'<p>{f"<span class=lbl>{lbl}:</span> " if lbl else ""}{e(v)}</p>')
    if it.get("themes"):
        parts.append("<div>" + "".join(f'<span class="tag">{e(t)}</span>' for t in it["themes"]) + "</div>")
    parts.append(f'<div class="src">Sources: {_src_links(it["sources"], arts)}</div>')
    return '<div class="card">' + "".join(parts) + "</div>"


def page(brief, nav_links=True):
    arts = brief.get("articles", {})
    gen = brief.get("generated_at", "")
    try:
        g = dt.datetime.fromisoformat(gen)
        gen_txt = g.strftime("%-I:%M %p ET").lower().replace(" et", " ET")
    except Exception:
        gen_txt = gen
    weekly = brief.get("kind") == "weekly"
    secs = brief.get("sections") or {}
    if weekly:
        secs = {"weekly": dict(title="Top developments of the week", overview=brief.get("overview", ""),
                               items=brief.get("items", []))}
    chips = ['<a href="#markets">Markets</a>'] if brief.get("market") else []
    chips += [f'<a href="#{sid}">{e(SECTION_BY_ID.get(sid, {}).get("title", sec.get("title", sid)))}</a>'
              for sid, sec in secs.items() if sec.get("items")]
    if brief.get("ceo"):
        chips.append('<a href="#ceo">CEO Brief</a>')
    chips.append('<a href="#sources">Sources</a>')
    body = []
    if brief.get("mode") == "headlines":
        body.append('<div class="banner"><b>Headlines only.</b> The AI analysis is off because no AI key is set yet; '
                    'these are the most important verified stories with their own summaries.</div>')
    for err in brief.get("errors", [])[:3]:
        body.append(f'<div class="banner">Part of this brief could not be written: {e(err)}</div>')
    if brief.get("market"):
        tiles = "".join(f'<div><span>{e(m["name"])}</span>{m["last"]:,} {_fmt_chg(m)}</div>' for m in brief["market"])
        body.append(f'<h2 id="markets">Markets this morning</h2><p class="ov">Last value vs the previous close '
                    f'(futures and Europe are live overnight).</p><div class="mk">{tiles}</div>')
    if weekly and brief.get("rising_themes"):
        body.append('<h2>Rising themes</h2>' + "".join(
            f'<div class="card"><h3>{e(t["theme"])}</h3><p>{e(t["why"])}</p></div>' for t in brief["rising_themes"]))
    for sid, sec in secs.items():
        if not sec.get("items"):
            continue
        title = SECTION_BY_ID.get(sid, {}).get("title", sec.get("title", sid))
        body.append(f'<h2 id="{e(sid)}">{e(title)}</h2>')
        if sec.get("overview"):
            body.append(f'<p class="ov">{e(sec["overview"])}</p>')
        body += [_item_html(it, arts, sid) for it in sec["items"]]
    ceo = brief.get("ceo")
    if ceo:
        blk = []
        for k, t in (("know", "3 Things I Should Know Today"), ("opportunities", "3 Opportunities Worth Watching"),
                     ("risks", "3 Risks Worth Monitoring")):
            pts = "".join(f'<li>{e(p["text"])} <span class="src">{_src_links(p["sources"], arts)}</span></li>'
                          for p in ceo.get(k, []))
            blk.append(f"<h3>{t}</h3><ol>{pts}</ol>")
        body.append(f'<h2 id="ceo">CEO Brief</h2><div class="card ceo">{"".join(blk)}</div>')
    src_rows = sorted(arts.values(), key=lambda a: (a["source"], a.get("published", "")))
    body.append('<h2 id="sources">Sources</h2><ol class="srcs">' + "".join(
        f'<li><b>{e(a["source"])}</b>, {e(a.get("published", "")[:10])}: <a href="{e(a["url"])}" target="_blank" '
        f'rel="noopener">{e(a["title"])}</a></li>' for a in src_rows) + "</ol>")
    st = brief.get("stats") or {}
    ai = st.get("ai") or {}
    foot = (f'{st.get("collected", "?")} articles from {st.get("sources_ok", "?")} of {st.get("sources_total", "?")} '
            f'sources, {st.get("stories", "?")} stories; links checked: '
            f'{", ".join(f"{k} {v}" for k, v in (st.get("links_checked") or {}).items())}.'
            + (f' AI: {ai.get("model")}, {ai.get("calls")} calls, ${ai.get("cost_usd", 0):.2f}.' if ai else ""))
    nav = ('<p class="meta"><a href="archive.html">Archive &amp; search</a> &middot; <a href="weekly.html">Weekly summary</a>'
           ' &middot; <a href="../app/">App</a></p>') if nav_links else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(brief.get("title"))}</title>
<style>{CSS}</style></head><body><main>
<h1>{e(brief.get("title", "Global Intelligence & Wealth Brief")).upper()}</h1>
<p class="meta">{e(brief.get("date_label"))} &middot; generated {e(gen_txt)}</p>{nav}
<nav class="chips">{"".join(chips)}</nav>
{"".join(body)}
<p class="meta" style="margin-top:20px">{e(foot)} Sources are publicly accessible; every link was checked when the
brief was made. Analysis is AI-generated from those articles and is not investment advice.</p>
</main></body></html>"""


def archive_page():
    """Static page; search runs in the browser over index.json, trends from trends.json."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Brief archive</title>
<style>{CSS}</style></head><body><main>
<h1>BRIEF ARCHIVE</h1><p class="meta"><a href="index.html">Today's brief</a> &middot; <a href="weekly.html">Weekly summary</a></p>
<input id="q" placeholder="Search every brief: e.g. Bank of Canada, Dubai real estate, ETF" autocomplete="off">
<div id="res"></div>
<h2>Trending themes (last 8 weeks)</h2><div id="trends" class="card"><p class="meta">Loading...</p></div>
<h2>Past briefs</h2><div id="days" class="card"><p class="meta">Loading...</p></div>
<script>
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const get = u => fetch(u + '?t=' + Date.now(), {{cache: 'no-store'}}).then(r => r.ok ? r.json() : []).catch(() => []);
Promise.all([get('index.json'), get('trends.json')]).then(([idx, tr]) => {{
  const days = [...new Set(idx.filter(r => r.kind === 'daily').map(r => r.date))];
  document.getElementById('days').innerHTML = days.length ? days.map(d => `<a href="d/${{d}}.html">${{d}}</a>`).join(' &middot; ') : '<p class="meta">None yet.</p>';
  const max = Math.max(1, ...tr.map(t => t.this_week));
  document.getElementById('trends').innerHTML = tr.length ? tr.slice(0, 15).map(t => `<div style="margin:5px 0">${{esc(t.theme)}}
    ${{t.rising ? '<span class="tag" style="color:var(--up)">rising</span>' : ''}}<span class="bar" style="width:${{Math.round(120 * t.this_week / max)}}px"></span>
    <span class="meta">${{t.this_week}} this week, ${{t.avg_before}}/week before</span></div>`).join('') : '<p class="meta">Builds up after a few days of briefs.</p>';
  const q = document.getElementById('q'), res = document.getElementById('res');
  q.oninput = () => {{
    const terms = q.value.toLowerCase().split(/\\s+/).filter(t => t.length > 1);
    if (!terms.length) {{ res.innerHTML = ''; return; }}
    const hits = idx.filter(r => terms.every(t => (r.headline + ' ' + r.summary + ' ' + r.themes.join(' ')).toLowerCase().includes(t))).slice(0, 40);
    res.innerHTML = `<p class="meta">${{hits.length}} result${{hits.length === 1 ? '' : 's'}}</p>` + hits.map(r => `<div class="card">
      <span class="tag">${{esc(r.date)}}</span><span class="tag">${{esc(r.title)}}</span><h3><a href="${{r.kind === 'weekly' ? 'w/' : 'd/'}}${{esc(r.date)}}.html">${{esc(r.headline)}}</a></h3>
      <p>${{esc(r.summary)}}</p><div class="src">${{r.sources.map(s => `<a href="${{esc(s.url)}}" target="_blank" rel="noopener">${{esc(s.source)}}</a>`).join(' &middot; ')}}</div></div>`).join('');
  }};
}});
</script></main></body></html>"""


# ------------------------------------------------------------------ Telegram
def _t(s):
    return html.escape(str(s or ""), quote=False)


def telegram_messages(brief, sections=None, full=False, url=""):
    """Telegram HTML messages (each under 4,000 characters). sections=None: the daily push (exec + CEO)."""
    arts = brief.get("articles", {})
    secs = brief.get("sections") or {}
    if brief.get("kind") == "weekly":
        secs = {"weekly": dict(title="Top developments of the week", overview=brief.get("overview", ""),
                               items=brief.get("items", []))}
    head = f"<b>{_t(brief.get('title', '')).upper()}</b>\n{_t(brief.get('date_label'))}"
    if brief.get("mode") == "headlines":
        head += "\n<i>Headlines only (AI analysis is off).</i>"
    blocks = [head]
    if sections is None:
        want = list(secs) if full else ["executive_summary", "weekly"]
    else:
        want = sections
    if brief.get("market") and (sections is None or "financial_markets" in want):
        key = [m for m in brief["market"] if m["ticker"] in ("^GSPC", "^GSPTSE", "ES=F", "^TNX", "CAD=X", "CL=F", "GC=F")]
        blocks.append("<b>Markets</b>\n" + "\n".join(
            f"{_t(m['name'])}: {m['last']:,} ({m['chg']:+.2f}{' pts' if m['kind'] == 'yield' else '%'})" for m in key))
    for sid in want:
        sec = secs.get(sid)
        if not sec or not sec.get("items"):
            continue
        title = SECTION_BY_ID.get(sid, {}).get("title", sec.get("title", sid))
        blocks.append(f"<b>{_t(title.upper())}</b>" + (f"\n<i>{_t(sec['overview'])}</i>" if sec.get("overview") else ""))
        for i, it in enumerate(sec["items"], 1):
            links = " ".join(f'<a href="{_t(arts[s]["url"])}">{_t(arts[s]["source"])}</a>'
                             for s in it["sources"][:2] if s in arts)
            lines = [f"<b>{i}. {_t(it['headline'])}</b>", _t(it.get("summary"))]
            if it.get("tag"):
                lines[0] = f"<b>{i}. [{_t(it['tag'])}] {_t(it['headline'])}</b>"
            for k in ("why_it_matters", "economic_impact", "market_impact", "investment_implications", "opportunities",
                      "risks"):
                v = (it.get(k) or "").strip()
                if v and (sid != "executive_summary" or k in ("why_it_matters", "market_impact")):
                    lines.append(f"<i>{LABELS[k]}:</i> {_t(v)}")
            lines.append(links)
            blocks.append("\n".join(x for x in lines if x))
    ceo = brief.get("ceo")
    if ceo and (sections is None or "ceo_brief" in want):
        for k, t in (("know", "3 THINGS TO KNOW TODAY"), ("opportunities", "3 OPPORTUNITIES"), ("risks", "3 RISKS")):
            if ceo.get(k):
                blocks.append(f"<b>{t}</b>\n" + "\n".join(f"{i}. {_t(p['text'])}" for i, p in enumerate(ceo[k], 1)))
    if url:
        blocks.append(f'<a href="{_t(url)}">Open the full brief</a>')
    msgs, cur = [], ""
    for b in blocks:
        b = b[:3900]
        if len(cur) + len(b) + 2 > 3900:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def write_site(store_dir, brief):
    """Write today's page, its dated copy and the archive page under <outdir>/intel/."""
    import os
    from pathlib import Path
    d = Path(store_dir) / "intel"
    sub = "w" if brief.get("kind") == "weekly" else "d"
    (d / sub).mkdir(parents=True, exist_ok=True)
    html_ = page(brief)
    (d / sub / f"{brief['date']}.html").write_text(html_.replace('href="archive.html"', 'href="../archive.html"')
                                                   .replace('href="weekly.html"', 'href="../weekly.html"')
                                                   .replace('href="../app/"', 'href="../../app/"'), encoding="utf-8")
    (d / ("weekly.html" if sub == "w" else "index.html")).write_text(html_, encoding="utf-8")
    (d / "archive.html").write_text(archive_page(), encoding="utf-8")
    if sub == "d" and not (d / "weekly.html").exists():
        (d / "weekly.html").write_text(page(dict(kind="weekly", title="Weekly Intelligence Summary", items=[],
                                                 date_label="The first weekly summary arrives on Sunday.")),
                                       encoding="utf-8")
