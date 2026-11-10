"""Archive: every brief, item, cited article and run log; search; theme trends.

JsonStore  - files under reports/intel/ (GitHub mode: committed to the repo, published with the site)
SqlStore   - PostgreSQL (server, DATABASE_URL=postgresql://...) or SQLite (DATABASE_URL=sqlite:///path)
Both have the same methods, so the rest of the code doesn't care which one it gets.
"""
import datetime as dt
import json
import os
import re
from pathlib import Path


def _week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _index_rows(brief):
    rows, arts = [], brief.get("articles", {})
    sections = brief.get("sections") or {}
    if brief.get("kind") == "weekly":
        sections = {"weekly": dict(title="Weekly summary", items=brief.get("items", []))}
    for sid, sec in sections.items():
        for pos, it in enumerate(sec.get("items", [])):
            rows.append(dict(date=brief["date"], kind=brief.get("kind", "daily"), section=sid, title=sec.get("title", sid),
                             position=pos, headline=it.get("headline", ""), summary=(it.get("summary") or "")[:400],
                             why=(it.get("why_it_matters") or "")[:300], themes=it.get("themes", []),
                             sources=[dict(id=s, source=arts.get(s, {}).get("source", ""), url=arts.get(s, {}).get("url", ""))
                                      for s in it.get("sources", []) if s in arts], body=it))
    return rows


def _trends(rows, weeks, end):
    """rows: (date, theme). Theme counts per ISO week; rising = this week vs the average of the weeks before."""
    end = end or dt.date.today()
    keys = [_week(end - dt.timedelta(weeks=i)) for i in range(weeks)][::-1]
    counts = {}
    for d, theme in rows:
        wk = _week(d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)[:10]))
        if wk in keys:
            counts.setdefault(theme, {k: 0 for k in keys})[wk] += 1
    out = []
    for theme, per in counts.items():
        this, before = per[keys[-1]], [per[k] for k in keys[:-1]]
        avg = sum(before) / len(before) if before else 0.0
        out.append(dict(theme=theme, this_week=this, avg_before=round(avg, 2), total=sum(per.values()),
                        rising=this >= 2 and this > 1.5 * avg, weekly=per))
    out.sort(key=lambda t: (-t["this_week"], -t["total"]))
    return out


class JsonStore:
    def __init__(self, outdir):
        self.dir = Path(outdir) / "intel"
        (self.dir / "briefs").mkdir(parents=True, exist_ok=True)
        (self.dir / "weekly").mkdir(parents=True, exist_ok=True)

    def _path(self, kind, date):
        return self.dir / ("briefs" if kind == "daily" else "weekly") / f"{date}.json"

    def save_brief(self, brief):
        p = self._path(brief.get("kind", "daily"), brief["date"])
        p.write_text(json.dumps(brief, ensure_ascii=False, default=str), encoding="utf-8")
        idx = self._index()
        idx = [r for r in idx if not (r["date"] == brief["date"] and r["kind"] == brief.get("kind", "daily"))]
        idx += [{k: v for k, v in r.items() if k != "body"} for r in _index_rows(brief)]
        idx.sort(key=lambda r: (r["date"], r["kind"], r["section"], r["position"]), reverse=True)
        (self.dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        (self.dir / ("latest.json" if brief.get("kind", "daily") == "daily" else "weekly_latest.json")).write_text(
            json.dumps(brief, ensure_ascii=False, default=str), encoding="utf-8")
        (self.dir / "trends.json").write_text(json.dumps(self.theme_trends(8), ensure_ascii=False), encoding="utf-8")

    def _index(self):
        p = self.dir / "index.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def get_brief(self, date, kind="daily"):
        p = self._path(kind, date)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def latest(self, kind="daily"):
        files = sorted((self.dir / ("briefs" if kind == "daily" else "weekly")).glob("*.json"))
        return json.loads(files[-1].read_text(encoding="utf-8")) if files else None

    def briefs_between(self, start, end):
        out = []
        for p in sorted((self.dir / "briefs").glob("*.json")):
            if str(start) <= p.stem <= str(end):
                out.append(json.loads(p.read_text(encoding="utf-8")))
        return out

    def search(self, q, limit=30):
        terms = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
        hits = []
        for r in self._index():
            text = f"{r['headline']} {r['summary']} {' '.join(r['themes'])}".lower()
            if terms and all(t in text for t in terms):
                hits.append(r)
        return hits[:limit]

    def theme_trends(self, weeks=8, end=None):
        return _trends([(r["date"], t) for r in self._index() if r["kind"] == "daily" for t in r["themes"]], weeks, end)

    def log_run(self, kind, status, error="", stats=None):
        p = self.dir / "runs.json"
        runs = json.loads(p.read_text()) if p.exists() else []
        runs.insert(0, dict(at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), kind=kind,
                            status=status, error=error[:500], stats=stats or {}))
        p.write_text(json.dumps(runs[:200], default=str), encoding="utf-8")

    def runs(self, limit=20):
        p = self.dir / "runs.json"
        return (json.loads(p.read_text()) if p.exists() else [])[:limit]


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, brief_date TEXT NOT NULL,
  generated_at TEXT, mode TEXT, model TEXT, cost_usd REAL, data TEXT NOT NULL, UNIQUE(kind, brief_date));
