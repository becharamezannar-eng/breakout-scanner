"""Command line:  python -m intel <command>

  run [--gate] [--no-send]   build today's brief, archive it, publish the pages, send it to Telegram
                             --gate: only between 5:00 and 9:00 am ET and only if today's brief doesn't exist yet
  weekly [--no-send]         build the weekly summary (Sundays automatically after `run`)
  probe                      check every source feed and a sample of links; writes intel/source_health.json
  search <words>             search the archive
  trends                     theme trends
  status                     last runs and health
  serve                      server mode: 6 am scheduler + Telegram commands + health endpoint
  selftest                   offline tests
"""
import argparse
import datetime as dt
import json
import logging
import sys
import time
import traceback

from .config import Settings, COMMANDS
from .cache import make_cache
from .store import make_store

log = logging.getLogger("intel")


def _setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run_daily(s, store, cache, gate=False, send=True, only=None):
    from . import pipeline, render, telegram
    today = pipeline.now_et()
    if gate:
        if not (5 <= today.hour < 9):
            print(f"Outside the morning window ({today:%H:%M} ET): nothing to do.")
            return None
        if store.get_brief(today.date().isoformat()):
            print("Today's brief already exists.")
            return None
    if not cache.acquire("brief", ttl=3600):
        print("Another brief is being built right now.")
        return None
    t0 = time.time()
    try:
        brief = pipeline.build_brief(s, cache, only=only)
        n_items = sum(len(x.get("items", [])) for x in brief["sections"].values())
        if not only:
            if n_items == 0:
                raise RuntimeError("the brief came out empty (no verified stories); check source_health")
            store.save_brief(brief)
            render.write_site(s.outdir, brief)
        sent = 0
        if send:
            sent = telegram.send(s, render.telegram_messages(brief, sections=only, full=s.telegram_full,
                                                             url=s.public_url))
        stats = dict(brief["stats"], items=n_items, telegram_messages=sent)
        stats.pop("source_health", None)
        store.log_run("daily" if not only else "section", "ok" if not brief["errors"] else "partial",
                      "; ".join(brief["errors"]), stats)
        if brief["errors"] and send:
            telegram.alert(s, "Today's brief was published with gaps: " + "; ".join(brief["errors"])[:800])
        print(f"Brief {brief['date']} ({brief['mode']}): {n_items} items, "
              f"{brief['stats']['collected']} articles, {time.time() - t0:.0f}s, AI {brief['stats']['ai']}")
        if not only and today.weekday() == 6:
            run_weekly(s, store, send=send)
        return brief
    except Exception as e:
        store.log_run("daily", "failed", f"{e}\n{traceback.format_exc()[-1500:]}")
        if send:
            telegram.alert(s, f"Today's brief failed: {e}")
        raise
    finally:
        cache.release("brief")


def run_weekly(s, store, send=True):
    from . import pipeline, render, telegram
    w = pipeline.build_weekly(s, store)
    store.save_brief(w)
    render.write_site(s.outdir, w)
    store.log_run("weekly", "ok", "", dict(days=w.get("days"), items=len(w.get("items", []))))
    if send:
        telegram.send(s, render.telegram_messages(w, url=s.public_url.rstrip("/") + "/weekly.html"))
    print(f"Weekly summary {w['date_label']}: {len(w.get('items', []))} items from {w.get('days')} briefs")
    return w


def probe(s, cache):
    """Check every feed and a sample of links from each; the report drives source fixes."""
    from .collect import collect, load_sources
    from .verify import verify_many
    from pathlib import Path
    arts, health = collect(s)
    by_src = {}
    for a in arts:
        by_src.setdefault(a["source_id"], []).append(a)
    sample = [a for v in by_src.values() for a in v[:3]]
    verify_many(sample, s.workers, cache)
    checks = {}
    for a in sample:
        k = f'{a["check"]}:{a.get("http")}'
        checks.setdefault(a["source_id"], {}).setdefault(k, 0)
        checks[a["source_id"]][k] += 1
    for h in health:
        h["link_checks"] = checks.get(h["id"], {})
    for sid, c in checks.items():
        if sid.startswith("gdelt_"):
            health.append(dict(id=sid, ok=True, status="links", items=len(by_src[sid]), link_checks=c))
    import requests, time as _t
    from .collect import GDELT_API, UA
    for q in ["domain:reuters.com canada", "canada domainis:reuters.com", "(canada OR canadian) domain:reuters.com",
              "domain:apnews.com", "sourcecountry:canada economy"]:
        _t.sleep(10)
        try:
            r = requests.get(GDELT_API, params=dict(query=q, mode="artlist", format="json", maxrecords=50, timespan="24h"),
                             headers={"User-Agent": UA}, timeout=40)
            n = len(r.json().get("articles", [])) if r.text.strip().startswith("{") else -1
            health.append(dict(id=f"gdelt-test:{q}", ok=n > 0, status=r.status_code, items=n, note=r.text[:80] if n < 0 else ""))
        except Exception as e:
            health.append(dict(id=f"gdelt-test:{q}", ok=False, status=type(e).__name__, items=0))
    out = Path(s.outdir) / "intel"
    out.mkdir(parents=True, exist_ok=True)
    (out / "source_health.json").write_text(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
                                                             sources=health), indent=1, default=str))
    bad = [h for h in health if not h.get("ok")]
    for h in health:
        print(f"{'OK ' if h.get('ok') else 'BAD'} {h['id']:<40} status={h.get('status')} items={h.get('items')} "
              f"recent={h.get('recent', '')} links={h.get('link_checks', {})}")
    print(f"{len(health) - len(bad)}/{len(health)} sources OK, {len(arts)} recent articles")
    return health


