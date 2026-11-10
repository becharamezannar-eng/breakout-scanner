"""Deduplicate articles into story clusters, score them and route them to sections."""
import datetime as dt
import re
import unicodedata

from .config import SECTIONS, FOCUS_BOOST

STOP = set("""a an the and or but of to in on for at by with from as is are was were be been being has have had it its
this that these those after before over under into about amid says said say new more most than up down out off not no
will would could should may might can just also how why what when who where which while his her their our your they he
she we you i us via per vs report reports latest live update updates watch video photos says amid set sees seen
le la les un une des du de et en au aux pour par sur dans avec plus est sont qui que ce cette ses son leur""".split())


def _fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def tokens(title):
    words = re.findall(r"[a-z0-9&$%.]+", _fold(title.lower()))
    out = set()
    for w in words:
        w = w.strip(".")
        if len(w) < 3 or w in STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _similar(a, b):
    shared = len(a & b)
    if shared < 3:
        return False
    return shared / len(a | b) >= 0.34 or shared / min(len(a), len(b)) >= 0.6


def cluster(articles):
    """Greedy clustering on title tokens, with an inverted index so it stays fast for thousands of articles."""
    arts = sorted(articles, key=lambda a: (a["tier"], -a["published"].timestamp()))
    clusters, index = [], {}
    for a in arts:
        tk = tokens(a["title"])
        a["_tokens"] = tk
        cands = {}
        for t in tk:
            for ci in index.get(t, ()):
                cands[ci] = cands.get(ci, 0) + 1
        best = None
        for ci, n in sorted(cands.items(), key=lambda z: -z[1]):
            if n < 3:
                break
            if _similar(tk, clusters[ci]["tokens"]):
                best = ci
                break
        if best is None:
            clusters.append(dict(cid=len(clusters), articles=[a], tokens=set(tk)))
            for t in tk:
                index.setdefault(t, []).append(len(clusters) - 1)
        else:
            c = clusters[best]
            c["articles"].append(a)
            if len(c["articles"]) <= 4:            # let the cluster's vocabulary grow a little
                new = tk - c["tokens"]
                c["tokens"] |= tk
                for t in new:
                    index.setdefault(t, []).append(best)
    for c in clusters:
        _finish(c)
    return clusters


# Sports, entertainment, lifestyle and event notices: kept out of the brief unless they carry a clear economic or
# policy signal (then other keywords outweigh this).
NOISE = [" nba ", " nfl ", " nhl ", " mlb ", " mls ", "grizzlies", "raptors", "maple leafs", "canadiens", "blue jays",
         "olympic", "world cup", "playoff", "touchdown", "coach", "athlete", "marathon", "ultramarathon",
         "runs 100", "concert", "festival", "album", "movie", "box office", "celebrity", "royal family", "horoscope",
         "recipe", "art gallery", "museum", "obituary", "dies at", "snowfall", "lottery",
         "conference on", "webinar", "call for papers", "wedding", "pet ", " zoo"]


def _hits(text, keys):
    return sum(1 for k in keys if k in text)


def _finish(c, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    arts = c["articles"]
    lead = min(arts, key=lambda a: (a["tier"], a.get("lang") != "en", a.get("paywall") != "none"))
    c["title"] = lead["title"]
    c["outlets"] = sorted({a["source"] for a in arts})
    c["published"] = max(a["published"] for a in arts)
    text = " " + " ".join(_fold(f"{a['title']} {a.get('summary', '')}".lower()) for a in arts[:6]) + " "
    c["text"] = text
    feed_secs = {s for a in arts for s in a["sections"]}
    rel = {}
    for s in SECTIONS:
        keys = [_fold(k.lower()) for k in s.get("keys", [])]
        if not keys:
            continue
        h = _hits(text, keys) + (2 if s["id"] in feed_secs else 0)
        if h:
            rel[s["id"]] = h
    noise = _hits(text, NOISE)
    if noise:                                            # demote, and drop weak section matches entirely
        rel = {k: v - 2 * noise for k, v in rel.items() if v - 2 * noise >= 2}
    c["noise"] = noise
    c["relevance"] = rel
    c["sections"] = sorted(rel)
    age_h = (now - c["published"]).total_seconds() / 3600
    best_tier = min(a["tier"] for a in arts)
    c["score"] = round(3.0 * min(len(c["outlets"]), 6) + {1: 4, 2: 2, 3: 1}.get(best_tier, 0)
                       + 1.5 * min(_hits(text, [_fold(k) for k in FOCUS_BOOST]), 4)
                       - 0.12 * max(age_h - 6, 0) - 6 * min(noise, 2), 2)


def pick(clusters, section_id, n):
    """Best clusters for a section: importance x relevance."""
    cand = [c for c in clusters if section_id in c["relevance"]]
    cand.sort(key=lambda c: -(c["score"] + 2.0 * min(c["relevance"][section_id], 5)))
    return cand[:n]


def top_overall(clusters, n):
    return sorted(clusters, key=lambda c: -c["score"])[:n]
