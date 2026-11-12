"""Offline tests (no network, no AI key): python -m intel selftest"""
import datetime as dt
import json
import re
import tempfile
from types import SimpleNamespace

from . import cluster as C, collect as K, verify as V, render as R, pipeline as PL, store as ST, prompts as P
from .config import Settings

OK = True


def check(name, cond, detail=""):
    global OK
    OK &= bool(cond)
    print(f"intel   : {name:<58} " + ("ok" if cond else f"FAILED {str(detail)[:200]}"))


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Bank of Canada holds rate at 2.25% as inflation cools</title><link>https://www.cbc.ca/news/boc-1?utm_source=rss</link>
<description>&lt;p&gt;The central bank kept its key rate.&lt;/p&gt;</description><pubDate>Mon, 10 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Interest Rate Announcement</title><link>https://www.bankofcanada.ca/2026/12/fad-press-release-2026-12-09/</link><pubDate>Wed, 09 Dec 2026 14:45:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://www.cbc.ca/old</link><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>Statistics Canada: Labour Force Survey, October 2026</title><link rel="alternate" href="https://www150.statcan.gc.ca/n1/daily-quotidien/261107/dq261107a-eng.htm"/>
<updated>2026-11-10T08:30:00Z</updated><summary>Employment rose.</summary></entry></feed>"""


def page_html(body="", ld=None, meta=""):
    ld_s = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld else ""
    return f"<html><head>{meta}{ld_s}</head><body><article><p>{body}</p></article></body></html>".encode()


class Resp:
    def __init__(self, status=200, content=b"", url="", ctype="text/html"):
        self.status_code, self.url, self.headers = status, url, {"content-type": ctype}
        self.raw = SimpleNamespace(read=lambda n, decode_content=True: content[:n])
        self.content, self.text = content, content.decode("utf-8", "ignore")

    def close(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for pat, resp in self.routes.items():
            if pat in url:
                return resp(url) if callable(resp) else resp
        return Resp(200, page_html("A long factual article body " * 20), url)


def test_feeds():
    now = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
    items = K.parse_feed(RSS)
    check("RSS parsed (3 items, HTML stripped)", len(items) == 3 and items[0][2] == "The central bank kept its key rate.")
    atom = K.parse_feed(ATOM)
    check("Atom parsed (href links)", len(atom) == 1 and atom[0][1].startswith("https://www150.statcan.gc.ca"))
    src = dict(id="cbc", name="CBC News", url="https://x/rss", tier=2, sections=["canada"])
    arts, h = K.fetch_feed(src, now - dt.timedelta(hours=30), FakeSession({"x/rss": Resp(200, RSS, "")}))
    check("only recent, non-future articles kept", len(arts) == 1 and h["ok"] and h["items"] == 3, (len(arts), h))
    check("tracking parameters removed",
          K.canonical_url("https://www.cbc.ca/news/boc-1/?utm_source=rss&id=5#top") == "https://cbc.ca/news/boc-1?id=5")
    _, bad = K.fetch_feed(src, now, FakeSession({"x/rss": Resp(404, b"", "")}))
    check("broken feed reported, not fatal", bad["ok"] is False and bad["status"] == 404)
    for s in ["Mon, 10 Nov 2026 10:00:00 -0500", "2026-11-10T08:30:00Z", "20261110T081500Z"]:
        check(f"date parsed: {s}", K.parse_date(s) is not None)


def art(title, source, tier=2, url=None, sections=(), hours=2, lang="en", summary=""):
    now = dt.datetime.now(dt.timezone.utc)
    url = url or f"https://{source.lower().replace(' ', '')}.com/{abs(hash(title)) % 10**8}"
    return dict(id=K.article_id(url), url=url, title=title, summary=summary or title, source=source, source_id=source,
                tier=tier, lang=lang, published=now - dt.timedelta(hours=hours), sections=list(sections),
                paywall="none", origin="rss")


def test_cluster():
    a = [art("Bank of Canada holds key interest rate at 2.25% as inflation eases", "CBC News", sections=["canada"]),
         art("Bank of Canada holds interest rate steady at 2.25%, inflation eases", "Reuters", tier=1),
         art("BoC keeps key interest rate at 2.25% as inflation eases further", "Global News"),
         art("Dubai launches new golden visa rules for property investors", "Gulf News", sections=["middle_east"]),
         art("Legault government announces Hydro-Québec investment in Montreal", "La Presse", lang="fr"),
         art("Nvidia shares jump after record AI chip demand", "CNBC"),
         art("Toronto's Zach Edey finding footing in NBA with Memphis Grizzlies", "CBC News", sections=["ontario"])]
    cl = C.cluster(a)
    boc = next(c for c in cl if "Bank of Canada" in c["title"] or "BoC" in c["title"])
    check("same story from 3 outlets merged", len(boc["outlets"]) == 3, boc["outlets"])
    check("lead article = best tier (Reuters)", "steady" in boc["title"], boc["title"])
    check("7 articles -> 5 stories", len(cl) == 5, [c["title"] for c in cl])
    nba = next(c for c in cl if "NBA" in c["title"])
    check("sports story kept out of Ontario (economic) section", "ontario" not in nba["sections"], nba["relevance"])
    check("Toronto sports story goes to Montreal & Toronto local", "local" in nba["sections"], nba["relevance"])
    crime = C.cluster([art("Montreal police arrest suspect after shooting in Saint-Michel", "CBC News", sections=["quebec"]),
                       art("Man charged after stabbing in Scarborough, Toronto police say", "Global News", sections=["ontario"]),
                       art("Florida man charged in alligator theft", "CNBC")])
    loc = [c["title"][:20] for c in crime if "local" in c["sections"]]
    check("Montreal and Toronto crime -> local; other crime not", len(loc) == 2 and not any("Florida" in t for t in loc), loc)
    q = next(c for c in cl if "Legault" in c["title"])
    check("Quebec story routed to Quebec", "quebec" in q["sections"], q["sections"])
    d = next(c for c in cl if "Dubai" in c["title"])
    check("Dubai story routed to Middle East", "middle_east" in d["sections"], d["sections"])
    check("multi-outlet story ranks first", C.top_overall(cl, 1)[0]["cid"] == boc["cid"])


def test_verify():
    st, ex, _ = V.inspect_html(page_html("Real reporting about rates. " * 40), "https://cbc.ca/a")
    check("free article -> ok with excerpt", st == "ok" and len(ex) > 200)
    st, _, _ = V.inspect_html(page_html("x", ld={"@type": "NewsArticle", "isAccessibleForFree": False}), "https://g.com/a")
    check("JSON-LD isAccessibleForFree=false -> paywalled", st == "paywalled")
    st, _, _ = V.inspect_html(page_html("x", meta='<meta property="article:content_tier" content="metered">'),
                              "https://fp.com/a", policy="check")
    check("metered article from a 'check' source -> paywalled", st == "paywalled")
    st, _, _ = V.inspect_html(page_html("x", ld={"@type": "OpinionNewsArticle"}), "https://cbc.ca/a")
    check("OpinionNewsArticle -> opinion", st == "opinion")
    st, _, _ = V.inspect_html(page_html("Subscribe to continue reading. Short teaser."), "https://x.com/a")
    check("subscriber wall text -> paywalled", st == "paywalled")
    check("opinion URL detected", V.is_opinion("https://www.cbc.ca/news/opinion/rates-1.234"))
    check("opinion headline detected", V.is_opinion("https://x.com/a", "Opinion: Why rates must fall"))
    check("hard paywall domain blocked", V.is_blocked("https://www.wsj.com/articles/x"))
    sess = FakeSession({
        "dead": Resp(404, b"", "https://cbc.ca/dead"),
        "reuters.com": lambda u: Resp(401, b"", u),
        "other403": Resp(403, b"", "https://other403.com/a"),
        "wall": lambda u: Resp(200, page_html("x"), "https://globe.com/subscribe?next=1"),
        "lapresse.ca": lambda u: Resp(202, b"", u),
    })
    res = {k: V.check(art(k, "S", url=u), sess)["check"] for k, u in [
        ("dead", "https://cbc.ca/dead"), ("reuters", "https://www.reuters.com/world/x-2026-11-10/"),
        ("other", "https://other403.com/a"), ("wall", "https://globe.com/wall-story"),
        ("wsj", "https://www.wsj.com/a"), ("opinion", "https://cbc.ca/opinion/a"), ("good", "https://cbc.ca/good"),
        ("lapresse", "https://www.lapresse.ca/affaires/2026-11-10/x.php")]}
    want = dict(dead="broken", reuters="protected", other="broken", wall="paywalled", wsj="blocked",
                opinion="opinion", good="ok", lapresse="protected")
    check("link checks: broken / protected / paywalled / blocked / opinion / ok", res == want, res)


class FakeLLM:
    """Returns schema-shaped answers citing the ids it was given, plus one invented id to prove it's removed."""
    def __init__(self, s):
        self.calls = 0

    def json(self, system, user, schema, name="x", max_tokens=0):
        self.calls += 1
        assert "Hard rules" in system
        ids = re.findall(r"\[(A[0-9a-f]{12})\]", user) or re.findall(r"(A[0-9a-f]{12})", user)
        item = lambda i, src: {k: "" for k in PL.ITEM_FIELDS} | dict(
            headline=f"{name} item {i}", summary="Reported fact.", why_it_matters="Matters.", themes=["Inflation"],
            sources=src, tag="Quebec" if name == "watchlist" else "")
        if schema is P.CEO_SCHEMA:
            pt = [dict(text=f"point {i}", sources=ids[:1]) for i in range(3)]
            return dict(know=pt, opportunities=pt, risks=pt)
        items = [item(i, [ids[i % len(ids)], "Adeadbeef0000"]) for i in range(min(3, len(ids)))]
        items.append(item(9, ["Ainvented0000"]))                       # no valid source -> must be dropped
        out = dict(overview="Overview.", items=items)
        if schema is P.WEEKLY_SCHEMA:
            out.update(rising_themes=[dict(theme="inflation", why="more coverage")], ceo=self.json(system, user, P.CEO_SCHEMA))
        return out

    def stats(self):
        return dict(model="fake", calls=self.calls, input_tokens=0, output_tokens=0, cost_usd=0.0)


