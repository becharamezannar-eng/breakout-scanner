"""Build the daily brief (and the weekly summary) end to end."""
import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from . import prompts as P
from .cluster import cluster, pick, top_overall
from .collect import collect
from .config import SECTIONS, SECTION_BY_ID, TZ
from .llm import LLM, BudgetExceeded
from .markets import snapshot, as_text
from .verify import verify_many, SHOWABLE

log = logging.getLogger("intel.pipeline")
ITEM_FIELDS = ["headline", "summary", "why_it_matters", "economic_impact", "market_impact", "investment_implications",
               "opportunities", "risks", "tag", "themes", "sources"]
# Section priority: a story is used by at most two sections, higher priority first
PRIORITY = ["canada", "quebec", "ontario", "financial_markets", "global_economy", "united_states", "middle_east",
            "geopolitics", "ai_technology", "wealth", "business"]
POOL = 14            # stories offered to the AI per section


def now_et():
    return dt.datetime.now(ZoneInfo(TZ))


def _assign(clusters):
    """Stories per section, each story in at most two sections, before verification."""
    used, plan = {}, {}
    for sid in PRIORITY:
        chosen = []
        for c in pick(clusters, sid, POOL * 3):
            if used.get(c["cid"], 0) >= 2:
                continue
            chosen.append(c)
            if len(chosen) >= POOL + 6:          # a few spares in case links fail verification
                break
        plan[sid] = chosen
        for c in chosen[:POOL]:
            used[c["cid"]] = used.get(c["cid"], 0) + 1
    plan["executive_summary"] = top_overall(clusters, 36)
    return plan


def _verify_clusters(plan, settings, cache):
    """Verify links story by story (up to 3 articles each) until every section has enough showable stories."""
    order, seen = [], set()
    lists = [plan[k] for k in ["executive_summary"] + PRIORITY if k in plan]
    for rank in range(max((len(x) for x in lists), default=0)):        # round-robin: every section's best first
        for cl in lists:
            if rank < len(cl) and cl[rank]["cid"] not in seen:
                seen.add(cl[rank]["cid"])
                order.append(cl[rank])
    budget, todo = settings.max_verify, []
    for c in order:
        arts = sorted(c["articles"], key=lambda a: (a["tier"], a.get("paywall") != "none", a.get("lang") != "en"))[:2]
        if budget <= 0:
            break
        todo += arts
        budget -= len(arts)
    verify_many(todo, settings.workers, cache)
    for c in order:
        c["show"] = [a for a in c["articles"] if a.get("check") in SHOWABLE]
        for a in c["show"]:
            a["aid"] = "A" + a["id"]
    for sid in plan:
        plan[sid] = [c for c in plan[sid] if c.get("show")][:POOL if sid != "executive_summary" else 30]
    stats = {}
    for a in todo:
        stats[a.get("check", "unchecked")] = stats.get(a.get("check", "unchecked"), 0) + 1
    return stats


def _clean_items(items, allowed, limit):
    out = []
    for it in items or []:
        src = [s for s in it.get("sources", []) if s in allowed]
        if not src or not (it.get("headline") or "").strip():
            continue                                    # no verified source -> not published
        clean = {k: (it.get(k) or ("" if k not in ("themes", "sources") else [])) for k in ITEM_FIELDS}
        clean["sources"] = list(dict.fromkeys(src))
        clean["themes"] = [t.strip().lower() for t in clean["themes"] if t.strip()][:3]
        out.append(clean)
    return out[:limit]


def _compact(sections):
    """Today's items as text for the watchlist / CEO calls."""
    lines = []
    for sid, sec in sections.items():
        for it in sec.get("items", []):
            lines.append(f"- [{sec['title']}] {it['headline']}: {it['why_it_matters'] or it['summary']} "
                         f"{('Opportunity: ' + it['opportunities']) if it.get('opportunities') else ''} "
                         f"(sources: {', '.join(it['sources'])})")
    return "\n".join(lines)


def _headline_items(clusters, limit):
    """No AI key: the most important verified stories with their own descriptions, clearly labelled."""
    items = []
    for c in clusters[:limit]:
        lead = c["show"][0]
        items.append(dict({k: "" for k in ITEM_FIELDS}, headline=c["title"],
                          summary=(lead.get("summary") or lead.get("excerpt") or "")[:320], themes=[],
                          sources=[a["aid"] for a in c["show"][:3]]))
    return items


