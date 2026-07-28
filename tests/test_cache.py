"""cache.py 与 matcher.extract_text 的测试。"""
import cache
from cache import MAX_ENTRIES, TTL_SECONDS
from matcher import extract_text


def setup_function():
    cache._store.clear()


def test_put_get_roundtrip():
    sid = cache.put({"totalHits": 1}, {"http://h/p": "text"})
    entry = cache.get(sid)
    assert entry["result"] == {"totalHits": 1}
    assert entry["texts"] == {"http://h/p": "text"}


def test_get_unknown_and_expired():
    assert cache.get("nonexistent") is None
    sid = cache.put({}, {})
    cache._store[sid]["ts"] -= TTL_SECONDS + 1
    assert cache.get(sid) is None
    assert sid not in cache._store


def test_eviction_at_max_entries():
    for i in range(MAX_ENTRIES + 5):
        cache.put({"i": i}, {})
    assert len(cache._store) == MAX_ENTRIES
    # 最旧的 5 条已被淘汰
    assert all(e["result"]["i"] >= 5 for e in cache._store.values())


def test_extract_text_skips_scripts_and_limits():
    html = ("<html><head><style>.x{color:red}</style>"
            "<script>var secret='hidden';</script></head><body>"
            "<p>Visible Alpha text</p><!-- comment --></body></html>")
    text = extract_text(html)
    assert "Visible Alpha text" in text
    assert "hidden" not in text and "color:red" not in text
    long_html = "<p>" + "字" * 5000 + "</p>"
    assert len(extract_text(long_html)) == 3000