def _fake_world(monkey_ai=True):
    titles = [
        ("Bank of Canada holds key interest rate at 2.25% as inflation eases", ["canada"]),
        ("Quebec unveils plan to speed up Hydro-Québec dam construction in Montreal region", ["quebec"]),
        ("Ontario government announces Toronto transit expansion funding", ["ontario"]),
        ("Federal Reserve officials signal patience on interest rate cuts", ["united_states"]),
        ("Dubai property prices hit record as UAE investors pour in", ["middle_east"]),
        ("Stocks rally as oil prices fall and bond yields ease", ["financial_markets"]),
        ("OpenAI and Microsoft announce new AI automation tools for business", ["ai_technology"]),
        ("ETF inflows hit record as investors rotate into bond funds", ["wealth"]),
        ("Small business owners turn to AI marketing tools, survey finds", ["business"]),
        ("G7 leaders agree on new sanctions package at summit", ["geopolitics"]),
        ("IMF raises global growth forecast, warns on tariffs", ["global_economy"]),
    ]
    titles += [("Canadian banks raise mortgage rates for fixed-term borrowers", ["canada"]),
               ("Montreal startup scene draws record venture funding in Quebec", ["quebec"]),
               ("Ontario expands nuclear power plans at Darlington station", ["ontario"]),
               ("US retail sales beat forecasts in October as shoppers spend", ["united_states"]),
               ("Abu Dhabi sovereign fund boosts private credit bets", ["middle_east"]),
               ("Gold slips as dollar firms ahead of inflation data", ["financial_markets"]),
               ("Cybersecurity firms warn of new ransomware wave hitting hospitals", ["ai_technology"]),
               ("Pension funds shift allocations toward infrastructure assets", ["wealth"]),
               ("Franchise owners report labour shortage easing in service sector", ["business"]),
               ("Election results reshape coalition talks in Germany", ["geopolitics"]),
               ("OECD trims trade outlook as shipping costs climb", ["global_economy"]),
               ("Canada housing starts fall as builders cite high costs", ["canada"]),
               ("Quebec labour market adds jobs in construction and health", ["quebec"]),
               ("Ontario budget update projects smaller deficit", ["ontario"]),
               ("Saudi Arabia announces new tourism investment zone", ["middle_east"]),
               ("Cloud software spending climbs as companies adopt automation", ["ai_technology"]),
               ("Wealth managers see demand for dividend ETFs from retirees", ["wealth"]),
               ("Digital marketing agencies report demand for video ads", ["business"]),
               ("Canadiens beat Maple Leafs 4-2 at the Bell Centre", ["local"]),
               ("Toronto police arrest two after downtown shooting", ["local"])]
    arts = []
    for i, (t, secs) in enumerate(titles):
        arts.append(art(t, "CBC News", sections=secs, url=f"https://www.cbc.ca/news/story-{i}"))
        arts.append(art(t + " report", "Reuters", tier=1, sections=secs, url=f"https://www.reuters.com/story-{i}/"))
    PL.collect = lambda s: (arts, [dict(id="cbc", ok=True, items=11), dict(id="dead", ok=False, status=404)])
    PL.snapshot = lambda: [dict(ticker="^GSPC", name="S&P 500", kind="index", last=6800.5, chg=0.42, date="2026-11-09"),
                           dict(ticker="^TNX", name="US 10-year yield", kind="yield", last=4.21, chg=-0.03, date="2026-11-09")]
    orig_verify = V.verify_many

    def fake_verify(articles, workers=12, cache=None):
        for a in articles:
            a.update(check="protected" if "reuters" in a["url"] else "ok", final_url=a["url"],
                     excerpt="Excerpt text " * 10, http=200)
        return articles
    PL.verify_many = fake_verify
    if monkey_ai:
        PL.LLM = FakeLLM
    return orig_verify


