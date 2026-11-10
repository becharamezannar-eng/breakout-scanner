"""Link verification, paywall / opinion detection and article text extraction.

A link is shown in the brief only if its check result is:
  ok        - the page loaded (HTTP 200) and is free to read, or
  protected - a tier-1 wire service (Reuters, AP) that blocks automated checks but is free to read in a browser;
              the URL comes straight from the publisher's feed or GDELT and is labelled as such.
Everything else is dropped: paywalled, broken (404/410/5xx/timeouts), opinion, blocked domain, login redirects.
"""
import datetime as dt
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from lxml import html as lhtml

from .collect import UA, domain_of, canonical_url

log = logging.getLogger("intel.verify")

BLOCKED_DOMAINS = {"wsj.com", "ft.com", "bloomberg.com", "economist.com", "barrons.com", "nytimes.com",
                   "washingtonpost.com", "thestar.com", "ledevoir.com", "theinformation.com", "seekingalpha.com",
                   "fool.com", "businessinsider.com", "telegraph.co.uk", "thetimes.co.uk", "hbr.org", "nationalpost.com",
                   "montrealgazette.com", "torontosun.com", "zerohedge.com"}
BOT_PROTECTED_OK = {"reuters.com", "apnews.com"}      # free to read, but often refuse automated requests
OPINION_PATH = re.compile(r"/(opinion|opinions|commentary|columnists?|editorials?|letters|blogs?|op-ed|chroniques?|"
                          r"opinions?-|debats?|analysis/opinion)(/|-|$)", re.I)
OPINION_TITLE = re.compile(r"^\s*(opinion|commentary|editorial|column|letters?|chronique|op-ed|analysis:? opinion)\b"
                           r"[:|\-–]?", re.I)
PAYWALL_TEXT = re.compile(r"(subscribe to (continue|keep) reading|this (article|story|content) is (only )?(available )?"
                          r"(for|to) (subscribers|members)|already a subscriber\?|become a subscriber to read|"
                          r"réservé aux abonnés|abonnez-vous pour lire|premium content|subscriber-only)", re.I)
LOGIN_PATH = re.compile(r"/(subscribe|subscription|login|signin|sign-in|paywall|register|account)(/|\?|$)", re.I)


def is_blocked(url):
    d = domain_of(url)
    return any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS)


def is_opinion(url, title=""):
    return bool(OPINION_PATH.search(urlparse(url).path) or OPINION_TITLE.match(title or ""))


def _jsonld(doc):
    out = []
    for s in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            d = json.loads(s)
        except Exception:
            continue
        stack = d if isinstance(d, list) else [d]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                out.append(x)
                for k in ("@graph", "mainEntity"):
                    v = x.get(k)
                    if isinstance(v, list):
                        stack += v
                    elif isinstance(v, dict):
                        stack.append(v)
            elif isinstance(x, list):
                stack += x
    return out


def inspect_html(content, url, policy="none"):
    """-> (status, excerpt, meta). status: ok | paywalled | opinion."""
    try:
        doc = lhtml.fromstring(content)
    except Exception:
        return "ok", "", {}
    meta = {}
    for m in doc.xpath("//meta[@property or @name]"):
        k = (m.get("property") or m.get("name") or "").lower()
        if k and m.get("content"):
            meta.setdefault(k, m.get("content"))
    ld = _jsonld(doc)
    types = " ".join(str(x.get("@type", "")) for x in ld).lower()
    free = [x.get("isAccessibleForFree") for x in ld if "isAccessibleForFree" in x]
    tier = (meta.get("article:content_tier") or "").lower()
    section = " ".join([str(x.get("articleSection", "")) for x in ld] + [meta.get("article:section", "")]).lower()
    if any(str(f).lower() in ("false", "0") for f in free) or tier == "locked" or (policy == "check" and tier == "metered"):
        return "paywalled", "", meta
    if "opinionnewsarticle" in types or re.search(r"\b(opinion|commentary|editorial|columnists?)\b", section):
        return "opinion", "", meta
    body = next((x.get("articleBody") for x in ld if isinstance(x.get("articleBody"), str) and len(x["articleBody"]) > 200), "")
    if not body:
        paras = doc.xpath("//article//p//text()") or doc.xpath("//main//p//text()") or doc.xpath("//p//text()")
        body = " ".join(t.strip() for t in paras if len(t.strip()) > 30)
    body = re.sub(r"\s+", " ", body).strip()
    page_text = " ".join(doc.xpath("//body//text()"))[:200000]
    if PAYWALL_TEXT.search(page_text) and len(body) < 900:
        return "paywalled", "", meta
    desc = meta.get("og:description") or meta.get("description") or ""
    excerpt = (body or desc)[:1500]
    return "ok", excerpt, meta


def check(article, session=None, cache=None):
    """Verify one article. Returns a dict merged into the article: check, final_url, http, excerpt, checked_at."""
    url = article["url"]
    key = "verify:" + canonical_url(url)
    if cache is not None:
        hit = cache.get_json(key)
        if hit:
            return hit
    res = dict(check="broken", final_url=url, http=None, excerpt="", checked_at=dt.datetime.now(dt.timezone.utc).isoformat())
    if is_blocked(url):
        res["check"] = "blocked"
    elif is_opinion(url, article.get("title", "")):
        res["check"] = "opinion"
    else:
        s = session or requests
        try:
            r = s.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                    "Accept-Language": "en-CA,en;q=0.9,fr;q=0.8"},
                      timeout=15, allow_redirects=True, stream=True)
            content = r.raw.read(1_500_000, decode_content=True) if r.status_code == 200 else b""
            r.close()
            res.update(http=r.status_code, final_url=r.url)
            dom = domain_of(r.url)
            if r.status_code == 200:
                if LOGIN_PATH.search(urlparse(r.url).path) or is_blocked(r.url):
                    res["check"] = "paywalled"
                elif "html" not in (r.headers.get("content-type") or "html"):
                    res["check"] = "ok"
                else:
                    st, excerpt, _ = inspect_html(content, r.url, article.get("paywall", "none"))
                    res.update(check=st, excerpt=excerpt)
            elif r.status_code in (401, 403, 429) and any(dom == d or dom.endswith("." + d) for d in BOT_PROTECTED_OK):
                res["check"] = "protected"
            else:
                res["check"] = "broken"
        except requests.RequestException as e:          # timeouts, DNS, TLS: never shown
            res.update(check="broken", http=type(e).__name__)
    if cache is not None and res["check"] != "broken":          # retry broken links next time
        cache.set_json(key, res, ttl=24 * 3600)
    return res


SHOWABLE = {"ok", "protected"}


def verify_many(articles, workers=12, cache=None):
    """Verify in parallel; returns the same articles with check fields added."""
    if not articles:
        return []
    with requests.Session() as sess, ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda a: check(a, sess, cache), articles))
    for a, r in zip(articles, results):
        a.update(r)
        if not a.get("excerpt"):
            a["excerpt"] = a.get("summary", "")
    counts = {}
    for a in articles:
        counts[a["check"]] = counts.get(a["check"], 0) + 1
    log.info("verified %d links: %s", len(articles), counts)
    return articles
