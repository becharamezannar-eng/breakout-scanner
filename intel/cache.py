"""Cache and locks: Redis on a server (REDIS_URL), in-memory otherwise (GitHub runs)."""
import json
import threading
import time


class MemoryCache:
    def __init__(self):
        self._d, self._lock = {}, threading.Lock()

    def get_json(self, key):
        with self._lock:
            v = self._d.get(key)
            if not v or (v[1] and v[1] < time.time()):
                return None
            return json.loads(v[0])

    def set_json(self, key, value, ttl=None):
        with self._lock:
            self._d[key] = (json.dumps(value, default=str), time.time() + ttl if ttl else None)

    def acquire(self, name, ttl=3600):
        with self._lock:
            v = self._d.get("lock:" + name)
            if v and v[1] > time.time():
                return False
            self._d["lock:" + name] = ("1", time.time() + ttl)
            return True

    def release(self, name):
        with self._lock:
            self._d.pop("lock:" + name, None)

    def ping(self):
        return True


class RedisCache:
    def __init__(self, url):
        import redis                                    # only needed on a server
        self.r = redis.Redis.from_url(url, socket_timeout=5, decode_responses=True)

    def get_json(self, key):
        v = self.r.get("intel:" + key)
        return json.loads(v) if v else None

    def set_json(self, key, value, ttl=None):
        self.r.set("intel:" + key, json.dumps(value, default=str), ex=ttl)

    def acquire(self, name, ttl=3600):
        """One brief at a time, even if the scheduler and a Telegram command fire together."""
        return bool(self.r.set("intel:lock:" + name, "1", nx=True, ex=ttl))

    def release(self, name):
        self.r.delete("intel:lock:" + name)

    def ping(self):
        return self.r.ping()


def make_cache(settings):
    if settings.redis_url:
        try:
            c = RedisCache(settings.redis_url)
            c.ping()
            return c
        except Exception:
            pass
    return MemoryCache()