CREATE TABLE IF NOT EXISTS brief_items (id INTEGER PRIMARY KEY, brief_id INTEGER REFERENCES briefs(id) ON DELETE CASCADE,
  brief_date TEXT, kind TEXT, section TEXT, position INTEGER, headline TEXT, summary TEXT, why TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS articles (aid TEXT PRIMARY KEY, url TEXT, source TEXT, title TEXT, published_at TEXT,
  check_status TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS item_articles (item_id INTEGER REFERENCES brief_items(id) ON DELETE CASCADE, aid TEXT);
CREATE TABLE IF NOT EXISTS item_themes (item_id INTEGER REFERENCES brief_items(id) ON DELETE CASCADE, theme TEXT,
  brief_date TEXT);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, at TEXT, status TEXT, error TEXT, stats TEXT);
CREATE INDEX IF NOT EXISTS ix_items_date ON brief_items(brief_date);
CREATE INDEX IF NOT EXISTS ix_themes ON item_themes(theme, brief_date);
"""


class SqlStore:
    """DB-API store. PostgreSQL via psycopg 3 (schema in deploy/schema.sql) or SQLite (schema above)."""

    def __init__(self, url):
        import threading
        self.lock = threading.RLock()                 # the bot and the scheduler share one connection
        self.pg = url.startswith(("postgres://", "postgresql://"))
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(url, autocommit=False)
            here = Path(__file__).resolve().parent.parent / "deploy" / "schema.sql"
            with self.conn.cursor() as cur:
                cur.execute(here.read_text())
            self.conn.commit()
        else:
            import sqlite3
            path = url.split("sqlite:///", 1)[-1] or ":memory:"
            if path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SQLITE_SCHEMA)
            self.conn.commit()

    def _sql(self, s):
        return s.replace("?", "%s") if self.pg else s

    def _exec(self, sql, args=(), fetch=None):
        with self.lock:
            return self._exec_unlocked(sql, args, fetch)

    def _exec_unlocked(self, sql, args=(), fetch=None):
        cur = self.conn.cursor()
        try:
            cur.execute(self._sql(sql), args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur
        finally:
            if fetch:
                cur.close()

    def save_brief(self, brief):
        kind, date = brief.get("kind", "daily"), brief["date"]
        data = json.dumps(brief, ensure_ascii=False, default=str)
        ai = (brief.get("stats") or {}).get("ai") or {}
        with self.lock:
            self._save(brief, kind, date, data, ai)

    def _save(self, brief, kind, date, data, ai):
        try:
            self._exec("DELETE FROM briefs WHERE kind=? AND brief_date=?", (kind, date))
            row = self._exec("INSERT INTO briefs(kind, brief_date, generated_at, mode, model, cost_usd, data) "
                             "VALUES (?,?,?,?,?,?,?) RETURNING id",
                             (kind, date, brief.get("generated_at"), brief.get("mode"), ai.get("model"),
                              ai.get("cost_usd"), data), fetch="one")
            bid = row[0]
            for aid, a in brief.get("articles", {}).items():
                self._exec("INSERT INTO articles(aid, url, source, title, published_at, check_status, first_seen) "
                           "VALUES (?,?,?,?,?,?,?) ON CONFLICT (aid) DO NOTHING",
                           (aid, a["url"], a["source"], a["title"], a.get("published"), a.get("check"),
                            brief.get("generated_at")))
            for r in _index_rows(brief):
                iid = self._exec("INSERT INTO brief_items(brief_id, brief_date, kind, section, position, headline, summary, "
                                 "why, body) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                                 (bid, date, kind, r["section"], r["position"], r["headline"], r["summary"], r["why"],
                                  json.dumps(r["body"], ensure_ascii=False)), fetch="one")[0]
                for s in r["sources"]:
                    self._exec("INSERT INTO item_articles(item_id, aid) VALUES (?,?)", (iid, s["id"]))
                for t in r["themes"]:
                    self._exec("INSERT INTO item_themes(item_id, theme, brief_date) VALUES (?,?,?)", (iid, t, date))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _j(v):
        return v if isinstance(v, (dict, list)) else json.loads(v or "null")      # Postgres JSONB comes back parsed

    def _load(self, row):
        return self._j(row[0]) if row else None

    def get_brief(self, date, kind="daily"):
        return self._load(self._exec("SELECT data FROM briefs WHERE kind=? AND brief_date=?", (kind, str(date)), "one"))

    def latest(self, kind="daily"):
        return self._load(self._exec("SELECT data FROM briefs WHERE kind=? ORDER BY brief_date DESC LIMIT 1", (kind,), "one"))

    def briefs_between(self, start, end):
        rows = self._exec("SELECT data FROM briefs WHERE kind='daily' AND brief_date BETWEEN ? AND ? ORDER BY brief_date",
                          (str(start), str(end)), "all")
        return [self._j(r[0]) for r in rows]

    def search(self, q, limit=30):
        if self.pg:
            rows = self._exec("SELECT brief_date, kind, section, headline, summary, why FROM brief_items "
                              "WHERE to_tsvector('english', coalesce(headline,'') || ' ' || coalesce(summary,'') || ' ' || "
                              "coalesce(why,'')) @@ websearch_to_tsquery('english', ?) "
                              "ORDER BY brief_date DESC, position LIMIT ?", (q, limit), "all")
        else:
            terms = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1] or [""]
            where = " AND ".join("lower(headline || ' ' || summary || ' ' || why) LIKE ?" for _ in terms)
            rows = self._exec(f"SELECT brief_date, kind, section, headline, summary, why FROM brief_items WHERE {where} "
                              f"ORDER BY brief_date DESC, position LIMIT ?", tuple(f"%{t}%" for t in terms) + (limit,), "all")
        return [dict(date=str(r[0]), kind=r[1], section=r[2], headline=r[3], summary=r[4], why=r[5]) for r in rows]

    def theme_trends(self, weeks=8, end=None):
        end = end or dt.date.today()
        start = end - dt.timedelta(weeks=weeks)
        rows = self._exec("SELECT t.brief_date, t.theme FROM item_themes t JOIN brief_items i ON i.id=t.item_id "
                          "WHERE i.kind='daily' AND t.brief_date >= ?", (str(start),), "all")
        return _trends([(dt.date.fromisoformat(str(d)[:10]), t) for d, t in rows], weeks, end)

    def log_run(self, kind, status, error="", stats=None):
        self._exec("INSERT INTO runs(kind, at, status, error, stats) VALUES (?,?,?,?,?)",
                   (kind, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), status, (error or "")[:2000],
                    json.dumps(stats or {}, default=str)))
        with self.lock:
            self.conn.commit()

    def runs(self, limit=20):
        rows = self._exec("SELECT at, kind, status, error, stats FROM runs ORDER BY id DESC LIMIT ?", (limit,), "all")
        return [dict(at=r[0], kind=r[1], status=r[2], error=r[3], stats=self._j(r[4]) or {}) for r in rows]


def make_store(settings):
    return SqlStore(settings.database_url) if settings.database_url else JsonStore(settings.outdir)