def test_pipeline():
    _fake_world()
    s = Settings()
    s.anthropic_key, s.provider = "test", "anthropic"
    from .cache import MemoryCache
    b = PL.build_brief(s, MemoryCache())
    secs = b["sections"]
    check("AI brief has every section", all(k in secs for k in ("executive_summary", "canada", "quebec", "ontario",
                                                                 "middle_east", "wealth", "business", "watchlist")),
          list(secs))
    allitems = [it for sec in secs.values() for it in sec["items"]]
    check("items without a verified source are dropped", not any("item 9" in it["headline"] for it in allitems))
    check("invented source ids removed", all(src in b["articles"] for it in allitems for src in it["sources"]))
    check("every cited article has a URL and date", all(a["url"].startswith("https") and a["published"]
                                                       for a in b["articles"].values()))
    check("CEO brief 3/3/3", b["ceo"] and all(len(b["ceo"][k]) == 3 for k in ("know", "opportunities", "risks")))
    check("themes lower-cased", all(t == t.lower() for it in allitems for t in it["themes"]))
    html = R.page(b)
    check("page renders sections, CEO brief, sources", all(x in html for x in ("EXECUTIVE SUMMARY".title(), "CEO Brief",
                                                                              'id="sources"', "S&amp;P 500")), "")
    check("protected wire links labelled", "blocks automated link checks" in html)
    msgs = R.telegram_messages(b, url="https://x/intel/")
    check("Telegram push split under 4,000 chars", msgs and all(len(m) <= 4000 for m in msgs), [len(m) for m in msgs])
    check("Telegram push has summary + CEO brief + link", "EXECUTIVE SUMMARY" in msgs[0] and
          any("3 RISKS" in m for m in msgs) and "Open the full brief" in msgs[-1])
    sec_msgs = R.telegram_messages(b, sections=["canada", "quebec", "ontario", "local"])
    check("/canada returns Canada, Quebec, Ontario only", "QUEBEC" in "".join(sec_msgs) and
          "EXECUTIVE" not in "".join(sec_msgs))
    s2 = Settings()
    s2.anthropic_key, s2.openai_key = "", ""
    hb = PL.build_brief(s2, MemoryCache())
    check("no AI key -> headlines brief, labelled", hb["mode"] == "headlines" and hb["sections"]["canada"]["items"]
          and "Headlines only" in R.page(hb))
    return b