def build_brief(settings, cache, only=None, store=None):
    """The full daily brief. only = list of section ids to (re)build, for bot commands."""
    t0 = time.time()
    when = now_et()
    date_str = when.strftime("%A, %B %d, %Y")
    articles, health = collect(settings)
    clusters = cluster(articles)
    plan = _assign(clusters)
    if only:
        plan = {k: v for k, v in plan.items() if k in only}
    vstats = _verify_clusters(plan, settings, cache)
    snap = snapshot()
    mtext = as_text(snap)
    ai = settings.ai_enabled
    llm = LLM(settings) if ai else None
    sections, errors = {}, []

    def run_section(sid):
        sec = SECTION_BY_ID[sid]
        cl = plan.get(sid, [])
        allowed = {a["aid"] for c in cl for a in c["show"]}
        if not cl:
            return sid, dict(title=sec["title"], overview="", items=[])
        if not ai:
            return sid, dict(title=sec["title"], overview="", items=_headline_items(cl, sec.get("n", 6)))
        if sid == "executive_summary":
            user = P.exec_prompt(cl, date_str, mtext)
        else:
            mt = mtext if sid in ("financial_markets", "wealth", "global_economy", "canada", "united_states") else ""
            user = P.section_prompt(sec, cl, date_str, mt)
        out = llm.json(P.SYSTEM, user, P.SECTION_SCHEMA, name=sid)
        return sid, dict(title=sec["title"], overview=out.get("overview", ""),
                         items=_clean_items(out.get("items"), allowed, sec.get("n", 6)))

    phase_a = [s["id"] for s in SECTIONS if s["kind"] in ("exec", "items") and (not only or s["id"] in only)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in [ex.submit(run_section, sid) for sid in phase_a]:
            try:
                sid, sec = fut.result()
                sections[sid] = sec
            except BudgetExceeded as e:
                errors.append(str(e))
            except Exception as e:
                log.exception("section failed")
                errors.append(f"section failed: {str(e)[:200]}")
    ceo = None
    allowed_all = {a["aid"] for cl in plan.values() for c in cl for a in c["show"]}
    if ai and (not only or "watchlist" in only or "ceo_brief" in only):
        done = _compact(sections)
        try:
            if not only or "watchlist" in only:
                ca = [c for sid in ("canada", "quebec", "ontario", "wealth", "business") for c in plan.get(sid, [])][:18]
                allowed = allowed_all | {a["aid"] for c in ca for a in c["show"]}
                out = llm.json(P.SYSTEM, P.watchlist_prompt(done, ca, date_str), P.SECTION_SCHEMA, name="watchlist")
                sections["watchlist"] = dict(title=SECTION_BY_ID["watchlist"]["title"], overview=out.get("overview", ""),
                                             items=_clean_items(out.get("items"), allowed, 8))
            if not only or "ceo_brief" in only:
                out = llm.json(P.SYSTEM, P.ceo_prompt(_compact(sections), date_str), P.CEO_SCHEMA, name="ceo")
                ceo = {k: [dict(text=p["text"], sources=[s for s in p["sources"] if s in allowed_all])
                           for p in out.get(k, []) if p.get("text")][:3] for k in ("know", "opportunities", "risks")}
        except Exception as e:
            log.exception("watchlist/CEO failed")
            errors.append(f"watchlist/CEO failed: {str(e)[:200]}")
    # articles actually cited
    used = {s for sec in sections.values() for it in sec["items"] for s in it["sources"]}
    if ceo:
        used |= {s for k in ceo for p in ceo[k] for s in p["sources"]}
    amap = {}
    for cl in plan.values():
        for c in cl:
            for a in c["show"]:
                if a["aid"] in used:
                    amap[a["aid"]] = dict(title=a["title"], source=a["source"], url=a.get("final_url") or a["url"],
                                          published=a["published"].isoformat(), check=a["check"])
    ordered = {s["id"]: sections[s["id"]] for s in SECTIONS if s["id"] in sections}
    brief = dict(kind="daily", date=when.date().isoformat(), generated_at=when.isoformat(timespec="minutes"),
                 title="Global Intelligence & Wealth Brief", date_label=date_str, mode="ai" if ai else "headlines",
                 market=snap, sections=ordered, ceo=ceo, articles=amap, errors=errors,
                 stats=dict(collected=len(articles), stories=len(clusters),
                            sources_ok=sum(1 for h in health if h.get("ok")), sources_total=len(health),
                            links_checked=vstats, ai=llm.stats() if llm else None,
                            minutes=round((time.time() - t0) / 60, 1)),
                 source_health=health)
    return brief


# ------------------------------------------------------------------ weekly
def build_weekly(settings, store, end=None):
    end = end or now_et().date()
    start = end - dt.timedelta(days=6)
    briefs = [b for b in store.briefs_between(start, end) if b.get("kind") == "daily"]
    items, amap = [], {}
    for b in briefs:
        for sid, sec in b.get("sections", {}).items():
            for it in sec.get("items", []):
                items.append((b["date"], sec["title"], it))
        amap.update(b.get("articles", {}))
    trends = store.theme_trends(weeks=5, end=end)
    tc = "\n".join(f"{t['theme']}: {t['this_week']} this week, {t['avg_before']:.1f}/week before" for t in trends[:25])
    when = now_et()
    base = dict(kind="weekly", date=end.isoformat(), generated_at=when.isoformat(timespec="minutes"),
                title="Weekly Intelligence Summary", date_label=f"{start:%b %d} - {end:%b %d, %Y}",
                articles=amap, trends=trends[:15], days=len(briefs))
    if not settings.ai_enabled or not items:
        top = [it for d, s, it in items if s == "Executive Summary"][:10]
        return dict(base, mode="headlines", overview="", items=top, rising_themes=[], ceo=None)
    llm = LLM(settings)
    text = "\n".join(f"- {d} [{s}] {it['headline']}: {it['summary']} Why: {it['why_it_matters']} "
                     f"(themes: {', '.join(it['themes'])}; sources: {', '.join(it['sources'])})" for d, s, it in items)
    out = llm.json(P.SYSTEM, P.weekly_prompt(text[:180000], start, end, tc), P.WEEKLY_SCHEMA, name="weekly")
    allowed = set(amap)
    ceo = out.get("ceo") or {}
    return dict(base, mode="ai", overview=out.get("overview", ""), items=_clean_items(out.get("items"), allowed, 10),
                rising_themes=out.get("rising_themes", [])[:5],
                ceo={k: [dict(text=p["text"], sources=[s for s in p["sources"] if s in allowed])
                         for p in ceo.get(k, [])][:3] for k in ("know", "opportunities", "risks")},
                stats=dict(ai=llm.stats()))
