"""Collect recent articles from RSS/Atom feeds, GDELT and (optionally) NewsAPI.

Every article is a plain dict:
  id, url, title, summary, source, source_id, tier, lang, published (aware UTC datetime),
  sections (list), paywall ('none'|'check'), origin ('rss'|'gdelt'|'newsapi')
"""
import datetime as dt
import email.utils
import hashlib
import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import yaml
from lxml import etree

from .config import HERE

log = logging.getLogger("intel.collect")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
      "IntelBrief/1.0")
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
           "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8"}
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|cmpid|taid|ref$|src$|__twitter|xtor|cid$|at_)", re.I)


def load_sources(path=None):
    with open(path or HERE / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def canonical_url(u):
    """Strip tracking parameters and fragments so the same story from two feeds dedupes."""
    try:
        p = urlparse(u.strip())
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
        return urlunparse((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), p.path.rstrip("/") or "/",
                           "", urlencode(q), ""))
    except Exception:
        return u


def article_id(url):
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


def clean_text(s, limit=600):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            m = re.match(r"(\d{8})T?(\d{6})Z?", s)          # GDELT 20261110T081500Z
            d = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _txt(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text
        # namespaced (Atom, dc, content)
        for child in el:
            tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else ""
            if tag == n.split(":")[-1] and (child.text or "").strip():
                return child.text
    return ""


def parse_feed(content):
    """RSS 2.0 / RSS 1.0 / Atom -> list of (title, link, summary, published_str)."""
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(content, parser=parser)
    if root is None:
        return []
    out = []
    items = [e for e in root.iter() if isinstance(e.tag, str) and e.tag.split("}")[-1] in ("item", "entry")]
    for it in items:
        title = _txt(it, "title")
        link = ""
        for child in it:
            if not isinstance(child.tag, str) or child.tag.split("}")[-1] != "link":
                continue
            href = child.get("href")
            if href and child.get("rel", "alternate") == "alternate":
                link = href
                break
            if (child.text or "").strip():
                link = child.text.strip()
                break
        if not link:
            link = _txt(it, "guid") if (_txt(it, "guid") or "").startswith("http") else ""
        summary = _txt(it, "description", "summary", "content")
        published = _txt(it, "pubDate", "published", "updated", "date")
        if title and link:
            out.append((clean_text(title, 300), link.strip(), clean_text(summary), published))
    return out


def fetch_feed(src, since, session=None):
    s = session or requests
    t0 = time.time()
    try:
        r = s.get(src["url"], headers=HEADERS, timeout=20)
        status = r.status_code
        if status != 200:
            return [], dict(id=src["id"], ok=False, status=status, items=0, secs=round(time.time() - t0, 1))
        entries = parse_feed(r.content)
    except Exception as e:
        return [], dict(id=src["id"], ok=False, status=type(e).__name__, items=0, secs=round(time.time() - t0, 1))
    arts = []
    for title, link, summary, published in entries:
        pub = parse_date(published)
        if pub is None or pub < since:
            continue
        arts.append(dict(id=article_id(link), url=link, title=title, summary=summary, source=src["name"],
                         source_id=src["id"], tier=src.get("tier", 2), lang=src.get("lang", "en"), published=pub,
                         sections=list(src.get("sections", [])), paywall=src.get("paywall", "none"), origin="rss"))
    return arts, dict(id=src["id"], ok=True, status=200, items=len(entries), recent=len(arts),
                      secs=round(time.time() - t0, 1))


GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
SOURCE_NAMES = {"reuters.com": "Reuters", "apnews.com": "Associated Press", "bnnbloomberg.ca": "BNN Bloomberg",
                "cbc.ca": "CBC News", "ctvnews.ca": "CTV News", "globalnews.ca": "Global News",
                "financialpost.com": "Financial Post", "cnbc.com": "CNBC", "bbc.com": "BBC News",
                "bbc.co.uk": "BBC News", "thenationalnews.com": "The National", "gulfnews.com": "Gulf News",
                "khaleejtimes.com": "Khaleej Times", "aljazeera.com": "Al Jazeera", "marketwatch.com": "MarketWatch",
                "theglobeandmail.com": "The Globe and Mail", "lapresse.ca": "La Presse",
                "ici.radio-canada.ca": "Radio-Canada"}
TIER1_DOMAINS = {"reuters.com", "apnews.com"}
CHECK_DOMAINS = {"financialpost.com", "theglobeandmail.com", "marketwatch.com"}


def domain_of(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def fetch_gdelt(cfg, hours, session=None, budget_s=240):
    """GDELT DOC 2.0 article lists. Polite: one request every 6 seconds."""
    s = session or requests
    allowed = set(cfg.get("domains", []))
    arts, health = [], []
    deadline = time.time() + budget_s                   # GDELT is a bonus source: never let it delay the brief
    for i, q in enumerate(cfg.get("queries", [])):
        if time.time() > deadline:
            health.append(dict(id=f"gdelt:{q['q'][:40]}", ok=False, status="skipped (time budget)", items=0))
            continue
        params = dict(query=q["q"] + " sourcelang:english", mode="artlist", format="json", maxrecords=200,
                      timespan=f"{hours}h", sort="datedesc")
        try:
            for attempt in range(3):                       # shared cloud IPs often hit GDELT's 1-per-5s limit
                time.sleep(7 if attempt == 0 else 12 * attempt)
                r = s.get(GDELT_API, params=params, headers={"User-Agent": UA}, timeout=40)
                if r.status_code != 429:
                    break
            data = r.json() if r.status_code == 200 and r.text.strip().startswith("{") else {}
            rows = data.get("articles", [])
            health.append(dict(id=f"gdelt:{q['q'][:40]}", ok=r.status_code == 200 and bool(data), status=r.status_code,
                               items=len(rows), note="" if data else r.text[:120]))
        except Exception as e:
            health.append(dict(id=f"gdelt:{q['q'][:40]}", ok=False, status=type(e).__name__, items=0))
            continue
        for a in rows:
            url, dom = a.get("url", ""), domain_of(a.get("url", ""))
            base = next((d for d in allowed if dom == d or dom.endswith("." + d)), None)
            if not base or not a.get("title"):
                continue
            arts.append(dict(id=article_id(url), url=url, title=clean_text(a["title"], 300), summary="",
                             source=SOURCE_NAMES.get(base, base), source_id=f"gdelt_{base}",
                             tier=1 if base in TIER1_DOMAINS else 2, lang="en", published=parse_date(a.get("seendate")),
                             sections=list(q.get("sections", [])), paywall="check" if base in CHECK_DOMAINS else "none",
                             origin="gdelt"))
    return [a for a in arts if a["published"]], health


def fetch_newsapi(key, hours, session=None):
    """Optional. Note: NewsAPI's free plan is for development only; a paid plan is needed in production."""
    if not key:
        return [], []
    s = session or requests
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    arts, health = [], []
    for q, secs in [("Canada OR Quebec OR Ontario", ["canada"]), ("Dubai OR UAE", ["middle_east"]),
                    ("\"interest rates\" OR inflation OR ETF", ["global_economy", "wealth"])]:
        try:
            r = s.get("https://newsapi.org/v2/everything", timeout=20,
                      params=dict(q=q, language="en", sortBy="publishedAt", pageSize=60, **{"from": since}),
                      headers={"X-Api-Key": key, "User-Agent": UA})
            rows = r.json().get("articles", []) if r.status_code == 200 else []
            health.append(dict(id=f"newsapi:{q}", ok=r.status_code == 200, status=r.status_code, items=len(rows)))
        except Exception as e:
            health.append(dict(id=f"newsapi:{q}", ok=False, status=type(e).__name__, items=0))
            continue
        for a in rows:
            url = a.get("url") or ""
            if not url or not a.get("title"):
                continue
            arts.append(dict(id=article_id(url), url=url, title=clean_text(a["title"], 300),
                             summary=clean_text(a.get("description")), source=(a.get("source") or {}).get("name") or
                             domain_of(url), source_id="newsapi", tier=2, lang="en",
                             published=parse_date(a.get("publishedAt")), sections=secs, paywall="check", origin="newsapi"))
    return [a for a in arts if a["published"]], health


def collect(settings, sources=None, now=None):
    """All recent articles (deduplicated by canonical URL) and per-source health."""
    sources = sources or load_sources()
    now = now or dt.datetime.now(dt.timezone.utc)
    hours = settings.lookback_hours + (48 if now.astimezone().weekday() == 0 else 0)     # Mondays: include the weekend
    since = now - dt.timedelta(hours=hours)
    arts, health = [], []
    with requests.Session() as sess, ThreadPoolExecutor(max_workers=settings.workers) as ex:
        for got, h in ex.map(lambda s: fetch_feed(s, since, sess), sources.get("rss", [])):
            arts += got
            health.append(h)
        try:
            g, gh = fetch_gdelt(sources.get("gdelt", {}), min(hours, 72), sess) if settings.gdelt else ([], [])
            arts += g
            health += gh
        except Exception as e:
            health.append(dict(id="gdelt", ok=False, status=type(e).__name__, items=0))
        n, nh = fetch_newsapi(settings.newsapi_key, hours, sess)
        arts += n
        health += nh
    seen, out = {}, []
    for a in arts:
        k = a["id"]
        if k in seen:                       # same URL from two feeds: keep the better tier, merge sections
            b = seen[k]
            b["sections"] = sorted(set(b["sections"]) | set(a["sections"]))
            if not b["summary"] and a["summary"]:
                b["summary"] = a["summary"]
            continue
        seen[k] = a
        out.append(a)
    log.info("collected %d articles from %d sources", len(out), sum(1 for h in health if h.get("ok")))
    return out, health
