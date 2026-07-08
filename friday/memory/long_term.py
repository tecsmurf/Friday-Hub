"""
Long-Term Memory — ChromaDB vector store for persistent intelligence.

Stores past task executions, learnings, and patterns.
Enables semantic search: "find similar tasks I've done before."
"""

from pathlib import Path
from friday.config import config, ROOT_DIR

_mem_cfg = config.get("memory", {}).get("long_term", {})
_DB_PATH = str(ROOT_DIR / _mem_cfg.get("db_path", "data/memory"))
_COLLECTION = _mem_cfg.get("collection_name", "friday_memory")
_MAX_RESULTS = _mem_cfg.get("max_results", 5)

# Lazy init — ChromaDB is only loaded when first accessed
_client = None
_collection = None


def _get_collection():
    """Lazy-initialize ChromaDB."""
    global _client, _collection
    if _collection is not None:
        return _collection

    import chromadb

    Path(_DB_PATH).mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=_DB_PATH)
    _collection = _client.get_or_create_collection(name=_COLLECTION)
    return _collection


def store(
    task_id: str,
    prompt: str,
    output: str,
    agent: str,
    metadata: dict | None = None,
) -> None:
    """Store a task execution in long-term memory."""
    col = _get_collection()
    doc = f"Task: {prompt}\nAgent: {agent}\nResult: {output[:500]}"
    meta = {
        "agent": agent,
        "prompt": prompt[:200],
        **(metadata or {}),
    }
    # Filter out None values — ChromaDB doesn't accept them
    meta = {k: v for k, v in meta.items() if v is not None}
    col.upsert(ids=[task_id], documents=[doc], metadatas=[meta])


def search(query: str, n: int | None = None) -> list[dict]:
    """Semantic search across past task executions."""
    col = _get_collection()
    n = n or _MAX_RESULTS

    if col.count() == 0:
        return []

    results = col.query(query_texts=[query], n_results=min(n, col.count()))

    entries = []
    for i, doc_id in enumerate(results["ids"][0]):
        entries.append({
            "id": doc_id,
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
            "distance": results["distances"][0][i] if results.get("distances") else None,
        })
    return entries


def count() -> int:
    """How many entries in long-term memory."""
    try:
        return _get_collection().count()
    except Exception:
        return 0