def test_store(b):
    with tempfile.TemporaryDirectory() as td:
        import os
        stores = [ST.JsonStore(td), ST.SqlStore(f"sqlite:///{td}/intel.db")]
        if os.environ.get("INTEL_TEST_DATABASE_URL"):                   # CI: real PostgreSQL
            stores.append(ST.SqlStore(os.environ["INTEL_TEST_DATABASE_URL"]))
        for store in stores:
            name = type(store).__name__
            store.save_brief(b)
            store.save_brief(b)                                              # idempotent re-save
            got = store.get_brief(b["date"])
            check(f"{name}: brief saved and read back", got and got["date"] == b["date"])
            check(f"{name}: latest()", store.latest()["date"] == b["date"])
            hits = store.search("item")
            check(f"{name}: archive search", len(hits) > 0, hits[:1])
            n_items = sum(len(sec["items"]) for sec in b["sections"].values())
            check(f"{name}: re-saving doesn't duplicate", len(store.search("item", 10000)) == n_items,
                  (len(store.search("item", 10000)), n_items))
            tr = store.theme_trends(4)
            check(f"{name}: theme trends", tr and tr[0]["theme"] == "inflation", tr[:1])
            store.log_run("daily", "ok", "", {"items": 3})
            check(f"{name}: run log", store.runs(1)[0]["status"] == "ok")
            w = PL.build_weekly(_ai_settings(), store)
            check(f"{name}: weekly summary built", w["kind"] == "weekly" and w["items"] and w["rising_themes"], w.get("mode"))
            store.save_brief(w)
            check(f"{name}: weekly stored separately", store.latest("weekly")["kind"] == "weekly"
                  and store.latest("daily")["kind"] == "daily")
        R.write_site(td, b)
        import os
        files = sorted(os.listdir(os.path.join(td, "intel")))
        check("site pages written (index, archive, weekly, dated copy)",
              {"index.html", "archive.html", "weekly.html", "d"} <= set(files), files)


