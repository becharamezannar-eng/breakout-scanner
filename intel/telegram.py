"""Telegram Bot API: send the brief, alerts, and answer commands (server mode).

Security: the bot only ever answers the one chat in TELEGRAM_CHAT_ID; messages from anyone else are ignored and
logged. Long polling is used, so the server needs no open port and no public web address.
"""
import logging
import threading
import time

import requests

log = logging.getLogger("intel.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

HELP = """<b>Global Intelligence &amp; Wealth Brief</b>
/brief - today's complete briefing (summary + CEO brief)
/markets - markets and investment briefing
/geopolitics - geopolitical briefing
/business - business opportunities
/wealth - wealth management briefing
/canada - Canada, Quebec and Ontario
/technology - AI and technology
/watchlist - opportunities and risks
/weekly - weekly intelligence summary
/search &lt;words&gt; - search the archive
/trends - themes gaining importance
/status - system health
Add <b>new</b> to rebuild with the latest news (e.g. <code>/markets new</code>); it takes a few minutes."""


def _call(token, method, **params):
    for attempt in range(4):
        try:
            r = requests.post(API.format(token=token, method=method), json=params, timeout=70)
            d = r.json()
            if d.get("ok"):
                return d["result"]
            if r.status_code == 429:
                time.sleep(int(d.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            raise RuntimeError(f"Telegram {method} failed: {d.get('description')}")
        except requests.RequestException as e:
            log.warning("Telegram %s error: %s", method, e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Telegram {method} failed after retries")


def send(settings, messages, chat=None):
    if not settings.telegram_token or not (chat or settings.telegram_chat):
        log.info("Telegram not configured; skipped %d message(s)", len(messages))
        return 0
    n = 0
    for m in messages:
        try:
            _call(settings.telegram_token, "sendMessage", chat_id=chat or settings.telegram_chat, text=m,
                  parse_mode="HTML", disable_web_page_preview=True)
        except RuntimeError as e:
            if "parse" in str(e).lower():          # never lose a brief to a formatting error: resend as plain text
                import re
                _call(settings.telegram_token, "sendMessage", chat_id=chat or settings.telegram_chat,
                      text=re.sub(r"<[^>]+>", "", m), disable_web_page_preview=True)
            else:
                raise
        n += 1
        time.sleep(0.6)
    return n


def alert(settings, text):
    """Error / health notifications. Never raises."""
    try:
        send(settings, [f"⚠️ <b>Brief system</b>\n{text[:3500]}"])
    except Exception as e:
        log.error("could not send alert: %s", e)


class Bot:
    """Long-polling command loop. handler(command, args) -> list of messages."""

    def __init__(self, settings, handler):
        self.s, self.handler = settings, handler
        self.offset = None
        self.last_poll = 0.0
        self.stop = threading.Event()

    def run(self):
        token, allowed = self.s.telegram_token, str(self.s.telegram_chat)
        _call(token, "setMyCommands", commands=[
            dict(command=c, description=d) for c, d in [
                ("brief", "Today's complete briefing"), ("markets", "Markets and investments"),
                ("geopolitics", "Geopolitics"), ("business", "Business opportunities"), ("wealth", "Wealth management"),
                ("canada", "Canada, Quebec, Ontario"), ("technology", "AI and technology"),
                ("watchlist", "Risks and opportunities"), ("weekly", "Weekly summary"), ("search", "Search the archive"),
                ("trends", "Rising themes"), ("status", "System health"), ("help", "All commands")]])
        log.info("bot started")
        while not self.stop.is_set():
            try:
                ups = _call(token, "getUpdates", timeout=50, offset=self.offset, allowed_updates=["message"])
                self.last_poll = time.time()
            except Exception as e:
                log.warning("poll failed: %s", e)
                time.sleep(10)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat != allowed:
                    log.warning("ignored message from chat %s", chat)
                    continue
                if not text.startswith("/"):
                    continue
                cmd, _, args = text[1:].partition(" ")
                cmd = cmd.split("@")[0].lower()
                threading.Thread(target=self._answer, args=(cmd, args.strip()), daemon=True).start()

    def _answer(self, cmd, args):
        try:
            send(self.s, self.handler(cmd, args))
        except Exception as e:
            log.exception("command failed")
            alert(self.s, f"/{cmd} failed: {e}")
