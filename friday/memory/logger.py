"""
Structured Logger — JSON logs per task for debugging & analytics.

Every task execution produces a structured JSON log entry.
"""

import json
import re
import time
from pathlib import Path

from friday.config import config, ROOT_DIR

_log_cfg = config.get("memory", {}).get("logs", {})
_LOG_DIR = ROOT_DIR / _log_cfg.get("directory", "data/logs")
_MAX_FILES = _log_cfg.get("max_files", 200)


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _ensure_dir():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_path(task_id: str) -> Path:
    """Path of a task's log file; rejects IDs that could escape the log dir."""
    if not _TASK_ID_RE.match(task_id) or task_id in (".", ".."):
        raise ValueError(f"Invalid task id: {task_id!r}")
    return _LOG_DIR / f"{task_id}.json"


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
    filepath = _log_path(task_id)
    filepath.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    _prune()
    return filepath


def add_feedback(task_id: str, rating: str, comment: str = "") -> bool:
    """Attach user feedback to an existing task log. Returns False if not found."""
    try:
        filepath = _log_path(task_id)
    except ValueError:
        return False
    if not filepath.exists():
        return False
    entry = json.loads(filepath.read_text(encoding="utf-8"))
    entry["feedback"] = {"rating": rating, "comment": comment or None}
    filepath.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    return True


def get_recent(n: int = 10) -> list[dict]:
    """Get N most recent log entries."""
    _ensure_dir()
    # Sort by name-embedded time first: feedback edits must not reorder history.
    files = sorted(_LOG_DIR.glob("*.json"), key=_sort_key, reverse=True)
    entries = []
    for f in files[:n]:
        try:
            entries.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return entries


def _sort_key(path: Path) -> tuple:
    """Order logs by the creation time encoded in ``task_<unix>_<hex>``."""
    parts = path.stem.split("_")
    if len(parts) >= 2 and parts[1].isdigit():
        return (int(parts[1]), path.stat().st_mtime)
    return (int(path.stat().st_mtime), path.stat().st_mtime)


def _prune():
    """Remove old logs beyond max_files."""
    files = sorted(_LOG_DIR.glob("*.json"), key=_sort_key)
    if len(files) > _MAX_FILES:
        for f in files[: len(files) - _MAX_FILES]:
            f.unlink(missing_ok=True)
