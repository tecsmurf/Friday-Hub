"""Shared test setup.

Friday reads its config and data paths at import time, so isolate the
runtime home *before* any ``friday`` module is imported: tests must never
write to the developer's real memory, logs, or version lock.
"""

import os
import tempfile

os.environ["FRIDAY_HOME"] = tempfile.mkdtemp(prefix="friday_test_home_")
os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:9")  # closed port: never hit a real model
os.environ.pop("OPENROUTER_API_KEY", None)