def _ai_settings():
    s = Settings()
    s.anthropic_key = "test"
    return s


def test_gate():
    from . import __main__ as M
    from .cache import MemoryCache

    class S:
        def get_brief(self, d):
            return {"date": d}
    real = PL.now_et
    PL.now_et = lambda: dt.datetime(2026, 11, 10, 4, 20, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    check("gate: 4:20 am ET -> skip", M.run_daily(Settings(), S(), MemoryCache(), gate=True, send=False) is None)
    PL.now_et = lambda: dt.datetime(2026, 11, 10, 6, 20, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    check("gate: brief already made -> skip", M.run_daily(Settings(), S(), MemoryCache(), gate=True, send=False) is None)
    PL.now_et = real
    import os
    caches = [MemoryCache()]
    if os.environ.get("INTEL_TEST_REDIS_URL"):                            # CI: real Redis
        from .cache import RedisCache
        caches.append(RedisCache(os.environ["INTEL_TEST_REDIS_URL"]))
    for c in caches:
        n = type(c).__name__
        c.release("brief")
        check(f"{n}: second concurrent build refused", c.acquire("brief") and not c.acquire("brief"))
        c.release("brief")
        c.set_json("k", {"a": 1}, ttl=60)
        check(f"{n}: cache round trip", c.get_json("k") == {"a": 1})


def test_no_repeats():
    """Regression (Nov 11): 23 of 82 items repeated a story already shown in another section."""
    arts = [art("Quebec won't bail out CF Montreal, premier says, after owner puts club up for sale", "CBC News",
                sections=["quebec"]),
            art("Québec ne ferme pas la porte à une participation publique pour sauver le CF Montréal", "La Presse",
                lang="fr", sections=["quebec"]),
            art("Sans CF Montréal, la Première ligue canadienne doit viser Québec, selon Rocco Placentino", "Radio-Canada",
                lang="fr", sections=["quebec"])]
    cl = C.cluster(arts)
    check("same story in French and English merged", len(cl) <= 2, [c["title"] for c in cl])
    _fake_world()
    s2 = Settings()
    s2.anthropic_key, s2.openai_key = "", ""
    from .cache import MemoryCache
    for name, st in (("headlines", s2), ("AI", _ai_settings())):
        b = PL.build_brief(st, MemoryCache())
        heads, srcs = [], []
        for sid, sec in b["sections"].items():
            if sid == "watchlist":
                continue
            for it in sec["items"]:
                heads.append(it["headline"])
                srcs += it["sources"]
        check(f"{name} brief: no headline shown twice", len(heads) == len(set(heads)),
              [h for h in heads if heads.count(h) > 1][:3])
        check(f"{name} brief: no article used by two items", len(srcs) == len(set(srcs)))
    from .cluster import same_story
    for t1, t2, want in [("Nvidia prepares to oust Jensen Huang, WSJ reports", "The unfolding Jensen Huang scandal: a timeline", True),
                         ("Bank of Canada moving carefully, but more hikes might be needed", "Further interest rate hikes might be needed: Bank of Canada", True),
                         ("Wall Street rallies as AI stocks rebound", "Wall Street banks cut 2,000 jobs", False),
                         ("Bank of Canada holds key rate", "Canada housing starts fall", False)]:
        check(f"same story? {t1[:30]}... -> {want}", same_story(t1, t2) == want)
    # headline shown = the article actually linked (an op-ed title must never front another article's link)
    c = dict(title="Op-ed: Nvidia scandal", show=[dict(title="Nvidia's China problem", aid="A1", tier=2, lang="en",
                                                      summary="s")])
    it = PL._headline_items([c], 1)[0]
    check("headline comes from the linked article", it["headline"] == "Nvidia's China problem" and it["sources"] == ["A1"])


def main():
    test_feeds()
    test_cluster()
    test_verify()
    b = test_pipeline()
    test_store(b)
    test_gate()
    test_no_repeats()
    print("INTEL SELFTEST", "PASSED" if OK else "FAILED")
    return 0 if OK else 1
