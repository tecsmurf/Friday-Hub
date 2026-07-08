"""Friday Hub — Memory system package.

Three-layer memory architecture:

- **short_term**: In-process key-value store with TTL-based expiry.
  Holds current conversation, active task state, and recent results.
- **long_term**: ChromaDB vector database for persistent semantic search.
  Enables finding similar past tasks and learning from history.
- **logger**: Structured JSON logs per task for debugging and analytics.

Modules:
    short_term      TTL key-value store (no external deps)
    long_term       ChromaDB semantic memory (lazy-loaded)
    logger          JSON task log writer and reader
"""
