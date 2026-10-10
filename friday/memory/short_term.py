"""
Short-Term Memory — In-memory store for active task context.

No external dependencies. Uses a simple dict with TTL-based expiry.
Stores the current conversation, recent results, and active task state.
"""

import itertools
import time

from friday.config import config

_mem_cfg = config.get("memory", {}).get("short_term", {})
_MAX_ENTRIES = _mem_cfg.get("max_entries", 50)
_TTL = _mem_cfg.get("ttl_seconds", 3600)

# The store: { key: { "value": ..., "ts": timestamp, "seq": insertion order } }
# ``seq`` breaks ties: time.time() can return the same value for consecutive
# writes (≈15 ms resolution on Windows), which made recency order unreliable.
_store: dict[str, dict] = {}
_seq = itertools.count()


def put(key: str, value) -> None:
    """Store a value with automatic TTL."""
    _prune()
    _store[key] = {"value": value, "ts": time.time(), "seq": next(_seq)}
    # Enforce max entries
    if len(_store) > _MAX_ENTRIES:
        oldest = min(_store, key=lambda k: _store[k]["seq"])
        _store.pop(oldest, None)


def get(key: str, default=None):
    """Get a value. Returns default if expired or missing."""
    entry = _store.get(key)
    if entry is None:
        return default
    if time.time() - entry["ts"] > _TTL:
        _store.pop(key, None)
        return default
    return entry["value"]


def get_recent(n: int = 10) -> list[dict]:
    """Get the N most recent entries."""
    _prune()
    sorted_items = sorted(_store.items(), key=lambda x: x[1]["seq"], reverse=True)
    return [
        {"key": k, "value": v["value"], "ts": v["ts"]}
        for k, v in sorted_items[:n]
    ]


def get_conversation() -> list[dict]:
    """Get the current conversation history from short-term memory."""
    return get("_conversation", [])


def add_to_conversation(role: str, content: str) -> None:
    """Append a message to the conversation history."""
    conv = get("_conversation", [])
    conv.append({"role": role, "content": content})
    # Keep last 20 messages to avoid context overflow
    if len(conv) > 20:
        conv = conv[-20:]
    put("_conversation", conv)


def clear() -> None:
    """Clear all short-term memory."""
    _store.clear()


def _prune() -> None:
    """Remove expired entries."""
    now = time.time()
    expired = [k for k, v in _store.items() if now - v["ts"] > _TTL]
    for k in expired:
        _store.pop(k, None)
