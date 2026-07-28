"""In-memory store for search results used by AI summarize/ask."""
from __future__ import annotations

import time
import uuid

TTL_SECONDS = 600
MAX_ENTRIES = 20

_store: dict[str, dict] = {}


def put(result: dict, texts: dict[str, str]) -> str:
    _evict()
    search_id = uuid.uuid4().hex
    _store[search_id] = {"ts": time.time(), "result": result, "texts": texts}
    return search_id


def get(search_id: str) -> dict | None:
    entry = _store.get(search_id)
    if not entry:
        return None
    if time.time() - entry["ts"] > TTL_SECONDS:
        del _store[search_id]
        return None
    return entry


def _evict() -> None:
    now = time.time()
    for sid in [s for s, e in _store.items()
                if now - e["ts"] > TTL_SECONDS]:
        del _store[sid]
    while len(_store) >= MAX_ENTRIES:
        oldest = min(_store, key=lambda s: _store[s]["ts"])
        del _store[oldest]
