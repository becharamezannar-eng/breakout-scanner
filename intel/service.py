"""Server mode (Docker on a VPS): daily scheduler + Telegram command bot + health endpoint, in one process.

- Daily brief starts at INTEL_BRIEF_TIME (default 05:40 Eastern) so it is ready by 6:00; DST handled by zoneinfo.
- If it fails it retries at +15 and +45 minutes, and you get a Telegram alert.
- Watchdog: if no brief exists by 07:00 Eastern, you get an alert.
- Weekly summary: Sundays after the daily brief.
- GET /healthz -> 200 when healthy (used by Docker's healthcheck), JSON with the details.
"""
import datetime as dt
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from .config import TZ

log = logging.getLogger("intel.service")
STATE = dict(started=time.time(), last_brief=None, last_error=None, next_brief=None)


def _next(hhmm, now):
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def scheduler(s, store, cache, stop):
    from .__main__ import run_daily
    from .telegram import alert
    tz = ZoneInfo(TZ)
    while not stop.is_set():
        now = dt.datetime.now(tz)
        nxt = _next(s.brief_time, now)
        STATE["next_brief"] = nxt.isoformat(timespec="minutes")
        watchdog = now.replace(hour=7, minute=0, second=0, microsecond=0)
        # sleep in short steps so the watchdog and shutdown stay responsive
        while not stop.is_set() and dt.datetime.now(tz) < nxt:
            n = dt.datetime.now(tz)
            if n >= watchdog and STATE.get("watchdog_day") != n.date() and not store.get_brief(n.date().isoformat()):
                STATE["watchdog_day"] = n.date()
                alert(s, "No brief yet at 7:00 am. Check `docker compose logs intel`.")
            time.sleep(30)
        for attempt, delay in enumerate([0, 15 * 60, 30 * 60]):
            if stop.wait(delay):
                return
            try:
                run_daily(s, store, cache)
                STATE.update(last_brief=dt.datetime.now(tz).isoformat(timespec="minutes"), last_error=None)
                break
            except Exception as e:
                STATE["last_error"] = f"{dt.datetime.now(tz):%Y-%m-%d %H:%M}: {e}"
                log.exception("daily brief failed (attempt %d)", attempt + 1)


class Health(BaseHTTPRequestHandler):
    bot = None

    def do_GET(self):
        if self.path.rstrip("/") not in ("/healthz", ""):
            self.send_response(404)
            self.end_headers()
            return
        bot_ok = self.bot is None or time.time() - self.bot.last_poll < 180
        ok = bot_ok and not STATE.get("last_error")
        body = json.dumps(dict(ok=ok, bot_polling=bot_ok, **{k: v for k, v in STATE.items() if k != "watchdog_day"}),
                          default=str).encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(s, store, cache, handler):
    from .telegram import Bot, alert
    stop = threading.Event()
    bot = Bot(s, handler) if s.telegram_token and s.telegram_chat else None
    Health.bot = bot
    srv = ThreadingHTTPServer(("0.0.0.0", s.health_port), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if s.schedule:
        threading.Thread(target=scheduler, args=(s, store, cache, stop), daemon=True).start()
    else:
        STATE["next_brief"] = "built by GitHub (INTEL_SCHEDULE=off)"
    log.info("service up: brief at %s %s, health on :%d, bot %s", s.brief_time if s.schedule else "(GitHub)", TZ, s.health_port,
             "on" if bot else "off (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    if bot:
        alert(s, "Brief service started. Send /help for commands.")
        try:
            bot.run()
        finally:
            stop.set()
    else:
        while True:
            time.sleep(3600)
