import time

from astolfo.cache import TTLCache, normalize


def test_normalize():
    assert normalize("  Hello   World  ") == "hello world"
    assert normalize("سلام  دنیا") == "سلام دنیا"
    assert normalize(None) == ""


def test_hit_and_miss():
    cache: TTLCache[str] = TTLCache(maxsize=4, ttl=60)
    assert cache.get("a") is None
    cache.set("a", "value")
    assert cache.get("a") == "value"
    assert cache.hits == 1
    assert cache.misses == 1
    assert cache.hit_rate == 0.5


def test_expiry():
    cache: TTLCache[str] = TTLCache(maxsize=4, ttl=0.05)
    cache.set("a", "value")
    time.sleep(0.08)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_lru_eviction():
    cache: TTLCache[int] = TTLCache(maxsize=2, ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")          # refresh a
    cache.set("c", 3)       # evicts b
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_disabled_when_ttl_is_zero():
    cache: TTLCache[int] = TTLCache(maxsize=4, ttl=0)
    cache.set("a", 1)
    assert cache.get("a") is None
