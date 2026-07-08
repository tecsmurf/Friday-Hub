"""
Structured Logger — JSON logs per task for debugging & analytics.

Every task execution produces a structured JSON log entry.
"""

import json
import time
from pathlib import Path
from friday.config import config, ROOT_DIR

_log_cfg = config.get("memory", {}).get("logs", {})
_LOG_DIR = ROOT_DIR / _log_cfg.get("directory", "data/logs")
_MAX_FILES = _log_cfg.get("max_files", 200)


def _ensure_dir():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_task(
    task_id: str,
    prompt: str,
    routing_decision: dict | None = None,
    agent: str | None = None,
    result: str | None = None,
    success: bool = True,
    duration: float = 0,
    feedback: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Log a complete task execution."""
    _ensure_dir()
    entry = {
        "id": task_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prompt": prompt,
        "routing": routing_decision,
        "agent": agent,
        "result": result[:2000] if result else None,
        "success": success,
        "duration_s": round(duration, 2),
        "feedback": feedback,
        **(extra or {}),
    }
    filepath = _LOG_DIR / f"{task_id}.json"
    filepath.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    _prune()
    return filepath


def get_recent(n: int = 10) -> list[dict]:
    """Get N most recent log entries."""
    _ensure_dir()
    files = sorted(_LOG_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    entries = []
    for f in files[:n]:
        try:
            entries.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return entries


def _prune():
    """Remove old logs beyond max_files."""
    files = sorted(_LOG_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    if len(files) > _MAX_FILES:
        for f in files[: len(files) - _MAX_FILES]:
            f.unlink(missing_ok=True)
