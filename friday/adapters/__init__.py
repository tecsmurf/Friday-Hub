"""Friday Hub — Agent adapters package.

Provides a unified interface for all supported agent CLIs. Each adapter
wraps a third-party command-line tool behind the :class:`BaseAdapter`
interface, enabling transparent detection, health checking, and execution.

Classes:
    BaseAdapter         Abstract base with shared detection/execution logic
    AdapterStatus       Dataclass representing an adapter's current state
    ExecResult          Dataclass representing a task execution result
    ClaudeCodeAdapter   Adapter for the ``claude`` CLI
    CodexAdapter        Adapter for the ``codex`` CLI
    OpenClawAdapter     Adapter for the ``openclaw`` CLI
"""

from friday.adapters.base import BaseAdapter, AdapterStatus, ExecResult
from friday.adapters.claude_code import ClaudeCodeAdapter
from friday.adapters.codex import CodexAdapter
from friday.adapters.openclaw import OpenClawAdapter

__all__ = [
    "BaseAdapter",
    "AdapterStatus",
    "ExecResult",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "OpenClawAdapter",
]