def _published(s, store, today):
    """GitHub-produced brief (INTEL_SCHEDULE=off): fetch today's published copy and archive it locally."""
    if not s.public_url:
        return None
    try:
        import requests
        r = requests.get(s.public_url.rstrip("/") + "/latest.json", timeout=20)
        b = r.json() if r.status_code == 200 else None
        if b and b.get("date") == today:
            store.save_brief(b)
            return b
    except Exception as e:
        log.warning("could not read the published brief: %s", e)
    return None


def command_handler(s, store, cache):
    """Telegram commands (server mode)."""
    from . import pipeline, render

    def handle(cmd, args):
        if cmd in ("start", "help"):
            from .telegram import HELP
            return [HELP]
        if cmd == "status":
            runs = store.runs(5)
            lines = [f"{r['at'][:16]} {r['kind']}: {r['status']}" + (f" ({r['error'][:120]})" if r["error"] else "")
                     for r in runs]
            return ["<b>Status</b>\n" + ("\n".join(lines) or "No runs yet.")]
        if cmd == "search":
            hits = store.search(args or "", 12)
            if not hits:
                return [f"Nothing found for <b>{args}</b>."]
            return ["<b>Archive: " + args + "</b>\n" + "\n".join(
                f"{h['date']} - {h['headline']}" for h in hits)]
        if cmd == "trends":
            tr = store.theme_trends(8)[:12]
            return ["<b>Themes this week</b> (count; average per week before)\n" + "\n".join(
                f"{'↑ ' if t['rising'] else ''}{t['theme']}: {t['this_week']} ({t['avg_before']})" for t in tr)
                    or "Not enough history yet."]
        if cmd == "weekly":
            w = store.latest("weekly")
            if args == "new" or not w:
                w = run_weekly(s, store, send=False)
            return render.telegram_messages(w)
        if cmd not in COMMANDS:
            return ["Unknown command. /help lists them."]
        today = pipeline.now_et().date().isoformat()
        b = store.get_brief(today) or _published(s, store, today)
        if args == "new" or not b:
            if args == "new" and COMMANDS[cmd]:
                b = run_daily(s, store, cache, send=False, only=COMMANDS[cmd])
            else:
                b = run_daily(s, store, cache, send=False)
            if b is None:
                return ["A brief is being built right now; try again in a few minutes."]
        return render.telegram_messages(b, sections=COMMANDS[cmd], url=s.public_url)
    return handle


def main(argv=None):
    _setup_logging()
    ap = argparse.ArgumentParser(prog="intel")
    ap.add_argument("command", choices=["run", "weekly", "probe", "search", "trends", "status", "serve", "selftest"])
    ap.add_argument("words", nargs="*")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--outdir")
    a = ap.parse_args(argv)
    s = Settings()
    if a.outdir:
        s.outdir = a.outdir
    if a.command == "selftest":
        from .selftest import main as st
        return st()
    cache, store = make_cache(s), make_store(s)
    if a.command == "run":
        run_daily(s, store, cache, gate=a.gate, send=not a.no_send)
    elif a.command == "weekly":
        run_weekly(s, store, send=not a.no_send)
    elif a.command == "probe":
        probe(s, cache)
    elif a.command == "search":
        for h in store.search(" ".join(a.words)):
            print(h["date"], h["section"], "-", h["headline"])
    elif a.command == "trends":
        for t in store.theme_trends(8)[:20]:
            print(f"{t['theme']:<28} this week {t['this_week']:>3}  before {t['avg_before']:>5}  "
                  f"{'RISING' if t['rising'] else ''}")
    elif a.command == "status":
        for r in store.runs(10):
            print(r["at"], r["kind"], r["status"], r["error"][:150])
    elif a.command == "serve":
        from .service import serve
        serve(s, store, cache, command_handler(s, store, cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
